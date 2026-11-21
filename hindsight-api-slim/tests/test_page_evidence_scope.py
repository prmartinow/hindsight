from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hindsight_api.engine.memory_engine import MemoryEngine, UnscopedRefreshError
from hindsight_api.engine.refresh_scope import validate_refresh_evidence_scope
from hindsight_api.extensions.operation_validator import OperationValidationError
from hindsight_api.models import RequestContext


@pytest.mark.parametrize(
    ("tags", "filters"),
    [
        (None, {}),
        (["repository"], {"tags_match": "any"}),
        (["repository"], {"tag_groups": []}),
        (None, {"tag_groups": [{"tags": ["repository"], "resolve": "fuzzy"}]}),
        (None, {"tag_groups": [{"or": [{"tags": ["repository"]}, {"not": {"tags": ["foreign"]}}]}]}),
    ],
)
def test_scoped_evidence_refuses_unbounded_filters(tags, filters):
    with pytest.raises(OperationValidationError, match="Scoped evidence requires") as failure:
        validate_refresh_evidence_scope(tags, {"require_scoped_evidence": True, **filters})
    assert failure.value.status_code == 400


def test_native_scoping_and_intentional_bank_summaries_can_coexist():
    validate_refresh_evidence_scope(None, None)
    validate_refresh_evidence_scope(None, {"require_scoped_evidence": False})
    validate_refresh_evidence_scope(["repository"], {"require_scoped_evidence": True})
    validate_refresh_evidence_scope(
        None,
        {
            "require_scoped_evidence": True,
            "tag_groups": [{"and": [{"tags": ["repository"]}, {"not": {"tags": ["draft"]}}]}],
        },
    )


@pytest.mark.asyncio
async def test_bank_default_requires_scope_before_page_creation_can_embed():
    engine = MemoryEngine.__new__(MemoryEngine)
    engine._authenticate_tenant = AsyncMock()
    engine._operation_validator = None
    engine._require_page_nodes_in_tag_scope = AsyncMock()
    engine._config_resolver = SimpleNamespace(
        get_bank_config=AsyncMock(return_value={"page_default_trigger": {"require_scoped_evidence": True}})
    )
    engine._tag_scope = AsyncMock(return_value=None)
    engine._write_tag_scope = AsyncMock(return_value=None)
    engine._mental_model_embedding_vector = AsyncMock()

    with pytest.raises(OperationValidationError, match="Scoped evidence requires"):
        await engine.create_page("bank", "Page", "Describe the repository.", "", request_context=RequestContext())

    engine._mental_model_embedding_vector.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_rechecks_saved_scope_and_fails_without_retry():
    engine = MemoryEngine.__new__(MemoryEngine)
    with pytest.raises(UnscopedRefreshError, match="Scoped evidence requires") as failure:
        await engine._execute_mental_model_refresh(
            "bank",
            {"id": "page", "trigger": {"require_scoped_evidence": True}},
            request_context=RequestContext(),
        )
    assert failure.value.retryable is False
    assert failure.value.reason == "unscoped_sources"


@pytest.mark.asyncio
async def test_page_edits_cannot_clear_the_last_required_scope():
    engine = MemoryEngine.__new__(MemoryEngine)
    engine._authenticate_tenant = AsyncMock()
    engine._operation_validator = None
    engine._get_backend = AsyncMock()
    engine._tag_scope = AsyncMock(return_value=None)
    engine._write_tag_scope = AsyncMock(return_value=None)
    connection = SimpleNamespace(
        transaction=nullcontext,
        fetchrow=AsyncMock(return_value={"tags": ["repository"], "trigger": {"require_scoped_evidence": True}}),
        execute=AsyncMock(),
    )

    with pytest.raises(OperationValidationError, match="Scoped evidence requires"):
        await engine.update_mental_model("bank", "page", tags=[], conn=connection, request_context=RequestContext())

    connection.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [False, True])
async def test_bad_saved_scope_is_rejected_before_enqueue(automatic):
    engine = MemoryEngine.__new__(MemoryEngine)
    engine._raise_if_mental_model_refresh_unavailable = Mock()
    engine._authenticate_tenant = AsyncMock()
    engine._automatic_refresh_paused = AsyncMock(return_value=False)
    engine._operation_validator = None
    engine.get_mental_model = AsyncMock(return_value={"trigger": {"require_scoped_evidence": True}})
    engine._write_tag_scope = AsyncMock(return_value=None)

    with pytest.raises(OperationValidationError, match="Scoped evidence requires"):
        await engine.submit_async_refresh_mental_model(
            "bank", "page", automatic=automatic, request_context=RequestContext()
        )
