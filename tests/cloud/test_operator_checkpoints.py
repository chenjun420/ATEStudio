"""API integration tests for the operator checkpoint endpoints.

Covers:
- GET /api/v1/executions/{run_id}/checkpoint/pending
    - 200 + pending=False when no handler registered
    - 200 + pending=False when handler has no pending checkpoint
    - 200 + pending=True with checkpoint definition when pending
- POST /api/v1/executions/{run_id}/checkpoint
    - 200 + pending=False after successful submission
    - 404 when execution not found
    - 404 when no handler registered for the run
    - 409 when no checkpoint is pending
    - 409 when submitted step_id does not match the pending one
- Auth bypass: dev_mode=True (set by cloud conftest) lets requests
  through without a token.

The tests register a real :class:`CheckpointHandler` on
``app.state.checkpoint_handlers`` (mirroring how the executor would
register its handler in production) and drive it directly to set up
the pending state, since the executor itself is not running.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.execution import Execution
from ate_platform.executor.checkpoint_handler import CheckpointHandler
from shared.operator_checkpoint import OperatorCheckpoint, OperatorInteractionType


@pytest.fixture
async def execution(db_session: AsyncSession) -> Execution:
    """Insert a minimal Execution row for the checkpoint tests."""
    execution = Execution(
        id="run-checkpoint-test",
        sequence_id="seq-1",
        status="RUNNING",
        config={},
    )
    db_session.add(execution)
    await db_session.commit()
    await db_session.refresh(execution)
    return execution


def _make_checkpoint(
    type_: OperatorInteractionType = OperatorInteractionType.SCAN,
    timeout_sec: float = 30.0,
    prompt: str = "Scan barcode for DUT SN-001",
    validation_regex: str | None = None,
) -> OperatorCheckpoint:
    """Build an OperatorCheckpoint with sensible test defaults."""
    return OperatorCheckpoint(
        type=type_,
        prompt=prompt,
        timeout_sec=timeout_sec,
        validation_regex=validation_regex,
    )


@pytest.fixture
def checkpoint_handler(app: Any) -> CheckpointHandler:
    """Register a CheckpointHandler on app.state for the test run.

    The handler is keyed by the execution id used throughout the tests.
    Cleans up the registry entry on teardown.
    """
    if not hasattr(app.state, "checkpoint_handlers"):
        app.state.checkpoint_handlers = {}
    handler = CheckpointHandler()
    app.state.checkpoint_handlers["run-checkpoint-test"] = handler
    yield handler
    app.state.checkpoint_handlers.pop("run-checkpoint-test", None)


class TestGetPendingCheckpoint:
    """GET /api/v1/executions/{run_id}/checkpoint/pending."""

    async def test_pending_false_when_no_handler(
        self, client: AsyncClient, execution: Execution,
    ) -> None:
        """Given no handler registered, when GET pending, then pending=False."""
        # Ensure no handler is registered for this run.
        resp = await client.get(
            f"/api/v1/executions/{execution.id}/checkpoint/pending"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == execution.id
        assert body["pending"] is False
        assert body["step_id"] is None
        assert body["checkpoint"] is None

    async def test_pending_false_when_handler_has_no_pending(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler,
    ) -> None:
        """Given a handler with no pending checkpoint, when GET pending, then pending=False."""
        resp = await client.get(
            f"/api/v1/executions/{execution.id}/checkpoint/pending"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pending"] is False

    async def test_pending_true_with_definition(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler,
    ) -> None:
        """Given a pending checkpoint, when GET pending, then returns the definition."""
        checkpoint = _make_checkpoint()
        # Drive the handler directly to register a pending checkpoint.
        # We start the wait in a background task, then GET, then resolve.
        wait_task = asyncio.create_task(
            checkpoint_handler.wait_for_response(
                execution.id, "step-scan-1", checkpoint,
            )
        )
        # Yield to let the wait register itself.
        await asyncio.sleep(0.05)

        try:
            resp = await client.get(
                f"/api/v1/executions/{execution.id}/checkpoint/pending"
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["pending"] is True
            assert body["step_id"] == "step-scan-1"
            assert body["checkpoint"]["type"] == "scan"
            assert body["checkpoint"]["prompt"] == "Scan barcode for DUT SN-001"
            assert body["checkpoint"]["timeout_sec"] == 30.0
            assert body["created_at"] is not None
        finally:
            # Clean up: resolve the checkpoint so the background task exits.
            checkpoint_handler.submit_response(
                execution.id, "step-scan-1", "SN-001",
            )
            await asyncio.wait_for(wait_task, timeout=2.0)

    async def test_404_when_execution_not_found(
        self, client: AsyncClient,
    ) -> None:
        """Given a non-existent execution, when GET pending, then 404."""
        resp = await client.get(
            "/api/v1/executions/run-nonexistent/checkpoint/pending"
        )
        assert resp.status_code == 404


class TestSubmitCheckpointResponse:
    """POST /api/v1/executions/{run_id}/checkpoint."""

    async def test_submit_resolves_pending_checkpoint(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler,
    ) -> None:
        """Given a pending checkpoint, when POST response, then 200 + pending=False."""
        checkpoint = _make_checkpoint()
        wait_task = asyncio.create_task(
            checkpoint_handler.wait_for_response(
                execution.id, "step-scan-2", checkpoint,
            )
        )
        await asyncio.sleep(0.05)

        try:
            resp = await client.post(
                f"/api/v1/executions/{execution.id}/checkpoint",
                json={
                    "step_id": "step-scan-2",
                    "response": "SN-002",
                    "reason": None,
                    "extra": {},
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["pending"] is False
            assert body["step_id"] == "step-scan-2"
            assert body["checkpoint"]["type"] == "scan"

            # The background wait should have completed with the response.
            response = await asyncio.wait_for(wait_task, timeout=2.0)
            assert response.response == "SN-002"
        finally:
            if not wait_task.done():
                checkpoint_handler.cancel(execution.id)
                try:
                    await asyncio.wait_for(wait_task, timeout=2.0)
                except RuntimeError:
                    pass

    async def test_submit_with_reason_for_visual_check_fail(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler,
    ) -> None:
        """Given a visual_check, when POST fail + reason, then reason is preserved."""
        checkpoint = _make_checkpoint(
            type_=OperatorInteractionType.VISUAL_CHECK,
            prompt="Inspect solder joints",
        )
        wait_task = asyncio.create_task(
            checkpoint_handler.wait_for_response(
                execution.id, "step-vc-1", checkpoint,
            )
        )
        await asyncio.sleep(0.05)

        try:
            resp = await client.post(
                f"/api/v1/executions/{execution.id}/checkpoint",
                json={
                    "step_id": "step-vc-1",
                    "response": "fail",
                    "reason": "Cracked joint on R12",
                    "extra": {},
                },
            )
            assert resp.status_code == 200
            response = await asyncio.wait_for(wait_task, timeout=2.0)
            assert response.response == "fail"
            assert response.reason == "Cracked joint on R12"
        finally:
            if not wait_task.done():
                checkpoint_handler.cancel(execution.id)
                try:
                    await asyncio.wait_for(wait_task, timeout=2.0)
                except RuntimeError:
                    pass

    async def test_404_when_execution_not_found(
        self, client: AsyncClient,
    ) -> None:
        """Given a non-existent execution, when POST response, then 404."""
        resp = await client.post(
            "/api/v1/executions/run-nonexistent/checkpoint",
            json={"step_id": "x", "response": "y"},
        )
        assert resp.status_code == 404

    async def test_404_when_no_handler_registered(
        self, client: AsyncClient, execution: Execution,
    ) -> None:
        """Given no handler for the run, when POST response, then 404."""
        resp = await client.post(
            f"/api/v1/executions/{execution.id}/checkpoint",
            json={"step_id": "step-x", "response": "ok"},
        )
        assert resp.status_code == 404
        assert "No active checkpoint handler" in resp.json()["detail"]

    async def test_409_when_no_checkpoint_pending(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler,
    ) -> None:
        """Given a handler with no pending checkpoint, when POST response, then 409."""
        resp = await client.post(
            f"/api/v1/executions/{execution.id}/checkpoint",
            json={"step_id": "step-none", "response": "ok"},
        )
        assert resp.status_code == 409
        assert "No pending checkpoint" in resp.json()["detail"]

    async def test_409_on_step_id_mismatch(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler,
    ) -> None:
        """Given a pending checkpoint, when POST wrong step_id, then 409."""
        checkpoint = _make_checkpoint(timeout_sec=10.0)
        wait_task = asyncio.create_task(
            checkpoint_handler.wait_for_response(
                execution.id, "step-correct", checkpoint,
            )
        )
        await asyncio.sleep(0.05)

        try:
            resp = await client.post(
                f"/api/v1/executions/{execution.id}/checkpoint",
                json={"step_id": "step-wrong", "response": "ok"},
            )
            assert resp.status_code == 409
            assert "does not match" in resp.json()["detail"]
        finally:
            checkpoint_handler.cancel(execution.id)
            try:
                await asyncio.wait_for(wait_task, timeout=2.0)
            except RuntimeError:
                pass

    async def test_submit_publishes_resolved_event(
        self, client: AsyncClient, execution: Execution,
        checkpoint_handler: CheckpointHandler, app: Any,
    ) -> None:
        """Given a pending checkpoint, when POST response, then an SSE event is published."""
        checkpoint = _make_checkpoint()
        wait_task = asyncio.create_task(
            checkpoint_handler.wait_for_response(
                execution.id, "step-evt", checkpoint,
            )
        )
        await asyncio.sleep(0.05)

        # Subscribe to the SSE bridge queue so we can observe the event.
        bridge = app.state.sse_bridge
        queue = bridge.get_or_create_queue(execution.id)

        try:
            resp = await client.post(
                f"/api/v1/executions/{execution.id}/checkpoint",
                json={"step_id": "step-evt", "response": "ok"},
            )
            assert resp.status_code == 200

            # The wait should resolve.
            await asyncio.wait_for(wait_task, timeout=2.0)

            # Drain the queue and look for the OPERATOR_CHECKPOINT_RESOLVED event.
            events: list[dict[str, Any]] = []
            try:
                while True:
                    events.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                pass

            resolved = [e for e in events if e.get("type") == "OPERATOR_CHECKPOINT_RESOLVED"]
            assert len(resolved) == 1
            assert resolved[0]["data"]["step_id"] == "step-evt"
            assert resolved[0]["data"]["response"] == "ok"
        finally:
            if not wait_task.done():
                checkpoint_handler.cancel(execution.id)
                try:
                    await asyncio.wait_for(wait_task, timeout=2.0)
                except RuntimeError:
                    pass
            bridge.remove_queue(execution.id)


class TestSchemaValidation:
    """Pydantic schema validation for OperatorCheckpointRequest/Response."""

    async def test_request_rejects_empty_step_id(self) -> None:
        """Given an empty step_id, when validating, then ValueError."""
        from ate_cloud.schemas.operator_checkpoint import OperatorCheckpointRequest

        with pytest.raises(ValueError, match="step_id"):
            OperatorCheckpointRequest(step_id="", response="ok")

    async def test_request_rejects_empty_response(self) -> None:
        """Given an empty response, when validating, then ValueError."""
        from ate_cloud.schemas.operator_checkpoint import OperatorCheckpointRequest

        with pytest.raises(ValueError, match="response"):
            OperatorCheckpointRequest(step_id="s1", response="")

    async def test_request_rejects_unknown_field(self) -> None:
        """Given an unknown field, when validating, then ValueError (extra=forbid)."""
        from ate_cloud.schemas.operator_checkpoint import OperatorCheckpointRequest

        with pytest.raises(ValueError, match="extra"):
            OperatorCheckpointRequest(  # type: ignore[call-arg]
                step_id="s1", response="ok", bogus=True,
            )

    async def test_checkpoint_rejects_unknown_type(self) -> None:
        """Given an invalid type, when validating OperatorCheckpoint, then ValueError."""
        with pytest.raises(ValueError):
            OperatorCheckpoint(  # type: ignore[arg-type]
                type="bogus", prompt="p", timeout_sec=10,
            )

    async def test_checkpoint_rejects_zero_timeout(self) -> None:
        """Given timeout_sec=0, when validating, then ValueError."""
        with pytest.raises(ValueError, match="timeout_sec"):
            OperatorCheckpoint(
                type=OperatorInteractionType.CONFIRM, prompt="p", timeout_sec=0,
            )

    async def test_checkpoint_rejects_empty_prompt(self) -> None:
        """Given an empty prompt, when validating, then ValueError."""
        with pytest.raises(ValueError, match="prompt"):
            OperatorCheckpoint(
                type=OperatorInteractionType.CONFIRM, prompt="", timeout_sec=10,
            )

    async def test_checkpoint_rejects_unknown_field(self) -> None:
        """Given an unknown field, when validating, then ValueError (extra=forbid)."""
        with pytest.raises(ValueError, match="extra"):
            OperatorCheckpoint(  # type: ignore[call-arg]
                type=OperatorInteractionType.CONFIRM,
                prompt="p", timeout_sec=10, bogus=True,
            )


# Quiet unused-import linters for fixtures only used via parameter names.
_ = AsyncGenerator
