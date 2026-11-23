"""A Git-backed rule edit survives interruption between commit and publication.

A preexisting bank has two native directives. An operator enrolls just one into Git.
A validator holds the publication after the next commit, and the caller recovers the
pending change using the returned SHA. All client reads go through the ordinary service.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from hindsight_client import Hindsight
from hindsight_client_api.exceptions import ApiException

from hindsight_system_tests import start_hindsight_server

pytestmark = pytest.mark.asyncio


async def _post(client, bank, operation, body):
    api = client.directives.api_client
    response = await api.call_api(
        "POST",
        f"{api.configuration.host}/v1/default/banks/{bank}/directive-git/{operation}",
        header_params={"Content-Type": "application/json"},
        body=body,
    )
    payload = json.loads(await response.read())
    assert response.status == 200, payload
    return payload


async def test_a_failed_publication_is_recoverable_by_sha(stub_server, client, bank_id, tmp_path):
    await client.acreate_bank(bank_id)
    style = await client.directives.create_directive(
        bank_id,
        {"name": "House style", "content": "Prefer summaries.", "tags": ["style"], "priority": 12},
    )
    dates = await client.directives.create_directive(bank_id, {"name": "Other rule", "content": "Mention dates."})
    repository = tmp_path / "rules.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(repository)], check=True)
    hold = tmp_path / "hold"
    extension_dir = Path(__file__).resolve().parents[1] / "server_extensions"
    server = start_hindsight_server(
        stub_url=stub_server.url,
        log_path=tmp_path / "managed-server.log",
        extra_env={
            "HINDSIGHT_API_DIRECTIVE_GIT_REPOSITORIES": json.dumps({"public": {bank_id: str(repository)}}),
            "DIRECTIVE_STORY_HOLD": str(hold),
            "HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION": "directive_publication_gate:DirectivePublicationGate",
            "PYTHONPATH": os.pathsep.join(filter(None, [str(extension_dir), os.environ.get("PYTHONPATH")])),
        },
    )
    managed = Hindsight(base_url=server.url)
    try:
        selection = {"directive_ids": [style.id]}
        preview = await _post(managed, bank_id, "adopt", selection)
        assert preview["enrolled"] == [style.id]
        assert preview["created"] == preview["updated"] == preview["deleted"] == []
        assert (await _post(managed, bank_id, "sync", {}))["head"] is None
        baseline = (await _post(managed, bank_id, "adopt", {**selection, "apply": True}))["revision"]

        hold.write_text(baseline)
        with pytest.raises(ApiException) as pending:
            await managed.directives.update_directive(bank_id, style.id, {"content": "Use short paragraphs."})
        status = await _post(managed, bank_id, "sync", {})
        revision = status["head"]
        assert pending.value.status == 409
        detail = json.loads(pending.value.body)["detail"]
        assert revision != baseline and detail["revision"] == revision and detail["state"] == "pending"
        assert status["pending"] and status["revision"] == baseline
        assert (await managed.directives.get_directive(bank_id, style.id)).content == "Prefer summaries."

        hold.unlink()
        target = {"revision": revision, "expected_revision": baseline}
        changes = await _post(managed, bank_id, "sync", target)
        assert changes["updated"] == [style.id]
        await _post(managed, bank_id, "sync", {**target, "apply": True})
        repeated = await _post(managed, bank_id, "sync", {**target, "apply": True})
        assert repeated["revision"] == revision and repeated["updated"] == []
        live = await managed.directives.get_directive(bank_id, style.id)
        assert (live.content, live.tags, live.priority) == ("Use short paragraphs.", ["style"], 12)

        await managed.directives.update_directive(bank_id, dates.id, {"content": "Use ISO dates."})
        rules = await managed.directives.list_directives(bank_id)
        assert {rule.id: rule.content for rule in rules.items} == {style.id: live.content, dates.id: "Use ISO dates."}
        assert await _post(managed, bank_id, "sync", {}) == {"head": revision, "revision": revision, "pending": False}
    finally:
        await managed.aclose()
        server.stop()
