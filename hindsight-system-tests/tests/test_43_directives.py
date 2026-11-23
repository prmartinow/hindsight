"""A directive is a hard rule, and it reaches the model or it is nothing.

Disposition shades how an agent reasons; a directive is not negotiable — "always
answer in French", "never speculate about medical matters". Which means the only
thing that makes a directive real is that its text arrives in the prompt. A
directive stored, listed, returned by the API and then dropped somewhere between
the bank and the model is worse than no feature at all: the caller believes a
constraint is in force and it is not.

That delivery is deterministic and therefore assertable here, without judging
what the model does with it. Whether the model *obeys* is a question for a real
model and the `hs_llm_core` judge tests; whether the server *told* it is this
suite's business.
"""

from __future__ import annotations

import httpx
import pytest

from hindsight_system_tests import reflect_loop
from hindsight_system_tests.payloads import consolidation, extracted, fact

pytestmark = pytest.mark.asyncio

QUERY = "Where does Alice live?"
DIRECTIVE = "Always answer in French."


@pytest.fixture
async def bank_with_facts(client, llm, bank_id, settled) -> str:
    llm.on_step("extract_facts").returns(
        extracted(fact("Alice moved to Berlin", who="Alice", entities=["Alice", "Berlin"]))
    )
    llm.on_step("consolidate").returns(consolidation())
    await client.aretain(bank_id=bank_id, content="Alice moved to Berlin.")
    await settled(bank_id)
    return bank_id


async def test_a_directive_round_trips_through_the_api(client, bank_with_facts):
    created = await client.directives.create_directive(bank_with_facts, {"name": "french", "content": DIRECTIVE})

    assert created.content == DIRECTIVE
    assert created.is_active is True

    listing = await client.directives.list_directives(bank_with_facts)
    assert [d.content for d in listing.items] == [DIRECTIVE]


async def test_the_directive_text_reaches_the_reflect_prompt(client, llm, bank_with_facts):
    """The assertion that makes the feature real.

    Storing it is easy and provable; delivering it is the part that can silently
    stop happening, and nothing in the response would show it.
    """
    await client.directives.create_directive(bank_with_facts, {"name": "french", "content": DIRECTIVE})
    reflect_loop(llm, answer="Alice habite à Berlin.")

    await client.areflect(bank_id=bank_with_facts, query=QUERY)

    prompts = llm.prompts_for("reflect")
    assert prompts, "reflect took no search turn"
    assert any(DIRECTIVE in prompt for prompt in prompts), "the directive never reached the model"


async def test_the_directive_is_presented_as_mandatory(client, llm, bank_with_facts):
    """Delivered *as a rule*. The same sentence buried in ordinary context is a
    suggestion; the heading is what tells the model it is not optional."""
    await client.directives.create_directive(bank_with_facts, {"name": "french", "content": DIRECTIVE})
    reflect_loop(llm, answer="Alice habite à Berlin.")

    await client.areflect(bank_id=bank_with_facts, query=QUERY)

    prompt = next(p for p in llm.prompts_for("reflect") if DIRECTIVE in p)
    assert "DIRECTIVES (MANDATORY)" in prompt


async def test_a_bank_without_directives_carries_none(client, llm, bank_with_facts):
    """The other direction: no directive, no section. A prompt that always ships
    an empty directives block trains the model to skim past a heading that
    sometimes matters."""
    reflect_loop(llm, answer="Alice lives in Berlin.")

    await client.areflect(bank_id=bank_with_facts, query=QUERY)

    prompts = llm.prompts_for("reflect")
    assert prompts
    assert not any("DIRECTIVES (MANDATORY)" in prompt for prompt in prompts)


async def test_a_deleted_directive_stops_being_sent(client, llm, bank_with_facts):
    """Revocation has to reach the model too — a rule that cannot be withdrawn is
    as broken as one that never arrives."""
    created = await client.directives.create_directive(bank_with_facts, {"name": "french", "content": DIRECTIVE})
    await client.directives.delete_directive(bank_with_facts, created.id)

    reflect_loop(llm, answer="Alice lives in Berlin.")
    await client.areflect(bank_id=bank_with_facts, query=QUERY)

    assert not any(DIRECTIVE in prompt for prompt in llm.prompts_for("reflect"))


async def test_conflicting_edit_and_restore_after_deletion(hindsight_server, client, bank_id):
    await client.acreate_bank(bank_id=bank_id, name="Directive revisions")
    async with httpx.AsyncClient(base_url=hindsight_server.url) as http:
        collection = f"/v1/default/banks/{bank_id}/directives"
        created = await http.post(
            collection, json={"name": "units", "content": "Use metric units.", "tags": ["science"]}
        )
        created.raise_for_status()
        row = created.json()
        url = f"{collection}/{row['id']}"
        original = row["revision"]
        edit = {"content": "Use SI units.", "expected_revision": original}
        updated = await http.patch(url, json=edit)
        updated.raise_for_status()
        newest = updated.json()["revision"]
        assert newest > original
        assert (await http.patch(url, json=edit)).status_code == 409

        deleted = await http.delete(url, params={"expected_revision": newest})
        deleted.raise_for_status()
        assert (await http.get(url)).status_code == 404
        history = await http.get(f"{url}/revisions")
        history.raise_for_status()
        latest, changed, first = history.json()
        assert latest["deleted"] and changed["snapshot"]["content"] == "Use SI units."
        assert first["revision"] == original and first["snapshot"]["content"] == "Use metric units."

        restore = await http.post(f"{url}/revert", json={"revision": original, "expected_revision": latest["revision"]})
        restore.raise_for_status()
        assert restore.json()["restored_from_revision"] == original
        current = (await http.get(url)).json()
        assert current["id"] == row["id"] and current["revision"] == restore.json()["revision"]
        assert current["content"] == "Use metric units." and current["tags"] == ["science"]
