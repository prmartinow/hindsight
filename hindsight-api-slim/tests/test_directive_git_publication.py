"""Git directive publication against an isolated PostgreSQL test bank."""

import asyncio
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from hindsight_api.engine.cross_encoder import RRFPassthroughCrossEncoder
from hindsight_api.engine.directive_git import publication
from hindsight_api.engine.directive_git.models import Conflict, Rule
from hindsight_api.engine.embeddings import Embeddings
from hindsight_api.engine.schema import fq_table
from hindsight_api.engine.search.tags import TagGroupLeaf
from hindsight_api.extensions.operation_validator import (
    BankWriteOperation,
    OperationValidationError,
    OperationValidatorExtension,
    ValidationResult,
)


@pytest.fixture
def embeddings():
    return Mock(spec=Embeddings, dimension=384, provider_name="stub")


@pytest.fixture
def cross_encoder():
    return RRFPassthroughCrossEncoder()


@pytest.fixture
async def git_bank(memory, request_context):
    bank = "git-directives-" + uuid4().hex
    await memory.ensure_bank_profile(bank_id=bank, request_context=request_context)
    yield bank
    async with (await memory._get_backend()).acquire() as conn:
        await conn.execute(f"DELETE FROM {fq_table('banks')} WHERE bank_id = $1", bank)


async def test_publish_enrollment_rename_deletion_and_drift(memory, git_bank, request_context):
    row = await memory.create_directive(
        git_bank, "Existing", "Approved", priority=5, tags=["docs"], request_context=request_context
    )
    original = Rule(**{field: row[field] for field in Rule.model_fields})
    added = Rule(id=uuid4(), name="Other", content="Another rule")
    snapshot = {r.id: r for r in (original, added)}

    async def publish(base, rev, before, after, dry=False, **kwargs):
        return await publication.publish(
            memory, git_bank, base, rev, before, after, dry_run=dry, request_context=request_context, **kwargs
        )

    preview = await publish(None, None, {}, snapshot, dry=True)
    assert preview["enrolled"] == [str(original.id)]
    assert preview["created"] == [str(added.id)]
    assert await publication.applied_revision(memory, git_bank, request_context) is None
    first = await publish(None, "a" * 40, {}, snapshot, return_id=added.id)
    assert first["directive"]["id"] == str(added.id)
    assert (await publish(None, "a" * 40, {}, snapshot))["created"] == []

    renamed = original.model_copy(update={"name": "Renamed", "tags": []})
    next_snapshot = {original.id: renamed}
    second = await publish("a" * 40, "b" * 40, snapshot, next_snapshot, return_id=original.id)
    assert second["updated"] == [str(original.id)]
    assert second["deleted"] == [str(added.id)]
    assert second["directive"]["tags"] == []
    assert second["directive"]["created_at"] == row["created_at"]
    backend = await memory._get_backend()
    async with backend.acquire() as conn:
        async with conn.transaction():
            with pytest.raises(Conflict, match="Git-managed"):
                await publication.lock_unmanaged(conn, git_bank, str(original.id))
        await conn.execute(
            f"UPDATE {fq_table('directives')} SET content='drift' WHERE bank_id=$1 AND id=$2", git_bank, original.id
        )
    with pytest.raises(Conflict, match="differs from its last applied revision"):
        await publish("b" * 40, "b" * 40, next_snapshot, next_snapshot, dry=True)


async def test_receipt_failure_rolls_back_rows_and_concurrent_revisions_do_not_overwrite(
    memory, git_bank, request_context, monkeypatch
):
    first = Rule(id=uuid4(), name="A", content="original")
    rules = {first.id: first}

    async def initial(revision):
        return await publication.publish(
            memory, git_bank, None, revision, {}, rules, dry_run=False, request_context=request_context
        )

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_save_receipt", AsyncMock(side_effect=RuntimeError("receipt failure")))
        with pytest.raises(RuntimeError, match="receipt failure"):
            await initial("a" * 40)
    assert await memory.get_directive(git_bank, str(first.id), request_context=request_context) is None
    results = await asyncio.gather(initial("b" * 40), initial("c" * 40), return_exceptions=True)
    assert sum(isinstance(result, Conflict) for result in results) == 1
    [winner] = [result for result in results if isinstance(result, dict)]
    assert await publication.applied_revision(memory, git_bank, request_context) == winner["revision"]


async def test_name_and_id_conflicts_cannot_claim_other_directives(memory, git_bank, request_context):
    existing = await memory.create_directive(git_bank, "Collision", "native", request_context=request_context)
    for rule in (
        Rule(id=uuid4(), name="Collision", content="override"),
        Rule(id=UUID(existing["id"]), name="Renamed", content="override"),
    ):
        with pytest.raises(Conflict):
            await publication.publish(
                memory, git_bank, None, None, {}, {rule.id: rule}, dry_run=True, request_context=request_context
            )
    assert (await memory.get_directive(git_bank, existing["id"], request_context=request_context))[
        "content"
    ] == "native"


async def test_empty_revision_requires_write_permission(memory, git_bank, request_context, monkeypatch):
    validator = AsyncMock(spec=OperationValidatorExtension)
    validator.resolve_tag_scope.return_value = None
    validator.resolve_write_tag_scope.return_value = None
    validator.validate_bank_read.return_value = ValidationResult.accept()
    validator.validate_bank_write.return_value = ValidationResult.reject("Read only")
    monkeypatch.setattr(memory, "_operation_validator", validator)
    with pytest.raises(OperationValidationError, match="Read only"):
        await publication.publish(
            memory, git_bank, None, "a" * 40, {}, {}, dry_run=False, request_context=request_context
        )
    assert validator.validate_bank_write.await_args.args[0].operation == BankWriteOperation.UPDATE_DIRECTIVE
    assert await publication.current_revision(memory, git_bank) is None


async def test_enrollment_does_not_recreate_a_concurrently_deleted_row(memory, git_bank, request_context):
    row = await memory.create_directive(git_bank, "Enroll", "Existing rule", request_context=request_context)
    rule = Rule(**{field: row[field] for field in Rule.model_fields})
    desired = {rule.id: rule}
    plan = await publication.publish(
        memory, git_bank, None, None, {}, desired, dry_run=True, request_context=request_context, enroll_only=True
    )
    assert plan["enrolled"] == [str(rule.id)]
    assert await memory.delete_directive(git_bank, row["id"], request_context=request_context)
    with pytest.raises(Conflict, match="selected for enrollment"):
        await publication.publish(
            memory,
            git_bank,
            None,
            "f" * 40,
            {},
            desired,
            dry_run=False,
            request_context=request_context,
            enroll_only=True,
        )
    assert await publication.current_revision(memory, git_bank) is None
    assert await memory.get_directive(git_bank, row["id"], request_context=request_context) is None


async def test_tag_scoped_writer_cannot_publish_bank_snapshot(memory, git_bank, request_context, monkeypatch):
    validator = AsyncMock(spec=OperationValidatorExtension)
    validator.resolve_tag_scope.return_value = None
    validator.resolve_write_tag_scope.return_value = ["project:one"]
    validator.validate_bank_read.return_value = ValidationResult.accept()
    validator.validate_bank_write.return_value = ValidationResult.accept()
    monkeypatch.setattr(memory, "_operation_validator", validator)
    with pytest.raises(OperationValidationError, match="tag-scoped"):
        await publication.publish(
            memory, git_bank, None, "e" * 40, {}, {}, dry_run=False, request_context=request_context
        )
    assert await publication.current_revision(memory, git_bank) is None


async def test_missing_binding_protects_published_bank_including_enrollment_race(
    memory, git_bank, request_context, monkeypatch
):
    # This engine has no repository mapping. Native CRUD works until a different
    # writer enrolls the bank, even when an ordinary write is already in flight.
    row = await memory.create_directive(git_bank, "Before", "initial", request_context=request_context)
    changed = await memory.update_directive(git_bank, row["id"], name="After", request_context=request_context)
    extra = await memory.create_directive(git_bank, "Temporary", "body", request_context=request_context)
    assert await memory.delete_directive(git_bank, extra["id"], request_context=request_context)
    rule = Rule(**{field: changed[field] for field in Rule.model_fields})
    locked, finish = asyncio.Event(), asyncio.Event()
    original = publication._save_receipt

    async def pause(*args, **kwargs):
        locked.set()
        await finish.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(publication, "_save_receipt", pause)
    publishing = asyncio.create_task(
        publication.publish(
            memory, git_bank, None, "f" * 40, {}, {rule.id: rule}, dry_run=False, request_context=request_context
        )
    )
    await asyncio.wait_for(locked.wait(), timeout=5)
    updating = asyncio.create_task(
        memory.update_directive(git_bank, row["id"], content="stale", request_context=request_context)
    )
    finish.set()
    await publishing
    with pytest.raises(Conflict, match="published Git rulebook"):
        await updating
    for attempt in (
        lambda: memory.create_directive(git_bank, "Untracked", "other", request_context=request_context),
        lambda: memory.delete_directive(git_bank, row["id"], request_context=request_context),
        lambda: memory.delete_bank(git_bank, request_context=request_context),
    ):
        with pytest.raises(Conflict, match="published Git rulebook"):
            await attempt()
    assert (await memory.get_directive(git_bank, row["id"], request_context=request_context))["content"] == "initial"


async def test_detach_requires_config_write_and_exact_sha_and_preserves_rows(
    memory, git_bank, request_context, monkeypatch
):
    rule = Rule(id=uuid4(), name="Published", content="Preserve me")
    sha = "a" * 40
    await publication.publish(
        memory, git_bank, None, sha, {}, {rule.id: rule}, dry_run=False, request_context=request_context
    )
    validator = AsyncMock(spec=OperationValidatorExtension)
    validator.resolve_tag_scope.return_value = None
    validator.resolve_write_tag_scope.return_value = None
    validator.validate_bank_write.return_value = ValidationResult.reject("No admin rights")
    with monkeypatch.context() as patch:
        patch.setattr(memory, "_operation_validator", validator)
        with pytest.raises(OperationValidationError, match="No admin rights"):
            await publication.detach(memory, git_bank, sha, request_context)
        assert validator.validate_bank_write.await_args.args[0].operation == BankWriteOperation.UPDATE_BANK_CONFIG
    with pytest.raises(Conflict, match="exact published"):
        await publication.detach(memory, git_bank, "b" * 40, request_context)
    assert await publication.current_revision(memory, git_bank) == sha
    assert (await publication.detach(memory, git_bank, sha, request_context))["detached_revision"] == sha
    assert await publication.current_revision(memory, git_bank) is None
    assert (await memory.get_directive(git_bank, str(rule.id), request_context=request_context))[
        "content"
    ] == rule.content
    assert await memory.delete_directive(git_bank, str(rule.id), request_context=request_context)


async def test_single_directive_scope_is_preserved_without_reading_or_editing_other_rules(
    memory, git_bank, request_context, monkeypatch
):
    mine = Rule(id=uuid4(), name="Scoped", content="initial", tags=["user:one"])
    private = Rule(id=uuid4(), name="Private", content="hidden", tags=["user:other"])
    base = {rule.id: rule for rule in (mine, private)}
    await publication.publish(
        memory, git_bank, None, "a" * 40, {}, base, dry_run=False, request_context=request_context
    )
    validator = AsyncMock(spec=OperationValidatorExtension)
    validator.resolve_tag_scope.return_value = [TagGroupLeaf(tags=["user:one"], match="any_strict")]
    validator.resolve_write_tag_scope.return_value = ["user:one"]
    validator.validate_bank_read.return_value = ValidationResult.reject("Whole-bank listing disabled")
    validator.validate_bank_write.return_value = ValidationResult.accept()
    monkeypatch.setattr(memory, "_operation_validator", validator)

    async def edit(key, desired, *, dry=True):
        return await publication.publish(
            memory,
            git_bank,
            "a" * 40,
            "b" * 40,
            base,
            desired,
            single_id=key,
            dry_run=dry,
            request_context=request_context,
        )

    new = {**base, mine.id: mine.model_copy(update={"content": "edited"})}
    assert (await edit(mine.id, new))["updated"] == [str(mine.id)]
    with pytest.raises(Conflict, match="rest of the rulebook"):
        await edit(private.id, new)
    with pytest.raises(OperationValidationError, match="not found"):
        await edit(private.id, {mine.id: mine})
    with pytest.raises(OperationValidationError, match="can't write"):
        await edit(mine.id, {**base, mine.id: mine.model_copy(update={"tags": []})})
    async with (await memory._get_backend()).acquire() as conn:
        await conn.execute(
            f"UPDATE {fq_table('directives')} SET content='drift' WHERE bank_id=$1 AND id=$2", git_bank, private.id
        )
    with pytest.raises(Conflict, match="rulebook conflicts") as caught:
        await edit(mine.id, new, dry=False)
    assert str(private.id) not in str(caught.value)
    validator.validate_bank_read.assert_not_called()
