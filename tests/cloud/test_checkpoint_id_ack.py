"""API integration tests for the checkpoint-id ack alias endpoint (RH-6).

Covers ``POST /api/v1/checkpoints/{checkpoint_id}/ack`` -- the design-doc
path alias for ``POST /api/v1/executions/{run_id}/checkpoint/ack`` (T42):

- Alias success returns the same response shape as the old path.
- Alias ack resumes the executor and carries operator/note in ``extra``.
- Unknown checkpoint_id -> 404.
- Repeat ack (already resolved) -> 409 (same as the old path).
- Missing/empty operator -> 422 (schema-level, same as the old path).
- Anonymous request (dev_mode off) -> 401.
- SSE ``OPERATOR_CHECKPOINT_RESOLVED`` event payload is identical to the
  old path's (both routes delegate to the same shared ack helper).
- The pending ``OPERATOR_CHECKPOINT`` SSE event carries the stable
  checkpoint id assigned by the cloud-side registry.
- The id is stable across GET /pending retries (one id, one event).
- The old ``/executions/{run_id}/checkpoint/ack`` path still works
  (regression guard).

The stable id is assigned when the cloud API first observes the pending
checkpoint (``GET .../checkpoint/pending``) -- the same moment the
``OPERATOR_CHECKPOINT`` SSE event is published. The id maps back to
``(run_id, step_id)`` so the alias endpoint can delegate to the exact
ack logic the old path uses.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.execution import Execution
from ate_platform.executor.checkpoint_handler import CheckpointHandler
from shared.operator_checkpoint import OperatorCheckpoint, OperatorInteractionType

RUN_ID = "run-cid-ack"


@pytest.fixture
async def execution(db_session: AsyncSession) -> Execution:
    """Insert a minimal Execution row for the checkpoint-id alias tests."""
    execution = Execution(
        id=RUN_ID,
        sequence_id="seq-1",
        status="RUNNING",
        config={},
    )
    db_session.add(execution)
    await db_session.commit()
    await db_session.refresh(execution)
    return execution


@pytest.fixture
def checkpoint_handler(app: Any) -> AsyncGenerator[CheckpointHandler, None]:
    """Register a CheckpointHandler on app.state for the test run.

    The handler is keyed by the execution id used throughout the tests.
    Cleans up the registry entry on teardown.
    """
    if not hasattr(app.state, "checkpoint_handlers"):
        app.state.checkpoint_handlers = {}
    handler = CheckpointHandler()
    app.state.checkpoint_handlers[RUN_ID] = handler
    yield handler
    app.state.checkpoint_handlers.pop(RUN_ID, None)


async def _make_pending(
    handler: CheckpointHandler,
    run_id: str,
    step_id: str = "step-cid-1",
    prompt: str = "确认工装就绪",
) -> asyncio.Task[object]:
    """Register a pending confirm checkpoint and return the wait task."""
    checkpoint = OperatorCheckpoint(
        type=OperatorInteractionType.CONFIRM,
        prompt=prompt,
        timeout_sec=30.0,
    )
    wait_task = asyncio.create_task(
        handler.wait_for_response(run_id, step_id, checkpoint)
    )
    await asyncio.sleep(0.05)
    return wait_task


async def _get_checkpoint_id(client: AsyncClient, run_id: str) -> str:
    """Observe the pending checkpoint via GET /pending and return its id."""
    resp = await client.get(f"/api/v1/executions/{run_id}/checkpoint/pending")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pending"] is True
    checkpoint_id = body["checkpoint_id"]
    assert checkpoint_id, "GET /pending must expose the stable checkpoint_id"
    return checkpoint_id


async def _drain_queue(queue: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drain all events currently sitting in an SSE queue."""
    events: list[dict[str, Any]] = []
    try:
        while True:
            events.append(queue.get_nowait())
    except asyncio.QueueEmpty:
        pass
    return events


async def _cleanup_wait(
    handler: CheckpointHandler, run_id: str, wait_task: asyncio.Task[object],
) -> None:
    """Cancel a still-running wait task so the handler slot is freed."""
    if not wait_task.done():
        handler.cancel(run_id)
        try:
            await asyncio.wait_for(wait_task, timeout=2.0)
        except RuntimeError:
            pass


class TestCheckpointIdAckAlias:
    """POST /api/v1/checkpoints/{checkpoint_id}/ack (RH-6 alias)."""

    async def test_alias_ack_returns_same_shape_as_old_path(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler,
    ) -> None:
        """Alias success response has the same shape/fields as the old path."""
        wait_a = await _make_pending(checkpoint_handler, execution.id, "step-shape-a")
        try:
            cid = await _get_checkpoint_id(client, execution.id)
            alias_resp = await client.post(
                f"/api/v1/checkpoints/{cid}/ack",
                json={"operator": "张三", "note": "别名路径确认"},
            )
            assert alias_resp.status_code == 200
            await asyncio.wait_for(wait_a, timeout=2.0)
        finally:
            await _cleanup_wait(checkpoint_handler, execution.id, wait_a)

        wait_b = await _make_pending(checkpoint_handler, execution.id, "step-shape-b")
        try:
            old_resp = await client.post(
                f"/api/v1/executions/{execution.id}/checkpoint/ack",
                json={"step_id": "step-shape-b", "operator": "张三", "note": None},
            )
            assert old_resp.status_code == 200
            await asyncio.wait_for(wait_b, timeout=2.0)
        finally:
            await _cleanup_wait(checkpoint_handler, execution.id, wait_b)

        alias_body = alias_resp.json()
        old_body = old_resp.json()
        # Identical response shape (same fields, same types of values).
        assert set(alias_body) == set(old_body)
        # Shared field values.
        assert alias_body["run_id"] == old_body["run_id"] == execution.id
        assert alias_body["operator"] == old_body["operator"] == "张三"
        assert alias_body["pending"] == old_body["pending"] is False
        assert alias_body["acknowledged_at"] is not None
        # Step-specific fields resolve to each pending checkpoint's step.
        assert alias_body["step_id"] == "step-shape-a"
        assert old_body["step_id"] == "step-shape-b"
        assert alias_body["note"] == "别名路径确认"

    async def test_alias_ack_resumes_executor_and_carries_operator(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler,
    ) -> None:
        """Alias ack resolves the blocked wait with operator identity in extra."""
        wait_task = await _make_pending(checkpoint_handler, execution.id)
        try:
            cid = await _get_checkpoint_id(client, execution.id)
            resp = await client.post(
                f"/api/v1/checkpoints/{cid}/ack",
                json={"operator": "李四", "note": "更换夹具后确认"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["operator"] == "李四"
            assert body["note"] == "更换夹具后确认"

            response = await asyncio.wait_for(wait_task, timeout=2.0)
            assert response.response == "ok"
            assert response.reason == "更换夹具后确认"
            assert response.extra == {"operator": "李四", "note": "更换夹具后确认"}
        finally:
            await _cleanup_wait(checkpoint_handler, execution.id, wait_task)

    async def test_unknown_checkpoint_id_returns_404(
        self, client: AsyncClient, execution: Execution,
    ) -> None:
        """An id never assigned by the registry is rejected with 404."""
        resp = await client.post(
            f"/api/v1/checkpoints/{uuid.uuid4()}/ack",
            json={"operator": "op"},
        )
        assert resp.status_code == 404
        assert "checkpoint" in resp.json()["detail"].lower()

    async def test_repeat_ack_returns_409(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler,
    ) -> None:
        """Acking an already-resolved checkpoint id again returns 409."""
        wait_task = await _make_pending(checkpoint_handler, execution.id)
        try:
            cid = await _get_checkpoint_id(client, execution.id)
            first = await client.post(
                f"/api/v1/checkpoints/{cid}/ack",
                json={"operator": "op"},
            )
            assert first.status_code == 200
            await asyncio.wait_for(wait_task, timeout=2.0)

            repeat = await client.post(
                f"/api/v1/checkpoints/{cid}/ack",
                json={"operator": "op"},
            )
            assert repeat.status_code == 409
            assert "No pending checkpoint" in repeat.json()["detail"]
        finally:
            await _cleanup_wait(checkpoint_handler, execution.id, wait_task)

    async def test_missing_operator_returns_422(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler,
    ) -> None:
        """Missing or empty operator is rejected at the schema level (422)."""
        wait_task = await _make_pending(checkpoint_handler, execution.id)
        try:
            cid = await _get_checkpoint_id(client, execution.id)
            missing = await client.post(
                f"/api/v1/checkpoints/{cid}/ack",
                json={"note": "no operator"},
            )
            assert missing.status_code == 422

            empty = await client.post(
                f"/api/v1/checkpoints/{cid}/ack",
                json={"operator": ""},
            )
            assert empty.status_code == 422
        finally:
            await _cleanup_wait(checkpoint_handler, execution.id, wait_task)

    async def test_anonymous_returns_401(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Anonymous request (dev_mode off) must be rejected with 401."""
        from ate_cloud.config import settings

        monkeypatch.setattr(settings, "dev_mode", False)
        resp = await client.post(
            f"/api/v1/checkpoints/{uuid.uuid4()}/ack",
            json={"operator": "op"},
        )
        assert resp.status_code == 401

    async def test_resolved_sse_payload_identical_to_old_path(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler, app: Any,
    ) -> None:
        """The alias's OPERATOR_CHECKPOINT_RESOLVED payload matches the old path's."""
        bridge = app.state.sse_bridge
        queue = bridge.get_or_create_queue(execution.id)
        wait_a: asyncio.Task[object] | None = None
        wait_b: asyncio.Task[object] | None = None
        try:
            # Leg 1: ack via the alias path.
            wait_a = await _make_pending(checkpoint_handler, execution.id, "step-sse-a")
            cid = await _get_checkpoint_id(client, execution.id)
            resp = await client.post(
                f"/api/v1/checkpoints/{cid}/ack",
                json={"operator": "op-sse", "note": None},
            )
            assert resp.status_code == 200
            await asyncio.wait_for(wait_a, timeout=2.0)

            # Leg 2: ack via the old path (same run, different step).
            wait_b = await _make_pending(checkpoint_handler, execution.id, "step-sse-b")
            resp = await client.post(
                f"/api/v1/executions/{execution.id}/checkpoint/ack",
                json={"step_id": "step-sse-b", "operator": "op-sse", "note": None},
            )
            assert resp.status_code == 200
            await asyncio.wait_for(wait_b, timeout=2.0)

            events = await _drain_queue(queue)
            resolved = [
                e for e in events if e.get("type") == "OPERATOR_CHECKPOINT_RESOLVED"
            ]
            assert len(resolved) == 2
            by_step = {e["data"]["step_id"]: e["data"] for e in resolved}
            alias_data = by_step["step-sse-a"]
            old_data = by_step["step-sse-b"]

            # Identical payload: same fields, same values (except step_id,
            # which identifies the pending checkpoint being acked).
            assert set(alias_data) == set(old_data)
            for field in alias_data:
                if field != "step_id":
                    assert alias_data[field] == old_data[field]
            # And the payload is exactly what the old path publishes.
            assert alias_data == {
                "run_id": execution.id,
                "step_id": "step-sse-a",
                "response": "ok",
                "reason": None,
                "operator": "op-sse",
                "note": None,
            }
        finally:
            bridge.remove_queue(execution.id)
            if wait_a is not None:
                await _cleanup_wait(checkpoint_handler, execution.id, wait_a)
            if wait_b is not None:
                await _cleanup_wait(checkpoint_handler, execution.id, wait_b)


class TestPendingCheckpointId:
    """Stable uuid assignment + pending SSE event (observation-time)."""

    async def test_pending_sse_event_carries_checkpoint_id(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler, app: Any,
    ) -> None:
        """Observing a pending checkpoint publishes OPERATOR_CHECKPOINT with its id."""
        wait_task = await _make_pending(checkpoint_handler, execution.id)
        bridge = app.state.sse_bridge
        queue = bridge.get_or_create_queue(execution.id)
        try:
            cid = await _get_checkpoint_id(client, execution.id)
            # The id is a valid uuid.
            uuid.UUID(cid)

            events = await _drain_queue(queue)
            pending_events = [
                e for e in events if e.get("type") == "OPERATOR_CHECKPOINT"
            ]
            assert len(pending_events) == 1
            data = pending_events[0]["data"]
            assert data["checkpoint_id"] == cid
            assert data["run_id"] == execution.id
            assert data["step_id"] == "step-cid-1"
            assert data["checkpoint"]["type"] == "confirm"
            assert data["checkpoint"]["prompt"] == "确认工装就绪"
            assert data["created_at"]
        finally:
            bridge.remove_queue(execution.id)
            await _cleanup_wait(checkpoint_handler, execution.id, wait_task)

    async def test_checkpoint_id_stable_across_retries(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler, app: Any,
    ) -> None:
        """Repeated GET /pending observations return the same id, one SSE event."""
        wait_task = await _make_pending(checkpoint_handler, execution.id)
        bridge = app.state.sse_bridge
        queue = bridge.get_or_create_queue(execution.id)
        try:
            cids = [
                await _get_checkpoint_id(client, execution.id) for _ in range(3)
            ]
            assert len(set(cids)) == 1, "checkpoint id must be stable across retries"

            events = await _drain_queue(queue)
            pending_events = [
                e for e in events if e.get("type") == "OPERATOR_CHECKPOINT"
            ]
            assert len(pending_events) == 1, "SSE pending event fires once per id"
            assert pending_events[0]["data"]["checkpoint_id"] == cids[0]
        finally:
            bridge.remove_queue(execution.id)
            await _cleanup_wait(checkpoint_handler, execution.id, wait_task)


class TestOldPathRegression:
    """The T42 path must keep working unchanged."""

    async def test_old_ack_path_still_works(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler,
    ) -> None:
        """POST /executions/{run_id}/checkpoint/ack keeps its T42 contract."""
        wait_task = await _make_pending(checkpoint_handler, execution.id, "step-old-1")
        try:
            resp = await client.post(
                f"/api/v1/executions/{execution.id}/checkpoint/ack",
                json={"step_id": "step-old-1", "operator": "王五"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["run_id"] == execution.id
            assert body["step_id"] == "step-old-1"
            assert body["operator"] == "王五"
            assert body["pending"] is False

            response = await asyncio.wait_for(wait_task, timeout=2.0)
            assert response.response == "ok"
            assert response.extra["operator"] == "王五"
        finally:
            await _cleanup_wait(checkpoint_handler, execution.id, wait_task)


# Quiet unused-import linters for fixtures only used via parameter names.
_ = AsyncGenerator
