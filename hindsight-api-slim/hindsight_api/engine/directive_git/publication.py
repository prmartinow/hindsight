"""Publish one immutable directive snapshot and its receipt in the same transaction."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from hindsight_api.extensions.operation_validator import (
    BankReadOperation,
    BankWriteContext,
    BankWriteOperation,
    OperationValidationError,
)
from hindsight_api.models import RequestContext

from ..db.base import DatabaseConnection
from ..db_utils import acquire_with_retry
from ..schema import fq_table
from .models import Conflict, Rule

if TYPE_CHECKING:
    from ..memory_engine import MemoryEngine


@dataclass
class _Plan:
    created: list[UUID] = field(default_factory=list)
    updated: list[UUID] = field(default_factory=list)
    deleted: list[UUID] = field(default_factory=list)
    enrolled: list[UUID] = field(default_factory=list)

    def result(self, revision: str | None) -> dict:
        return {
            "revision": revision,
            "created": list(map(str, self.created)),
            "updated": list(map(str, self.updated)),
            "deleted": list(map(str, self.deleted)),
            "enrolled": list(map(str, self.enrolled)),
        }


async def _lock_bank(conn: DatabaseConnection, bank_id: str) -> bool:
    # The persistent bank row also serializes the first publication, before a receipt exists.
    # NO KEY UPDATE leaves unrelated foreign-key inserts free to reference the bank.
    return (
        await conn.fetchval(f"SELECT bank_id FROM {fq_table('banks')} WHERE bank_id = $1 FOR NO KEY UPDATE", bank_id)
        is not None
    )


async def require_no_publication(conn: DatabaseConnection, bank_id: str) -> None:
    """Check after locking the bank: an absent process binding cannot undo durable enrollment."""
    if (
        await conn.fetchval(
            f"SELECT revision FROM {fq_table('directive_git_publications')} WHERE bank_id = $1", bank_id
        )
        is not None
    ):
        raise Conflict("This bank has a published Git rulebook. Restore its repository binding before writing.")


async def lock_unbound(conn: DatabaseConnection, bank_id: str) -> None:
    if await _lock_bank(conn, bank_id):
        await require_no_publication(conn, bank_id)


async def _receipt(conn: DatabaseConnection, bank_id: str) -> tuple[str | None, set[UUID]]:
    row = await conn.fetchrow(
        f"SELECT revision, managed_ids FROM {fq_table('directive_git_publications')} WHERE bank_id = $1", bank_id
    )
    if row is None:
        return None, set()
    return row["revision"], {UUID(item) for item in json.loads(row["managed_ids"])}


async def lock_unmanaged(
    conn: DatabaseConnection, bank_id: str, directive_id: str | None, name: str | None = None
) -> None:
    """Check an ordinary write inside its transaction, sharing the publication lock."""
    await _lock_bank(conn, bank_id)
    _, managed = await _receipt(conn, bank_id)
    if directive_id is not None and UUID(directive_id) in managed:
        raise Conflict("This directive is Git-managed; retry the edit against the published revision")
    if name is not None and managed:
        rows = await conn.fetch(
            f"SELECT id FROM {fq_table('directives')} WHERE bank_id = $1 AND name = $2", bank_id, name
        )
        if any(UUID(str(row["id"])) in managed for row in rows):
            raise Conflict("The requested name belongs to a Git-managed directive")


async def current_revision(engine: MemoryEngine, bank_id: str) -> str | None:
    """Internal routing lookup. The caller has already authorized access and bound the tenant."""
    async with acquire_with_retry(await engine._get_backend()) as conn:
        return await conn.fetchval(
            f"SELECT revision FROM {fq_table('directive_git_publications')} WHERE bank_id = $1", bank_id
        )


async def owned_revision(engine: MemoryEngine, bank_id: str, directive_id: UUID) -> str | None:
    """Route an already authorized edit without contacting Git for unmanaged directives."""
    async with acquire_with_retry(await engine._get_backend()) as conn:
        revision, owned = await _receipt(conn, bank_id)
        return revision if directive_id in owned else None


async def applied_revision(engine: MemoryEngine, bank_id: str, request_context: RequestContext) -> str | None:
    await engine._authenticate_tenant(request_context)
    await engine._authorize_bank_read(bank_id, BankReadOperation.LIST_DIRECTIVES, request_context)
    await engine._refuse_bank_wide_if_scoped(bank_id, request_context, "inspect the bank's directive revision")
    await engine._require_bank_exists(bank_id)
    return await current_revision(engine, bank_id)


async def _plan(conn: DatabaseConnection, bank_id: str, previous: dict[UUID, Rule], desired: dict[UUID, Rule]) -> _Plan:
    ids = sorted(previous.keys() | desired.keys())
    names = [rule.name for rule in desired.values()]
    if len(names) != len(set(names)):
        raise Conflict("A directive name appears more than once in the proposed snapshot")
    plan = _Plan()
    if not ids:
        return plan

    fields = "id, name, content, priority, is_active, tags"
    rows = await conn.fetch(
        f"SELECT {fields} FROM {fq_table('directives')} WHERE bank_id = $1 "
        "AND (id = ANY($2::uuid[]) OR name = ANY($3::text[]))",
        bank_id,
        ids,
        names,
    )
    live = {}
    for row in rows:
        key = UUID(str(row["id"]))
        if key not in desired and key not in previous and row["name"] in names:
            raise Conflict("A proposed name already belongs to an unmanaged directive")
        live[key] = Rule(
            id=key,
            name=row["name"],
            content=row["content"],
            priority=row["priority"],
            is_active=bool(row["is_active"]),
            tags=row["tags"] or [],
        )
    for key in ids:
        old, new, stored = previous.get(key), desired.get(key), live.get(key)
        if old is not None:
            if old != stored:
                raise Conflict(f"Published directive {key} differs from its last applied revision")
            if new is None:
                plan.deleted.append(key)
            elif old != new:
                plan.updated.append(key)
        elif stored is None:
            plan.created.append(key)
        elif stored == new:
            plan.enrolled.append(key)
        else:
            raise Conflict(f"Existing directive {key} differs from its proposed enrollment")
    if plan.created and await conn.fetchval(
        f"SELECT COUNT(*) FROM {fq_table('directives')} WHERE id = ANY($1::uuid[]) AND bank_id <> $2",
        plan.created,
        bank_id,
    ):
        raise Conflict("A proposed directive ID is unavailable")
    return plan


async def _write(conn: DatabaseConnection, bank_id: str, plan: _Plan, desired: dict[UUID, Rule]) -> None:
    table = fq_table("directives")
    if plan.deleted:
        await conn.execute(f"DELETE FROM {table} WHERE bank_id = $1 AND id = ANY($2::uuid[])", bank_id, plan.deleted)
    for keys, sql in (
        (
            plan.created,
            f"INSERT INTO {table} (bank_id,id,name,content,priority,is_active,tags) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        ),
        (
            plan.updated,
            f"UPDATE {table} SET name=$3,content=$4,priority=$5,is_active=$6,tags=$7,updated_at=now() "
            "WHERE bank_id=$1 AND id=$2",
        ),
    ):
        for key in keys:
            r = desired[key]
            await conn.execute(sql, bank_id, key, r.name, r.content, r.priority, r.is_active, r.tags)


async def _compare(
    conn: DatabaseConnection,
    bank_id: str,
    previous_sha: str | None,
    revision: str | None,
    previous: dict[UUID, Rule],
    desired: dict[UUID, Rule],
) -> tuple[str | None, _Plan]:
    stored, managed = await _receipt(conn, bank_id)
    already_applied = revision is not None and stored == revision
    baseline = desired if already_applied else previous
    if (not already_applied and stored != previous_sha) or managed != baseline.keys():
        raise Conflict("The published revision changed; reload the current revision")
    return stored, await _plan(conn, bank_id, baseline, desired)


def _operations(plan: _Plan, already_applied: bool) -> set[BankWriteOperation]:
    required = {
        operation
        for changed, operation in (
            (plan.created, BankWriteOperation.CREATE_DIRECTIVE),
            (plan.updated or plan.enrolled, BankWriteOperation.UPDATE_DIRECTIVE),
            (plan.deleted, BankWriteOperation.DELETE_DIRECTIVE),
        )
        if changed
    }
    # Even an empty Git commit advances the receipt and therefore requires write access.
    if not required and not already_applied:
        required.add(BankWriteOperation.UPDATE_DIRECTIVE)
    return required


async def _authorize_single(
    engine: MemoryEngine,
    bank_id: str,
    key: UUID,
    previous: dict[UUID, Rule],
    desired: dict[UUID, Rule],
    request_context: RequestContext,
) -> BankWriteOperation:
    if any(previous.get(i) != desired.get(i) for i in previous.keys() | desired.keys() if i != key):
        raise Conflict("An individual directive edit cannot modify the rest of the rulebook")
    old, new = previous.get(key), desired.get(key)
    if old is None and new is None:
        raise Conflict("The requested directive does not exist")
    if old and not engine._directive_visible(old.tags, await engine._tag_scope(bank_id, request_context)):
        raise OperationValidationError("Directive not found", status_code=404)
    write_scope = await engine._write_tag_scope(bank_id, request_context)
    for rule in (old, new):
        if rule:
            engine._refuse_unwritable(rule.tags, write_scope, "a directive")
    if old is None:
        return BankWriteOperation.CREATE_DIRECTIVE
    return BankWriteOperation.UPDATE_DIRECTIVE if new else BankWriteOperation.DELETE_DIRECTIVE


async def publish(
    engine: MemoryEngine,
    bank_id: str,
    previous_sha: str | None,
    revision: str | None,
    previous: dict[UUID, Rule],
    desired: dict[UUID, Rule],
    *,
    dry_run: bool,
    request_context: RequestContext,
    return_id: UUID | None = None,
    enroll_only: bool = False,
    single_id: UUID | None = None,
) -> dict:
    """Compare the verified Git snapshots with the published bank. A dry run never writes."""
    await engine._authenticate_tenant(request_context)
    single_operation = None
    if single_id is None:
        await engine._authorize_bank_read(bank_id, BankReadOperation.LIST_DIRECTIVES, request_context)
        await engine._refuse_bank_wide_if_scoped(bank_id, request_context, "publish the bank's directive revision")
    else:
        if enroll_only or return_id not in (None, single_id):
            raise ValueError("An individual directive edit may only return its own record")
        single_operation = await _authorize_single(engine, bank_id, single_id, previous, desired, request_context)
    if return_id is not None and return_id not in desired:
        raise ValueError("The returned directive must belong to the requested revision")
    if not dry_run and revision is None:
        raise ValueError("A committed revision is required to publish directives")
    backend = await engine._get_backend()
    approved = None
    if engine._operation_validator:
        # Hooks can acquire another connection and inspect the bank. Run them outside the
        # lock and without occupying a pool slot, then recheck their scope under the lock.
        if single_operation is not None:
            approved = {single_operation}
        else:
            async with acquire_with_retry(backend) as conn:
                stored, plan = await _compare(conn, bank_id, previous_sha, revision, previous, desired)
            approved = _operations(plan, revision is not None and stored == revision)
        for operation in sorted(approved):
            ctx = BankWriteContext(bank_id=bank_id, operation=operation, request_context=request_context)
            await engine._validate_operation(engine._operation_validator.validate_bank_write(ctx))
    async with acquire_with_retry(backend) as conn, conn.transaction():
        if not await _lock_bank(conn, bank_id):
            raise OperationValidationError(f"Bank '{bank_id}' not found", status_code=404)
        try:
            stored, plan = await _compare(conn, bank_id, previous_sha, revision, previous, desired)
        except Conflict:
            if single_id is not None:
                # A scoped change can encounter drift in a rule the caller cannot read.
                raise Conflict("The published rulebook conflicts with this directive change") from None
            raise
        if single_id is not None and plan.enrolled:
            raise Conflict("An individual directive edit cannot enroll an existing directive")
        already_applied = revision is not None and stored == revision
        if enroll_only and (plan.created or plan.deleted or plan.updated):
            raise Conflict("Every directive selected for enrollment must already exist with matching metadata")
        if approved is not None and _operations(plan, already_applied) - approved:
            raise Conflict("Directive ownership changed during authorization; retry the publication")
        if dry_run:
            return plan.result(stored)
        if not already_applied:
            await _write(conn, bank_id, plan, desired)
            await _save_receipt(conn, bank_id, revision, desired, first=stored is None)
        result = plan.result(revision)
        if return_id is not None:
            row = await conn.fetchrow(
                f"SELECT * FROM {fq_table('directives')} WHERE bank_id = $1 AND id = $2", bank_id, return_id
            )
            result["directive"] = engine._row_to_directive(row)
        return result


async def _save_receipt(
    conn: DatabaseConnection, bank_id: str, revision: str | None, desired: dict[UUID, Rule], *, first: bool
) -> None:
    receipt = fq_table("directive_git_publications")
    inventory = json.dumps([str(key) for key in sorted(desired)])
    if first:
        await conn.execute(
            f"INSERT INTO {receipt} (bank_id, revision, managed_ids) VALUES ($1,$2,$3)", bank_id, revision, inventory
        )
    else:
        await conn.execute(
            f"UPDATE {receipt} SET revision=$2, managed_ids=$3 WHERE bank_id=$1", bank_id, revision, inventory
        )


async def detach(engine: MemoryEngine, bank_id: str, expected_revision: str, request_context: RequestContext) -> dict:
    """Relinquish the exact published revision; its active directive rows stay in the bank."""
    await engine._authenticate_tenant(request_context)
    await engine._refuse_bank_wide_if_scoped(bank_id, request_context, "detach the bank's Git rulebook")
    if engine._operation_validator:
        ctx = BankWriteContext(
            bank_id=bank_id, operation=BankWriteOperation.UPDATE_BANK_CONFIG, request_context=request_context
        )
        await engine._validate_operation(engine._operation_validator.validate_bank_write(ctx))
    async with acquire_with_retry(await engine._get_backend()) as conn, conn.transaction():
        if not await _lock_bank(conn, bank_id):
            raise OperationValidationError(f"Bank '{bank_id}' not found", status_code=404)
        stored, _ = await _receipt(conn, bank_id)
        if not stored or expected_revision != stored:
            raise Conflict("Detach requires the exact published directive revision")
        await conn.execute(f"DELETE FROM {fq_table('directive_git_publications')} WHERE bank_id = $1", bank_id)
        return {"revision": None, "detached_revision": stored}
