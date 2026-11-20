"""A retained causal link is readable through the built-in bank MCP tool."""

import json

import httpx
import pytest

from hindsight_system_tests.payloads import FactCausalRelation, consolidation, extracted, fact


@pytest.mark.asyncio
async def test_retained_causal_relationship_is_queryable_over_mcp(client, llm, hindsight_server, bank_id, settled):
    extraction = extracted(
        fact("The pump lost power"),
        fact("The pump stopped", causal_relations=[FactCausalRelation(target_index=0)]),
    )
    llm.on_step("extract_facts", contains="pump").returns(extraction)
    llm.on_step("consolidate").returns(consolidation())

    await client.aretain(bank_id=bank_id, content="The pump stopped because it lost power.", document_id="pump")
    await settled(bank_id)
    await client.aupdate_bank_config(bank_id, mcp_enabled_tools=["get_causal_subgraph"])

    async with httpx.AsyncClient(
        base_url=hindsight_server.url, headers={"Accept": "application/json, text/event-stream"}, timeout=30
    ) as http:
        path = f"/mcp/{bank_id}/"

        async def post(method: str, params: dict, request_id: int | None = None) -> httpx.Response:
            payload = {"jsonrpc": "2.0", "method": method, "params": params}
            if request_id is not None:
                payload["id"] = request_id
            response = await http.post(path, json=payload)
            response.raise_for_status()
            return response

        initialized = await post(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "system-test", "version": "1"},
            },
            1,
        )
        if session_id := initialized.headers.get("Mcp-Session-Id"):
            http.headers["Mcp-Session-Id"] = session_id
        await post("notifications/initialized", {})
        response = await post(
            "tools/call",
            {
                "name": "get_causal_subgraph",
                "arguments": {"query_or_entity": "pump", "relationship_filter": "caused_by"},
            },
            2,
        )

    if response.headers["content-type"].startswith("text/event-stream"):
        data = next(line[5:].strip() for line in response.text.splitlines() if line.startswith("data:"))
        body = json.loads(data)
    else:
        body = response.json()
    result = body["result"]
    assert not result.get("isError"), result
    payload = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert payload["bank_id"] == bank_id
    assert payload["count"] == 1
    link = payload["causal_links"][0]
    assert link["relationship"] == "caused_by"
    assert "The pump stopped" in link["from_text"]
    assert "The pump lost power" in link["to_text"]
