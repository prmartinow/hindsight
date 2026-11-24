"""Scope regressions run offline: no database, network, or LLM calls."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from hindsight_api.engine.memory_engine import (
    MemoryEngine,
    _MentalModelScopeWatermark,
)


def _engine(has_sources):
    engine = object.__new__(MemoryEngine)
    now = datetime.now(timezone.utc)
    engine._mental_model_refresh_cutoff = AsyncMock(return_value=now)
    engine._mental_model_scope_watermark = AsyncMock(
        return_value=_MentalModelScopeWatermark(
            newest_in_scope=now if has_sources else None, watermark=now if has_sources else None
        )
    )
    engine._siblings_are_a_source = AsyncMock(side_effect=AssertionError("sibling contamination"))
    engine.reflect_async = AsyncMock(side_effect=RuntimeError("retrieval boundary reached"))
    return engine


@pytest.mark.parametrize(
    ("tags", "tags_match", "exclude_mental_models"),
    [
        (["repo:one", "topic:two"], "all_strict", True),
        ([], "any", False),
    ],
    ids=["tagged-default", "bank-wide"],
)
@pytest.mark.asyncio
async def test_same_scope_reaches_preflight_and_retrieval(tags, tags_match, exclude_mental_models):
    engine = _engine(True)
    model = {
        "id": "mm-test",
        "tags": tags,
        "trigger": {"mode": "full", "exclude_mental_models": exclude_mental_models},
        "content": "accepted body",
        "source_query": "Project status",
    }
    with pytest.raises(RuntimeError, match="retrieval boundary reached"):
        await engine._execute_mental_model_refresh("test", model, request_context=None)
    scope = engine._mental_model_scope_watermark.await_args.args[2]
    reflect_options = engine.reflect_async.await_args.kwargs
    assert scope.tags == reflect_options["tags"] == tags
    assert scope.tags_match == reflect_options["tags_match"] == tags_match
    assert reflect_options["exclude_mental_models"] == exclude_mental_models


@pytest.mark.asyncio
async def test_no_scoped_sources_preserves_existing_body_without_siblings():
    engine = _engine(False)
    model = {
        "id": "mm-test",
        "tags": ["repo:one", "topic:two"],
        "trigger": {"mode": "full", "exclude_mental_models": True},
        "content": "accepted body",
        "source_query": "Project status",
    }
    run = await engine._execute_mental_model_refresh("test", model, request_context=None)
    assert run.outcome == "content_preserved_no_new_facts"
    assert model["content"] == "accepted body"
    engine.reflect_async.assert_not_called()
    engine._siblings_are_a_source.assert_not_called()
