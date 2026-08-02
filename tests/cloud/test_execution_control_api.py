"""Tests for execution control API endpoints: pause/resume/force_next.

Verifies:
- POST /api/v1/executions/{run_id}/pause   -> 200, publishes control message via Core NATS
- POST /api/v1/executions/{run_id}/resume  -> 200, publishes control message via Core NATS
- POST /api/v1/executions/{run_id}/force_next -> 200, publishes control message via Core NATS
- 404 when execution not found
- 409 when execution is in terminal state
- Control message published to ate.control.{run_id} subject with correct action
- SSE events published for each control action
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.execution import Execution


async def _insert_execution(
    db_session: AsyncSession,
    run_id: str,
    status: str = "RUNNING",
) -> Execution:
    """Insert an Execution record for testing."""
    execution = Execution(
        id=run_id,
        sequence_id="seq-test",
        status=status,
    )
    db_session.add(execution)
    await db_session.flush()
    return execution


def _make_mock_nc() -> MagicMock:
    """Build a mock NATS client with async publish."""
    mock_nc = MagicMock()
    mock_nc.publish = AsyncMock()
    return mock_nc


class TestPauseExecution:
    """Tests for POST /api/v1/executions/{run_id}/pause."""

    @pytest.mark.asyncio
    async def test_pause_returns_200(self, db_session, client) -> None:
        """POST /pause should return 200 with action=paused."""
        await _insert_execution(db_session, "run-pause-1", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post("/api/v1/executions/run-pause-1/pause")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "run-pause-1"
        assert data["action"] == "pause"
        assert data["status"] == "PAUSING"

    @pytest.mark.asyncio
    async def test_pause_publishes_control_message(self, db_session, client) -> None:
        """POST /pause should publish control message to ate.control.{run_id}."""
        await _insert_execution(db_session, "run-pause-2", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            await client.post("/api/v1/executions/run-pause-2/pause")

        mock_nc.publish.assert_awaited_once()
        subject = mock_nc.publish.call_args.args[0]
        assert subject == "ate.control.run-pause-2"

        payload = json.loads(mock_nc.publish.call_args.args[1])
        assert payload["action"] == "pause"
        assert payload["run_id"] == "run-pause-2"

    @pytest.mark.asyncio
    async def test_pause_404_when_not_found(self, db_session, client) -> None:
        """POST /pause should return 404 when execution not found."""
        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post("/api/v1/executions/nonexistent/pause")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_pause_409_when_terminal(self, db_session, client) -> None:
        """POST /pause should return 409 when execution is terminal."""
        await _insert_execution(db_session, "run-pause-3", status="COMPLETED")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post("/api/v1/executions/run-pause-3/pause")

        assert response.status_code == 409
        assert "terminal" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_pause_publishes_sse_event(self, db_session, client) -> None:
        """POST /pause should publish EXECUTION_PAUSED SSE event."""
        await _insert_execution(db_session, "run-pause-4", status="RUNNING")

        mock_nc = _make_mock_nc()
        mock_bridge = client.app.state.sse_bridge
        mock_bridge.publish_event = AsyncMock()

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            await client.post("/api/v1/executions/run-pause-4/pause")

        mock_bridge.publish_event.assert_awaited()
        call_kwargs = mock_bridge.publish_event.call_args.kwargs
        assert call_kwargs["run_id"] == "run-pause-4"
        assert call_kwargs["event_type"] == "EXECUTION_PAUSED"

    @pytest.mark.asyncio
    async def test_pause_without_nats_still_returns_200(self, db_session, client) -> None:
        """POST /pause should return 200 even when NATS is unavailable (RuntimeError)."""
        await _insert_execution(db_session, "run-pause-5", status="RUNNING")

        with patch("ate_cloud.main.get_nats", side_effect=RuntimeError("no nats")):
            response = await client.post("/api/v1/executions/run-pause-5/pause")

        assert response.status_code == 200
        assert response.json()["action"] == "pause"


class TestResumeExecution:
    """Tests for POST /api/v1/executions/{run_id}/resume."""

    @pytest.mark.asyncio
    async def test_resume_returns_200(self, db_session, client) -> None:
        """POST /resume should return 200 with action=resume."""
        await _insert_execution(db_session, "run-resume-1", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post("/api/v1/executions/run-resume-1/resume")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "run-resume-1"
        assert data["action"] == "resume"
        assert data["status"] == "RESUMING"

    @pytest.mark.asyncio
    async def test_resume_publishes_control_message(self, db_session, client) -> None:
        """POST /resume should publish control message to ate.control.{run_id}."""
        await _insert_execution(db_session, "run-resume-2", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            await client.post("/api/v1/executions/run-resume-2/resume")

        mock_nc.publish.assert_awaited_once()
        subject = mock_nc.publish.call_args.args[0]
        assert subject == "ate.control.run-resume-2"

        payload = json.loads(mock_nc.publish.call_args.args[1])
        assert payload["action"] == "resume"
        assert payload["run_id"] == "run-resume-2"

    @pytest.mark.asyncio
    async def test_resume_404_when_not_found(self, db_session, client) -> None:
        """POST /resume should return 404 when execution not found."""
        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post("/api/v1/executions/nonexistent/resume")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_resume_409_when_terminal(self, db_session, client) -> None:
        """POST /resume should return 409 when execution is terminal."""
        await _insert_execution(db_session, "run-resume-3", status="ABORTED")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post("/api/v1/executions/run-resume-3/resume")

        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_resume_publishes_sse_event(self, db_session, client) -> None:
        """POST /resume should publish EXECUTION_STARTED SSE event (resume signal)."""
        await _insert_execution(db_session, "run-resume-4", status="RUNNING")

        mock_nc = _make_mock_nc()
        mock_bridge = client.app.state.sse_bridge
        mock_bridge.publish_event = AsyncMock()

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            await client.post("/api/v1/executions/run-resume-4/resume")

        mock_bridge.publish_event.assert_awaited()
        call_kwargs = mock_bridge.publish_event.call_args.kwargs
        assert call_kwargs["run_id"] == "run-resume-4"
        assert call_kwargs["event_type"] == "EXECUTION_STARTED"

    @pytest.mark.asyncio
    async def test_resume_without_nats_still_returns_200(self, db_session, client) -> None:
        """POST /resume should return 200 even when NATS is unavailable."""
        await _insert_execution(db_session, "run-resume-5", status="RUNNING")

        with patch("ate_cloud.main.get_nats", side_effect=RuntimeError("no nats")):
            response = await client.post("/api/v1/executions/run-resume-5/resume")

        assert response.status_code == 200
        assert response.json()["action"] == "resume"


class TestForceNextExecution:
    """Tests for POST /api/v1/executions/{run_id}/force_next."""

    @pytest.mark.asyncio
    async def test_force_next_returns_200(self, db_session, client) -> None:
        """POST /force_next should return 200 with action=force_next."""
        await _insert_execution(db_session, "run-fn-1", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post("/api/v1/executions/run-fn-1/force_next")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "run-fn-1"
        assert data["action"] == "force_next"
        assert data["status"] == "FORCE_NEXT"

    @pytest.mark.asyncio
    async def test_force_next_publishes_control_message(self, db_session, client) -> None:
        """POST /force_next should publish control message to ate.control.{run_id}."""
        await _insert_execution(db_session, "run-fn-2", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            await client.post("/api/v1/executions/run-fn-2/force_next")

        mock_nc.publish.assert_awaited_once()
        subject = mock_nc.publish.call_args.args[0]
        assert subject == "ate.control.run-fn-2"

        payload = json.loads(mock_nc.publish.call_args.args[1])
        assert payload["action"] == "force_next"
        assert payload["run_id"] == "run-fn-2"

    @pytest.mark.asyncio
    async def test_force_next_404_when_not_found(self, db_session, client) -> None:
        """POST /force_next should return 404 when execution not found."""
        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post("/api/v1/executions/nonexistent/force_next")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_force_next_409_when_terminal(self, db_session, client) -> None:
        """POST /force_next should return 409 when execution is terminal."""
        await _insert_execution(db_session, "run-fn-3", status="FAILED")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post("/api/v1/executions/run-fn-3/force_next")

        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_force_next_publishes_sse_event(self, db_session, client) -> None:
        """POST /force_next should publish EXTERNAL_CMD SSE event."""
        await _insert_execution(db_session, "run-fn-4", status="RUNNING")

        mock_nc = _make_mock_nc()
        mock_bridge = client.app.state.sse_bridge
        mock_bridge.publish_event = AsyncMock()

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            await client.post("/api/v1/executions/run-fn-4/force_next")

        mock_bridge.publish_event.assert_awaited()
        call_kwargs = mock_bridge.publish_event.call_args.kwargs
        assert call_kwargs["run_id"] == "run-fn-4"
        assert call_kwargs["event_type"] == "EXTERNAL_CMD"

    @pytest.mark.asyncio
    async def test_force_next_without_nats_still_returns_200(self, db_session, client) -> None:
        """POST /force_next should return 200 even when NATS is unavailable."""
        await _insert_execution(db_session, "run-fn-5", status="RUNNING")

        with patch("ate_cloud.main.get_nats", side_effect=RuntimeError("no nats")):
            response = await client.post("/api/v1/executions/run-fn-5/force_next")

        assert response.status_code == 200
        assert response.json()["action"] == "force_next"


class TestControlEndpointAllStates:
    """Tests for control endpoints across different execution states."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["pause", "resume", "force_next"])
    async def test_works_on_pending_execution(
        self, db_session, client, action: str,
    ) -> None:
        """Control endpoints should work on PENDING executions (not yet RUNNING)."""
        await _insert_execution(db_session, f"run-state-{action}", status="PENDING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                f"/api/v1/executions/run-state-{action}/{action}",
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "terminal_status", ["COMPLETED", "FAILED", "ABORTED"],
    )
    async def test_rejects_terminal_states(
        self, db_session, client, terminal_status: str,
    ) -> None:
        """Control endpoints should reject terminal states with 409."""
        run_id = f"run-terminal-{terminal_status}"
        await _insert_execution(db_session, run_id, status=terminal_status)

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            for action in ["pause", "resume", "force_next"]:
                response = await client.post(f"/api/v1/executions/{run_id}/{action}")
                assert response.status_code == 409, (
                    f"{action} should return 409 for {terminal_status}"
                )


class TestControlMessageSubject:
    """Tests verifying the NATS control message subject format."""

    @pytest.mark.asyncio
    async def test_control_subject_uses_ate_control_prefix(
        self, db_session, client,
    ) -> None:
        """Control messages should be published to ate.control.{run_id}."""
        await _insert_execution(db_session, "run-subject-test", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            await client.post("/api/v1/executions/run-subject-test/pause")

        subject = mock_nc.publish.call_args.args[0]
        assert subject.startswith("ate.control.")
        assert subject.endswith("run-subject-test")

    @pytest.mark.asyncio
    async def test_control_payload_has_action_and_run_id(
        self, db_session, client,
    ) -> None:
        """Control message payload should contain action and run_id fields."""
        await _insert_execution(db_session, "run-payload-test", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            await client.post("/api/v1/executions/run-payload-test/force_next")

        payload_bytes = mock_nc.publish.call_args.args[1]
        payload = json.loads(payload_bytes)

        assert "action" in payload
        assert "run_id" in payload
        assert payload["action"] == "force_next"
        assert payload["run_id"] == "run-payload-test"
