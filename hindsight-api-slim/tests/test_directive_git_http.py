"""Wire contracts shared by the native HTTP write routes and the Git administration endpoints."""

from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import UUID

import httpx
import pytest
import pytest_asyncio

from hindsight_api.api import create_app
from hindsight_api.engine.directive_git import service
from hindsight_api.engine.directive_git.models import Conflict, PublicationPending
from hindsight_api.extensions import OperationValidationError

BANK = "test-bank"
KEY = UUID("69a6a241-02e4-4874-93d5-e451ec83b74e")
BASE = "a" * 40
NEXT = "b" * 40


@pytest.fixture
def memory():
    engine = MagicMock()
    engine.audit_logger = None
    engine.refuse_knowledge_bank = AsyncMock()
    engine.resolve_bank_alias = AsyncMock(side_effect=lambda bank_id, **_: bank_id)
    engine._authenticate_tenant = AsyncMock()
    engine._authorize_bank_read = AsyncMock()
    return engine


@pytest_asyncio.fixture
async def client(memory):
    app = create_app(memory, initialize_memory=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "body"),
    [
        ("sync", {}),
        ("sync", {"revision": NEXT, "expected_revision": BASE, "apply": True}),
        ("adopt", {"directive_ids": [str(KEY)], "apply": True}),
    ],
)
async def test_bank_wide_permission_is_checked_before_inspecting_git(memory, client, monkeypatch, endpoint, body):
    memory._refuse_bank_wide_if_scoped = AsyncMock(side_effect=OperationValidationError("tag-scoped", status_code=403))
    resolve = Mock()
    monkeypatch.setattr(service, "manager_for_bank", resolve)
    result = await client.post(
        f"/v1/default/banks/{BANK}/directive-git/{endpoint}", json=body, headers={"Authorization": "Bearer test"}
    )
    assert result.status_code == 403 and result.json()["detail"] == "tag-scoped"
    assert memory._authenticate_tenant.await_args.args[0].api_key == "test"
    memory._authorize_bank_read.assert_awaited_once()
    resolve.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "body"),
    [
        ("sync", {}),
        ("adopt", {"directive_ids": [str(KEY)]}),
        ("detach", {"expected_revision": BASE}),
    ],
)
async def test_git_management_routes_refuse_knowledge_banks(memory, client, monkeypatch, endpoint, body):
    memory.refuse_knowledge_bank.side_effect = OperationValidationError("Knowledge bank", status_code=400)
    inspect = Mock()
    monkeypatch.setattr(service, "manager_for_bank", inspect)
    result = await client.post(f"/v1/default/banks/{BANK}/directive-git/{endpoint}", json=body)
    assert result.status_code == 400 and result.json()["detail"] == "Knowledge bank"
    memory.refuse_knowledge_bank.assert_awaited_once()
    inspect.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verb", "method", "url", "body"),
    [
        ("POST", "create_directive", "", {"name": "Style", "content": "Be brief."}),
        ("PATCH", "update_directive", f"/{KEY}", {"content": "Be brief."}),
        ("DELETE", "delete_directive", f"/{KEY}", None),
    ],
)
async def test_conditional_writes_report_both_conflict_outcomes(memory, client, verb, method, url, body):
    writer = AsyncMock(side_effect=[Conflict("stale revision"), PublicationPending(NEXT)])
    setattr(memory, method, writer)

    async def edit():
        return await client.request(
            verb,
            f"/v1/default/banks/{BANK}/directives{url}",
            json=body,
            headers={"X-Hindsight-Expected-Revision": BASE},
        )

    stale = await edit()
    assert stale.status_code == 409 and stale.json()["detail"] == "stale revision"
    pending = await edit()
    assert pending.status_code == 409
    assert pending.json()["detail"]["revision"] == NEXT
    assert pending.json()["detail"]["state"] == "pending"
    assert writer.await_args.kwargs["expected_revision"] == BASE


@pytest.mark.asyncio
async def test_bound_bank_deletion_reports_a_conflict(memory, client):
    memory.delete_bank = AsyncMock(side_effect=Conflict("Git-managed bank"))
    response = await client.delete(f"/v1/default/banks/{BANK}")
    assert response.status_code == 409 and response.json()["detail"] == "Git-managed bank"
