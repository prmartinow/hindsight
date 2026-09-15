"""Content existence is independent of successful async task completion.

Unit tests use a fake connection only: no database, workers, or LLM calls.
"""

import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from hindsight_api.engine import memory_engine
from hindsight_api.engine.memory_engine import MemoryEngine, _operation_details
from hindsight_api.models import RequestContext


@pytest.fixture
def content_engine(monkeypatch):
    engine = object.__new__(MemoryEngine)
    engine._authenticate_tenant = AsyncMock()
    engine._operation_validator = None
    engine._get_backend = AsyncMock()
    conn = Mock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.parse_json = lambda value: value

    @asynccontextmanager
    async def acquire(_backend):
        yield conn

    monkeypatch.setattr(memory_engine, "acquire_with_retry", acquire)
    return engine, conn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,skipped,ready",
    [
        ("Generating content...", True, False),
        ("  Generating content...\n", True, False),
        ("", True, False),
        (" \n", True, False),
        ("Existing document", True, True),
        ("Newly generated document", False, True),
    ],
)
async def test_refresh_readiness_survives_operation_read(content_engine, content, skipped, ready):
    engine, conn = content_engine
    operation_id = str(uuid.uuid4())
    outcome = "content_preserved_no_new_facts" if skipped else "content_written"
    rr = {"outcome": outcome, "based_on": {}}
    if skipped:
        rr["reflect_skipped"] = "no_sources_in_scope"
    await engine._write_refresh_outcome_metadata(operation_id, {"content": content, "reflect_response": rr})
    metadata = json.loads(conn.execute.call_args.args[2])
    assert metadata["populated_content"] is ready
    assert metadata["no_sources_in_scope"] is skipped
    assert metadata["outcome"] == outcome  # Existing outcome vocabulary is unchanged.
    conn.fetchrow.return_value = {
        "operation_id": operation_id,
        "operation_type": "refresh_mental_model",
        "status": "completed",
        "result_metadata": metadata,
        "created_at": None,
        "updated_at": None,
        "completed_at": None,
        "error_message": None,
        "retry_count": 0,
        "next_retry_at": None,
    }
    result = await engine.get_operation_status("bank", operation_id, request_context=RequestContext())
    assert result["status"] == "completed"
    assert result["details"]["content_ready"] is ready
    assert result["details"]["no_sources_in_scope"] is skipped
    assert result["details"]["outcome"] == outcome


@pytest.mark.parametrize(
    "metadata,expected_ready",
    [
        ({"outcome": "content_written"}, None),
        ({"outcome": "content_written", "populated_content": True}, True),
        ({"outcome": "content_preserved_no_new_facts", "populated_content": False}, False),
        # A failed retry must not report success metadata left by an earlier attempt.
        ({"outcome": "refresh_failed_error", "populated_content": True, "no_sources_in_scope": True}, None),
    ],
)
def test_old_and_failed_operations_do_not_invent_readiness(metadata, expected_ready):
    details = _operation_details("refresh_mental_model", metadata)
    assert details["content_ready"] is expected_ready
    assert details["no_sources_in_scope"] is None


def test_unfinished_and_other_operations_keep_existing_shape():
    assert _operation_details("refresh_mental_model", {}) is None
    assert _operation_details("batch_retain", {"outcome": "content_written"}) is None
    assert _operation_details("refresh_mental_model", {"outcome": "future_outcome"}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,ready",
    [
        (None, False),
        ("", False),
        (" \n", False),
        ("Generating content...", False),
        ("  Generating content...\n", False),
        ("# Actual document", True),
    ],
)
async def test_page_readiness_checks_body_not_timestamp(content_engine, content, ready):
    engine, conn = content_engine
    conn.fetchrow.return_value = {"mental_model_id": "mm-1", "mm_content": content}
    engine._row_to_knowledge_node = Mock(return_value={"id": "kp-1", "last_refreshed_at": "2026-01-01"})
    engine._gate_mental_model_read = AsyncMock()
    engine._record_mental_model_read = AsyncMock()
    page = await engine.get_knowledge_page("bank", "kp-1", request_context=RequestContext())
    assert page["content"] == content  # No mutation of the stored placeholder or preserved prose.
    assert page["content_ready"] is ready
    engine._gate_mental_model_read.assert_awaited_once()
    engine._record_mental_model_read.assert_awaited_once()
