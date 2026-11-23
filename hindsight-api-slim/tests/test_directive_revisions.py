"""PostgreSQL directive history shares native write transactions and authorization."""

import asyncio
from unittest.mock import Mock
from uuid import uuid4

import pytest

from hindsight_api.engine.cross_encoder import RRFPassthroughCrossEncoder
from hindsight_api.engine.embeddings import Embeddings
from hindsight_api.engine.schema import _is_oracle, fq_table
from hindsight_api.extensions import OperationValidationError
from hindsight_api.models import RequestContext


# Directive writes use neither embeddings nor reranking.
@pytest.fixture
def embeddings():
    return Mock(spec=Embeddings, dimension=384, provider_name="stub")


@pytest.fixture
def cross_encoder():
    return RRFPassthroughCrossEncoder()


@pytest.fixture
async def bank(memory):
    if _is_oracle():
        pytest.skip("PostgreSQL revision history")
    bank_id = f"directive-revisions-{uuid4().hex}"
    await memory.update_bank(bank_id, name="history", request_context=RequestContext())
    try:
        yield bank_id
    finally:
        await memory.delete_bank(bank_id, request_context=RequestContext())


@pytest.mark.asyncio
async def test_racing_edits_and_replayed_deletion(memory, bank):
    ctx = RequestContext(api_key_id=str(uuid4()))
    rule = await memory.create_directive(bank, "Rule", "Original", tags=["first"], request_context=ctx)
    assert rule["revision"] == 1
    rule_id = rule["id"]
    responses = await asyncio.gather(
        *(
            memory.update_directive(bank, rule_id, expected_revision=1, content=v, request_context=ctx)
            for v in ("A", "B")
        ),
        return_exceptions=True,
    )
    (edited,) = [r for r in responses if isinstance(r, dict)]
    (conflict,) = [r for r in responses if isinstance(r, OperationValidationError)]
    assert conflict.status_code == 409
    assert edited["revision"] == 2
    assert await memory.delete_directive(bank, rule_id, expected_revision=2, request_context=ctx)
    with pytest.raises(OperationValidationError) as err:
        await memory.delete_directive(bank, rule_id, expected_revision=2, request_context=ctx)
    assert err.value.status_code == 409
    revived = await memory.revert_directive(bank, rule_id, revision=1, expected_revision=3, request_context=ctx)
    assert revived["snapshot"]["id"] == rule_id and revived["snapshot"]["content"] == "Original"
    assert revived["revision"] == 4 and not revived["deleted"] and revived["restored_from_revision"] == 1
    for head in (4, 5):
        restored = await memory.revert_directive(bank, rule_id, revision=3, expected_revision=head, request_context=ctx)
        assert restored["deleted"] and restored["revision"] == head + 1
    assert await memory.get_directive(bank, rule_id, request_context=ctx) is None
    history = await memory.list_directive_revisions(bank, rule_id, request_context=ctx)
    assert [h["operation"] for h in reversed(history)] == ["create", "update", "delete", "revert", "revert", "revert"]
    assert {h["actor_id"] for h in history} == {ctx.api_key_id}
    assert await memory.list_directive_revisions(f"other-{bank}", rule_id, request_context=ctx) is None
    assert await memory.list_directive_revisions(bank, rule_id, offset=6, request_context=ctx) == []


@pytest.mark.asyncio
async def test_bulk_writes_and_failed_writes_share_one_transaction(memory, bank):
    ctx = RequestContext()
    rule = await memory.create_directive(bank, "First", "One", request_context=ctx)
    backend = await memory._get_backend()
    async with backend.acquire() as conn:
        with pytest.raises(RuntimeError, match="abort import"):
            async with conn.transaction():
                await conn.execute(f"UPDATE {fq_table('directives')} SET content='rolled back' WHERE id=$1", rule["id"])
                assert (
                    await conn.fetchval(
                        f"SELECT MAX(revision) FROM {fq_table('directive_revisions')} WHERE directive_id=$1", rule["id"]
                    )
                    == 2
                )
                raise RuntimeError("abort import")
        # A successful out-of-band bulk write is journaled as well.
        await conn.execute(f"UPDATE {fq_table('directives')} SET content='bulk edit' WHERE id=$1", rule["id"])
    history = await memory.list_directive_revisions(bank, rule["id"], request_context=ctx)
    assert [(r["revision"], r["snapshot"]["content"]) for r in history] == [(2, "bulk edit"), (1, "One")]
    assert history[0]["actor_id"] is None


@pytest.mark.asyncio
async def test_history_rejects_scoped_reader_even_after_retag_or_delete(memory, bank, monkeypatch):
    from unittest.mock import AsyncMock

    ctx = RequestContext()
    rule = await memory.create_directive(bank, "Private", "Old secret", tags=["secret"], request_context=ctx)
    await memory.update_directive(bank, rule["id"], tags=[], request_context=ctx)
    await memory.delete_directive(bank, rule["id"], request_context=ctx)
    monkeypatch.setattr(memory, "_write_tag_scope", AsyncMock(return_value=["public"]))
    for action in (
        memory.list_directive_revisions(bank, rule["id"], request_context=ctx),
        memory.revert_directive(bank, rule["id"], revision=1, expected_revision=3, request_context=ctx),
    ):
        with pytest.raises(OperationValidationError) as err:
            await action
        assert err.value.status_code == 403


@pytest.mark.asyncio
async def test_published_history_can_only_be_erased_with_its_bank(memory, bank):
    import asyncpg

    ctx = RequestContext()
    rule = await memory.create_directive(bank, "Rule", "Protected", request_context=ctx)
    backend = await memory._get_backend()
    async with backend.acquire() as conn:
        for sql in ("UPDATE {table} SET operation='update'", "DELETE FROM {table}"):
            with pytest.raises(asyncpg.RaiseError, match="append-only"):
                await conn.execute(
                    sql.format(table=fq_table("directive_revisions")) + " WHERE directive_id=$1", rule["id"]
                )
    # Clearing memories leaves the profile's directives and their history intact.
    await memory.delete_bank(bank, delete_bank_profile=False, request_context=ctx)
    assert (await memory.get_directive(bank, rule["id"], request_context=ctx))["revision"] == 1
    assert len(await memory.list_directive_revisions(bank, rule["id"], request_context=ctx)) == 1
    await memory.delete_bank(bank, request_context=ctx)
    async with backend.acquire() as conn:
        assert (
            await conn.fetchval(f"SELECT count(*) FROM {fq_table('directive_revisions')} WHERE bank_id=$1", bank) == 0
        )


@pytest.mark.asyncio
async def test_revert_checks_history_and_the_write_it_would_perform(memory, bank, monkeypatch):
    from unittest.mock import AsyncMock

    from hindsight_api.extensions import (
        BankReadOperation,
        BankWriteOperation,
        OperationValidatorExtension,
        ValidationResult,
    )

    ctx = RequestContext()
    rule = await memory.create_directive(bank, "Rule", "A", request_context=ctx)
    rid = rule["id"]
    await memory.delete_directive(bank, rid, request_context=ctx)
    validator = AsyncMock(spec=OperationValidatorExtension)
    validator.resolve_tag_scope.return_value = None
    validator.resolve_write_tag_scope.return_value = None
    validator.validate_bank_read.side_effect = lambda c: (
        ValidationResult.reject("history disabled")
        if c.operation == BankReadOperation.LIST_DIRECTIVE_REVISIONS
        else ValidationResult.accept()
    )
    monkeypatch.setattr(memory, "_operation_validator", validator)
    assert await memory.get_directive(bank, rid, request_context=ctx) is None
    with pytest.raises(OperationValidationError, match="history disabled"):
        await memory.revert_directive(bank, rid, revision=1, expected_revision=2, request_context=ctx)
    validator.validate_bank_read.side_effect = None
    validator.validate_bank_read.return_value = ValidationResult.accept()
    validator.validate_bank_write.return_value = ValidationResult.reject("write disabled")
    with pytest.raises(OperationValidationError, match="write disabled"):
        await memory.revert_directive(bank, rid, revision=1, expected_revision=2, request_context=ctx)
    assert validator.validate_bank_write.await_args.args[0].operation == BankWriteOperation.CREATE_DIRECTIVE
    validator.validate_bank_write.return_value = ValidationResult.accept()
    await memory.revert_directive(bank, rid, revision=1, expected_revision=2, request_context=ctx)
    validator.validate_bank_write.return_value = ValidationResult.reject("write disabled")
    for target, operation in [(1, BankWriteOperation.UPDATE_DIRECTIVE), (2, BankWriteOperation.DELETE_DIRECTIVE)]:
        with pytest.raises(OperationValidationError, match="write disabled"):
            await memory.revert_directive(bank, rid, revision=target, expected_revision=3, request_context=ctx)
        assert validator.validate_bank_write.await_args.args[0].operation == operation
    assert (await memory.get_directive(bank, rid, request_context=ctx))["revision"] == 3
