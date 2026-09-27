"""Scope regressions run offline: no database, network, or LLM calls."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from hindsight_api.engine.memory_engine import (
    MemoryEngine,
    MentalModelRefreshError,
    _MentalModelScopeWatermark,
    _resolve_refresh_tag_filtering,
)
from hindsight_api.engine.search.tags import build_tag_groups_where_clause, build_tags_where_clause


@pytest.mark.parametrize(
    "tags,trigger",
    [
        ([], {}),
        (None, {}),
        ([""], {}),
        (["repo:hermes"], {"tag_groups": []}),
        ([], {"tag_groups": [{"not": {"tags": ["repo:wispr"]}}]}),
        ([], {"tag_groups": [{"or": [{"tags": ["repo:hermes"]}, {"not": {"tags": ["repo:wispr"]}}]}]}),
        ([], {"tag_groups": [{"tags": ["repo:hermes"], "resolve": "fuzzy"}]}),
        ([], {"tag_groups": [{"tags": [], "match": "exact"}]}),
    ],
)
@pytest.mark.asyncio
async def test_unbounded_refresh_never_reads_or_synthesizes(tags, trigger):
    engine = object.__new__(MemoryEngine)
    engine._get_backend = AsyncMock(side_effect=AssertionError("database access"))
    engine.reflect_async = AsyncMock(side_effect=AssertionError("LLM access"))
    model = {
        "id": "mm-test",
        "tags": tags,
        "trigger": {"mode": "delta", **trigger},
        "content": "previous accepted body",
        "source_query": "Hermes vault",
    }
    with pytest.raises(MentalModelRefreshError) as raised:
        await engine._execute_mental_model_refresh("test", model, request_context=None)
    assert raised.value.reason == "unscoped_sources"
    assert model["content"] == "previous accepted body"
    engine._get_backend.assert_not_called()
    engine.reflect_async.assert_not_called()


@pytest.mark.parametrize(
    "match,expected", [("any", "any_strict"), ("all", "all_strict"), ("all_strict", "all_strict"), ("exact", "exact")]
)
def test_flat_source_scope_never_admits_untagged(match, expected):
    scope = _resolve_refresh_tag_filtering(["repo:hermes", "topic:vault"], {"tags_match": match})
    assert scope.is_scoped
    assert scope.tags_match == expected
    clause = build_tags_where_clause(scope.tags, 1, match=scope.tags_match)
    assert "IS NULL OR" not in clause.sql
    assert clause.params == [["repo:hermes", "topic:vault"]]


def test_positive_and_negative_groups_are_strict_without_mutating_input():
    raw = {
        "tag_groups": [
            {
                "and": [
                    {"tags": ["repo:hermes"], "match": "all"},
                    {"not": {"tags": ["topic:audio"], "match": "any"}},
                ]
            }
        ]
    }
    scope = _resolve_refresh_tag_filtering([], raw)
    assert scope.is_scoped
    clause = build_tag_groups_where_clause(scope.tag_groups, 1)
    assert "IS NULL OR" not in clause.sql
    assert raw["tag_groups"][0]["and"][0]["match"] == "all"


def _engine(has_sources):
    engine = object.__new__(MemoryEngine)
    now = datetime.now(timezone.utc)
    engine._mental_model_refresh_cutoff = AsyncMock(return_value=now)
    engine._mental_model_scope_watermark = AsyncMock(
        return_value=_MentalModelScopeWatermark(
            newest_in_scope=now if has_sources else None, watermark=now if has_sources else None
        )
    )
    engine._build_mm_scope_filter = Mock(return_value=object())
    engine._bank_has_readable_document = AsyncMock(side_effect=AssertionError("sibling contamination"))
    engine.reflect_async = AsyncMock(side_effect=RuntimeError("retrieval boundary reached"))
    return engine


@pytest.mark.asyncio
async def test_same_strict_scope_reaches_preflight_and_retrieval():
    engine = _engine(True)
    model = {
        "id": "mm-test",
        "tags": ["repo:hermes", "topic:vault"],
        "trigger": {"mode": "full", "tags_match": "all", "exclude_mental_models": False},
        "content": "accepted body",
        "source_query": "Hermes vault",
    }
    with pytest.raises(RuntimeError, match="retrieval boundary reached"):
        await engine._execute_mental_model_refresh("test", model, request_context=None)
    scope = engine._build_mm_scope_filter.call_args.args[1]
    kw = engine.reflect_async.call_args.kwargs
    assert scope.tags == kw["tags"] == ["repo:hermes", "topic:vault"]
    assert scope.tags_match == kw["tags_match"] == "all_strict"
    assert kw["exclude_mental_models"] is True


@pytest.mark.asyncio
async def test_no_scoped_sources_preserves_existing_body_without_siblings():
    engine = _engine(False)
    model = {
        "id": "mm-test",
        "tags": ["repo:hermes", "topic:vault"],
        "trigger": {"mode": "full"},
        "content": "accepted body",
        "source_query": "Hermes vault",
    }
    run = await engine._execute_mental_model_refresh("test", model, request_context=None)
    assert run.outcome == "content_preserved_no_new_facts"
    assert model["content"] == "accepted body"
    engine.reflect_async.assert_not_called()
    engine._bank_has_readable_document.assert_not_called()
