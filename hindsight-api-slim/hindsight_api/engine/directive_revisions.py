"""PostgreSQL directive publication history. The database trigger records all write paths."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from uuid import UUID

from hindsight_api.extensions.operation_validator import BankWriteContext, BankWriteOperation, OperationValidationError

from .db_utils import acquire_with_retry
from .schema import _is_oracle, fq_table

if TYPE_CHECKING:
    from hindsight_api.models import RequestContext

    from .memory_engine import MemoryEngine


def columns() -> str:
    common = "id, bank_id, name, content, priority, is_active, tags, created_at, updated_at"
    return common if _is_oracle() else f"{common}, revision"


def require_history() -> None:
    if _is_oracle():
        raise OperationValidationError("Directive revision history requires PostgreSQL", status_code=501)


@asynccontextmanager
async def writing(backend, bank_id: str, directive_id: str | None, context: RequestContext, *, guarded=False):
    """Hold the bank lock even for a tombstone, for which no live row can be locked."""
    if guarded:
        require_history()
    async with acquire_with_retry(backend) as conn:
        if _is_oracle():
            yield conn
            return
        async with conn.transaction():
            if directive_id:
                await conn.fetchval(
                    f"SELECT bank_id FROM {fq_table('banks')} WHERE bank_id=$1 FOR NO KEY UPDATE", bank_id
                )
                # Bulk writers also use this row. Serialize scope and revision checks with their DML.
                await conn.fetchval(
                    f"SELECT revision FROM {fq_table('directives')} WHERE bank_id=$1 AND id=$2 FOR UPDATE",
                    bank_id,
                    directive_id,
                )
            await conn.fetchval("SELECT set_config('hindsight.directive_actor', $1, true)", context.api_key_id or "")
            yield conn


def positive_revision(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Revision must be a positive integer")


async def check_expected(conn, bank_id: str, directive_id: str, expected: int | None) -> int | None:
    if expected is None:
        return None
    positive_revision(expected)
    actual = await conn.fetchval(
        f"SELECT MAX(revision) FROM {fq_table('directive_revisions')} WHERE bank_id=$1 AND directive_id=$2",
        bank_id,
        directive_id,
    )
    if actual is not None and expected != actual:
        raise OperationValidationError(f"Expected directive revision {expected}, current revision is {actual}", 409)
    return actual


def _as_revision(row) -> dict:
    item = dict(row)
    item["directive_id"] = str(item["directive_id"])
    item["recorded_at"] = item["recorded_at"].isoformat()
    if isinstance(item["snapshot"], str):
        item["snapshot"] = json.loads(item["snapshot"])
    return item


async def list_history(backend, bank_id: str, directive_id: str, limit: int, offset: int) -> list[dict] | None:
    require_history()
    directive_id = str(UUID(directive_id))
    if limit < 1 or offset < 0:
        raise ValueError("Limit must be positive and offset must be nonnegative")
    async with acquire_with_retry(backend) as conn:
        rows = await conn.fetch(
            f"SELECT * FROM {fq_table('directive_revisions')} WHERE bank_id=$1 AND directive_id=$2 "
            "ORDER BY revision DESC LIMIT $3 OFFSET $4",
            bank_id,
            directive_id,
            limit,
            offset,
        )
        if rows:
            return [_as_revision(row) for row in rows]
        if offset and await conn.fetchval(
            f"SELECT 1 FROM {fq_table('directive_revisions')} WHERE bank_id=$1 AND directive_id=$2 LIMIT 1",
            bank_id,
            directive_id,
        ):
            return []
        return None


async def restore(
    engine: MemoryEngine,
    bank_id: str,
    directive_id: str,
    revision: int,
    expected_revision: int,
    request_context: RequestContext,
) -> dict | None:
    require_history()
    positive_revision(revision)
    positive_revision(expected_revision)
    directive_id = str(UUID(directive_id))
    backend = await engine._get_backend()
    rules = fq_table("directives")
    history = fq_table("directive_revisions")
    async with writing(backend, bank_id, directive_id, request_context) as conn:
        head = await check_expected(conn, bank_id, directive_id, expected_revision)
        if head is None:
            return None
        target = await conn.fetchrow(
            f"SELECT * FROM {history} WHERE bank_id=$1 AND directive_id=$2 AND revision=$3",
            bank_id,
            directive_id,
            revision,
        )
        if target is None:
            return None
        data = _as_revision(target)
        state = data["snapshot"]
        live = await conn.fetchval(f"SELECT 1 FROM {rules} WHERE bank_id=$1 AND id=$2", bank_id, directive_id)
        if data["deleted"]:
            operation = BankWriteOperation.DELETE_DIRECTIVE
        elif live:
            operation = BankWriteOperation.UPDATE_DIRECTIVE
        else:
            operation = BankWriteOperation.CREATE_DIRECTIVE
        if engine._operation_validator:
            ctx = BankWriteContext(bank_id=bank_id, operation=operation, request_context=request_context)
            await engine._validate_operation(engine._operation_validator.validate_bank_write(ctx))
        await conn.fetchval("SELECT set_config('hindsight.directive_revert', $1, true)", str(revision))
        if data["deleted"] and live:
            await conn.execute(f"DELETE FROM {rules} WHERE bank_id=$1 AND id=$2", bank_id, directive_id)
        elif data["deleted"]:
            # Reverting an already deleted directive still records the explicit restore.
            await conn.execute(
                f"""INSERT INTO {history}
                    (bank_id,directive_id,revision,operation,deleted,snapshot,actor_id,restored_from_revision)
                    SELECT bank_id,directive_id,$4,'revert',true,
                        snapshot || jsonb_build_object('revision',$4::bigint),$5,$3
                    FROM {history} WHERE bank_id=$1 AND directive_id=$2 AND revision=$3""",
                bank_id,
                directive_id,
                revision,
                head + 1,
                request_context.api_key_id,
            )
        else:
            # Only restore fields of a directive, never historical identity, revision, or updated_at.
            fields = [state[key] for key in ("name", "content", "priority", "is_active", "tags")]
            if live:
                await conn.execute(
                    f"""UPDATE {rules} SET name=$3,content=$4,priority=$5,is_active=$6,tags=$7,updated_at=now()
                        WHERE bank_id=$1 AND id=$2""",
                    bank_id,
                    directive_id,
                    *fields,
                )
            else:
                await conn.execute(
                    f"""INSERT INTO {rules} (bank_id,id,name,content,priority,is_active,tags,created_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::text::timestamptz)""",
                    bank_id,
                    directive_id,
                    *fields,
                    state["created_at"],
                )
        published = await conn.fetchrow(
            f"SELECT * FROM {history} WHERE bank_id=$1 AND directive_id=$2 AND revision=$3",
            bank_id,
            directive_id,
            head + 1,
        )
        return _as_revision(published)
