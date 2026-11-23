"""Directive revision transport contracts, with no provider or production database."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from hindsight_api.api import create_app
from hindsight_api.extensions import OperationValidationError


@pytest.fixture
def memory():
    engine = MagicMock()
    engine.audit_logger = None
    engine.refuse_knowledge_bank = AsyncMock()
    engine.resolve_bank_alias = AsyncMock(side_effect=lambda bank_id, **_: bank_id)
    return engine


@pytest_asyncio.fixture
async def client(memory):
    app = create_app(memory, initialize_memory=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test"}
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_read_and_restore_include_the_snapshot_and_authorized_context(memory, client):
    key = str(uuid4())
    url = f"/v1/default/banks/b/directives/{key}"
    revision = {
        "bank_id": "b",
        "directive_id": key,
        "revision": 1,
        "operation": "create",
        "deleted": False,
        "snapshot": {"id": key, "content": "Initial rule"},
        "actor_id": "example",
        "recorded_at": "2026-01-15T12:00:00+00:00",
        "restored_from_revision": None,
    }
    restored = {**revision, "operation": "revert", "revision": 3, "restored_from_revision": 1}
    memory.list_directive_revisions = AsyncMock(return_value=[revision])
    memory.revert_directive = AsyncMock(return_value=restored)
    listing = await client.get(f"{url}/revisions?limit=1&offset=1")
    assert listing.status_code == 200 and listing.json() == [revision]
    restore = await client.post(f"{url}/revert", json={"revision": 1, "expected_revision": 2})
    assert restore.status_code == 200 and restore.json() == restored
    for method, expected in (
        (memory.list_directive_revisions, {"limit": 1, "offset": 1}),
        (memory.revert_directive, {"revision": 1, "expected_revision": 2}),
    ):
        assert method.await_args.args == ("b", key)
        assert expected.items() <= method.await_args.kwargs.items()
        assert method.await_args.kwargs["request_context"].api_key == "test"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verb", "suffix", "method", "body"),
    [
        ("PATCH", "", "update_directive", {"content": "new", "expected_revision": 4}),
        ("DELETE", "?expected_revision=4", "delete_directive", None),
        ("POST", "/revert", "revert_directive", {"revision": 1, "expected_revision": 4}),
    ],
)
async def test_stale_edits_return_conflict(memory, client, verb, suffix, method, body):
    mutate = AsyncMock(side_effect=OperationValidationError("stale revision", status_code=409))
    setattr(memory, method, mutate)
    result = await client.request(verb, f"/v1/default/banks/b/directives/{uuid4()}{suffix}", json=body)
    assert result.status_code == 409 and result.json()["detail"] == "stale revision"
    assert mutate.await_args.kwargs["expected_revision"] == 4


@pytest.mark.asyncio
async def test_no_history_404_and_restore_requires_current_revision(memory, client):
    url = f"/v1/default/banks/b/directives/{uuid4()}"
    memory.list_directive_revisions = AsyncMock(return_value=None)
    memory.revert_directive = AsyncMock()
    assert (await client.get(f"{url}/revisions")).status_code == 404
    assert (await client.post(f"{url}/revert", json={"revision": 1})).status_code == 422
    memory.revert_directive.assert_not_awaited()
