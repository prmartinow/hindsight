"""Regression tests for UnscopedRefreshError non-retryable task handling.

An unscoped mental model refresh (missing positive exact tags or bounded tag groups)
is a deterministic configuration error. It must be classified as non-retryable by
_is_non_retryable_task_error and marked terminally failed on first execution by
the worker, without raising RetryTaskAt or scheduling retries, while preserving
content without calling reflect/LLM. Transient MentalModelRefreshError failures
(retrieval failures, tool errors, etc.) must remain retryable.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from hindsight_api.engine.llm_interface import ProviderContentPolicyError
from hindsight_api.engine.memory_engine import (
    MemoryEngine,
    MentalModelRefreshError,
    UnscopedRefreshError,
    _is_non_retryable_task_error,
)
from hindsight_api.engine.reflect import ReflectToolExecutionError
from hindsight_api.worker.exceptions import RetryTaskAt

# ---------------------------------------------------------------------------
# Unit classification regressions
# ---------------------------------------------------------------------------


def test_unscoped_refresh_error_is_classified_non_retryable():
    """UnscopedRefreshError is deterministic bad config and must not be retried."""
    err = UnscopedRefreshError(
        "Knowledge refresh requires a nonempty positive exact source scope",
        outcome="refresh_failed_error",
        reason="unscoped_sources",
    )
    assert _is_non_retryable_task_error(err) is True


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("refresh_failed_error", "retrieval_failed"),
        ("refresh_failed_error", "no_answer"),
        ("refresh_failed_error", "unexpected_error"),
        ("refresh_failed_delta_not_applied", "delta_ops_failed"),
    ],
)
def test_transient_mental_model_refresh_errors_remain_retryable(outcome, reason):
    """Base MentalModelRefreshError with transient failure reasons must remain retryable."""
    err = MentalModelRefreshError(
        f"Transient failure: {reason}",
        outcome=outcome,
        reason=reason,
    )
    assert _is_non_retryable_task_error(err) is False


def test_other_exceptions_classification_preserved():
    """Verify other error classifications are preserved."""
    import asyncpg

    # Existing non-retryable errors
    assert _is_non_retryable_task_error(ProviderContentPolicyError("refusal")) is True
    assert _is_non_retryable_task_error(asyncpg.exceptions.UniqueViolationError("pk")) is True

    # Existing retryable errors
    assert _is_non_retryable_task_error(RuntimeError("transient connection drop")) is False
    assert _is_non_retryable_task_error(asyncpg.exceptions.ForeignKeyViolationError("fk race")) is False


# ---------------------------------------------------------------------------
# Worker route integration regressions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_route_unscoped_refresh_fails_terminally_without_retry(
    memory: MemoryEngine, request_context
):
    """Actual worker route: unscoped refresh fails terminal failed once.

    Ensures:
    - Terminal failed once (status == 'failed')
    - retry_count unchanged / no RetryTaskAt raised
    - Typed failure_reason 'unscoped_sources' and outcome 'refresh_failed_error' preserved
    - Content unchanged
    - reflect / LLM not called
    """
    bank_id = f"test-unscoped-worker-{uuid.uuid4().hex[:8]}"
    operation_id = uuid.uuid4()
    original_content = "# Team Status\n\nAlice leads engineering.\n"

    await memory.ensure_bank_profile(bank_id, request_context=request_context)
    mm = await memory.create_mental_model(
        bank_id=bank_id,
        name="Unscoped Model",
        source_query="What is the team status?",
        content=original_content,
        tags=[],
        request_context=request_context,
    )

    pool = await memory._get_pool()
    payload = {
        "type": "refresh_mental_model",
        "operation_id": str(operation_id),
        "bank_id": bank_id,
        "mental_model_id": mm["id"],
        "_retry_count": 0,
    }
    await pool.execute(
        """
        INSERT INTO async_operations (operation_id, bank_id, operation_type, status, task_payload, result_metadata)
        VALUES ($1, $2, 'refresh_mental_model', 'pending', $3::jsonb, $4::jsonb)
        """,
        operation_id,
        bank_id,
        json.dumps(payload),
        json.dumps({"mental_model_id": mm["id"], "name": mm["name"]}),
    )

    reflect_mock = AsyncMock()
    with patch.object(memory, "reflect_async", reflect_mock):
        # Must execute without raising RetryTaskAt
        try:
            await memory.execute_task(payload)
        except RetryTaskAt as exc:
            pytest.fail(f"Unscoped refresh must not be retried, but execute_task raised RetryTaskAt: {exc}")

    # Reflect / LLM was not called
    reflect_mock.assert_not_called()

    # Operation row must be terminally failed with failure metadata preserved
    row = await pool.fetchrow(
        "SELECT status, error_message, result_metadata FROM async_operations WHERE operation_id = $1",
        operation_id,
    )
    assert row is not None
    assert row["status"] == "failed"
    assert "positive exact source scope" in (row["error_message"] or "")

    meta = json.loads(row["result_metadata"]) if isinstance(row["result_metadata"], str) else (row["result_metadata"] or {})
    assert meta.get("outcome") == "refresh_failed_error"
    assert meta.get("failure_reason") == "unscoped_sources"
    status = await memory.get_operation_status(
        bank_id=bank_id, operation_id=str(operation_id), request_context=request_context
    )
    assert status["retry_count"] == 0
    assert status["next_retry_at"] is None

    # Model content must be unchanged
    stored = await memory.get_mental_model(bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context)
    assert stored is not None
    assert stored["content"] == original_content

    await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
async def test_worker_route_transient_refresh_error_preserves_retry(
    memory: MemoryEngine, request_context, monkeypatch
):
    """Actual worker route: transient refresh failure raises RetryTaskAt and is not failed."""
    bank_id = f"test-transient-worker-{uuid.uuid4().hex[:8]}"
    operation_id = uuid.uuid4()
    original_content = "# Team Status\n\nAlice leads engineering.\n"

    await memory.ensure_bank_profile(bank_id, request_context=request_context)
    mm = await memory.create_mental_model(
        bank_id=bank_id,
        name="Scoped Model",
        source_query="What is the team status?",
        content=original_content,
        tags=["engineering"],
        request_context=request_context,
    )

    pool = await memory._get_pool()
    payload = {
        "type": "refresh_mental_model",
        "operation_id": str(operation_id),
        "bank_id": bank_id,
        "mental_model_id": mm["id"],
        "_retry_count": 0,
    }
    await pool.execute(
        """
        INSERT INTO async_operations (operation_id, bank_id, operation_type, status, task_payload, result_metadata)
        VALUES ($1, $2, 'refresh_mental_model', 'pending', $3::jsonb, $4::jsonb)
        """,
        operation_id,
        bank_id,
        json.dumps(payload),
        json.dumps({"mental_model_id": mm["id"], "name": mm["name"]}),
    )

    # Simulate a transient retrieval failure during reflect
    async def fake_reflect_fails(**kwargs):
        raise ReflectToolExecutionError("Reflect tool 'recall' timed out")

    from tests.test_refresh_outcome_metadata import stub_refresh_has_sources

    monkeypatch.setattr(memory, "reflect_async", fake_reflect_fails)
    stub_refresh_has_sources(monkeypatch, memory)

    with pytest.raises(RetryTaskAt) as excinfo:
        await memory.execute_task(payload)

    assert "Reflect tool 'recall' timed out" in str(excinfo.value)

    # Operation row must NOT be marked failed (it should still be pending / retrying)
    row = await pool.fetchrow(
        "SELECT status, result_metadata FROM async_operations WHERE operation_id = $1",
        operation_id,
    )
    assert row is not None
    assert row["status"] != "failed"

    # Model content must be unchanged
    stored = await memory.get_mental_model(bank_id=bank_id, mental_model_id=mm["id"], request_context=request_context)
    assert stored is not None
    assert stored["content"] == original_content

    await memory.delete_bank(bank_id, request_context=request_context)
