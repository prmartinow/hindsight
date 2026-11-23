"""The native write routes honor the operator's bank binding after authentication."""

from unittest.mock import AsyncMock, Mock

import pytest

from hindsight_api.engine import memory_engine
from hindsight_api.engine.directive_git.models import Conflict
from hindsight_api.engine.memory_engine import MemoryEngine
from hindsight_api.extensions import OperationValidationError
from hindsight_api.models import RequestContext

BANK = "project"
DIRECTIVE_ID = "2f1f7c0c-010b-4be9-bd85-95aa367ea72b"


@pytest.fixture
def engine(monkeypatch):
    engine = object.__new__(MemoryEngine)
    engine._operation_validator = None
    for name, result in (("_authenticate_tenant", "public"), ("_write_tag_scope", None), ("_directive_writable", True)):
        monkeypatch.setattr(engine, name, AsyncMock(return_value=result))
    monkeypatch.setattr(engine, "_get_backend", AsyncMock(side_effect=AssertionError("unexpected database write")))
    return engine


@pytest.mark.asyncio
async def test_managed_mutations_reach_publisher_with_authorized_context(engine, monkeypatch):
    rc = RequestContext()
    manager = AsyncMock()
    manager.create.return_value = {"id": DIRECTIVE_ID}
    manager.update_if_managed.return_value = {"name": "renamed"}
    manager.delete_if_managed.return_value = True
    monkeypatch.setattr(memory_engine, "manager_for_bank", lambda bank_id: manager if bank_id == BANK else None)
    assert await engine.create_directive(BANK, "test", "content", request_context=rc) == {"id": DIRECTIVE_ID}
    assert await engine.update_directive(
        BANK,
        DIRECTIVE_ID,
        name="renamed",
        tags=[],
        priority=0,
        is_active=False,
        expected_revision="base",
        request_context=rc,
    ) == {"name": "renamed"}
    manager.update_if_managed.assert_awaited_once_with(
        engine,
        BANK,
        DIRECTIVE_ID,
        {"name": "renamed", "tags": [], "priority": 0, "is_active": False},
        request_context=rc,
        expected_revision="base",
    )
    assert await engine.delete_directive(BANK, DIRECTIVE_ID, request_context=rc)
    assert manager.create.await_args.kwargs["request_context"] is rc
    assert manager.delete_if_managed.await_args.kwargs["request_context"] is rc


@pytest.mark.asyncio
async def test_access_denial_precedes_git_lookup(engine, monkeypatch):
    resolve = Mock()
    monkeypatch.setattr(memory_engine, "manager_for_bank", resolve)
    engine._directive_writable.return_value = False
    assert await engine.update_directive(BANK, DIRECTIVE_ID, content="new", request_context=RequestContext()) is None
    assert await engine.delete_directive(BANK, DIRECTIVE_ID, request_context=RequestContext()) is False
    engine._authenticate_tenant.side_effect = OperationValidationError("unauthorized")
    with pytest.raises(OperationValidationError):
        await engine.create_directive(BANK, "name", "content", request_context=RequestContext())
    resolve.assert_not_called()


@pytest.mark.asyncio
async def test_unmanaged_directive_rejects_conditional_edit(engine, monkeypatch):
    monkeypatch.setattr(memory_engine, "manager_for_bank", lambda _: None)
    with pytest.raises(Conflict):
        await engine.update_directive(
            BANK, DIRECTIVE_ID, content="new", expected_revision="base", request_context=RequestContext()
        )
    engine._get_backend.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_restore_checks_binding_before_partial_bank_cleanup(engine, monkeypatch):
    monkeypatch.setattr(memory_engine, "manager_for_bank", lambda _: object())
    backend = Mock()
    monkeypatch.setattr(engine, "_get_backend", AsyncMock(return_value=backend))
    with pytest.raises(Conflict, match="restore target"):
        await engine.import_bank_async(b"", RequestContext(), target_bank_id=BANK, operation_id="retry")
    backend.acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update", "delete"])
async def test_enrollment_during_unmanaged_fallback_prevents_direct_write(engine, monkeypatch, action):
    from contextlib import asynccontextmanager

    manager = AsyncMock()
    manager.update_if_managed.return_value = manager.delete_if_managed.return_value = None
    monkeypatch.setattr(memory_engine, "manager_for_bank", lambda _: manager)
    monkeypatch.setattr(engine, "_get_backend", AsyncMock())
    conn = Mock()
    in_transaction = False

    @asynccontextmanager
    async def acquire(_backend):
        yield conn

    @asynccontextmanager
    async def transaction():
        nonlocal in_transaction
        in_transaction = True
        try:
            yield
        finally:
            in_transaction = False

    async def just_enrolled(connection, bank_id, directive_id, name=None):
        assert connection is conn and in_transaction and bank_id == BANK and directive_id == DIRECTIVE_ID
        raise Conflict("managed")

    conn.transaction = transaction
    monkeypatch.setattr(memory_engine, "acquire_with_retry", acquire)
    monkeypatch.setattr(memory_engine, "lock_unmanaged", just_enrolled)
    with pytest.raises(Conflict, match="managed"):
        if action == "update":
            await engine.update_directive(BANK, DIRECTIVE_ID, name="renamed", request_context=RequestContext())
        else:
            await engine.delete_directive(BANK, DIRECTIVE_ID, request_context=RequestContext())
    conn.execute.assert_not_called()
    conn.fetchrow.assert_not_called()
