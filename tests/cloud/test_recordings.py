"""Tests for the Recordings API endpoints.

Covers:
- POST /api/v1/executions/{id}/record - start recording
- GET /api/v1/executions/{id}/recording - get recording status
- POST /api/v1/executions/{id}/replay - start replay
- GET /api/v1/executions/{id}/recordings - list recorded events
- POST /api/v1/executions/{id}/replay/diff - compute diff
- POST /api/v1/executions/{id}/replay/pause - pause replay
- POST /api/v1/executions/{id}/replay/resume - resume replay
- GET /api/v1/executions/{id}/replay/stream - SSE replay stream

Uses mock NATS clients (no real NATS server required). Auth is bypassed
via dev_mode in the cloud conftest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.execution import Execution
from ate_platform.recorder.types import RecordedEvent, RecordedEventType


def _make_mock_nc(
    events: list[RecordedEvent] | None = None,
    connected: bool = True,
) -> MagicMock:
    """Build a mock NATS client with a connected JetStream context.

    If events are provided, pull_subscribe returns mock messages whose
    data is the JSONL of each event. Otherwise fetch raises TimeoutError
    (empty stream).
    """
    if events:
        msgs = []
        for e in events:
            msg = MagicMock()
            msg.data = e.to_jsonl().encode("utf-8")
            msg.ack = AsyncMock()
            msgs.append(msg)
        psub = MagicMock()
        psub.fetch = AsyncMock(return_value=msgs)
        psub.unsubscribe = AsyncMock()
    else:
        psub = MagicMock()
        psub.fetch = AsyncMock(side_effect=TimeoutError())
        psub.unsubscribe = AsyncMock()

    js = MagicMock()
    js.pull_subscribe = AsyncMock(return_value=psub)
    js.publish = AsyncMock(return_value=MagicMock(seq=1))

    nc = MagicMock()
    nc.is_connected = connected
    nc.jetstream = MagicMock(return_value=js)
    nc.publish = AsyncMock()
    return nc


def _make_events(session_id: str = "run-test") -> list[RecordedEvent]:
    """Build a small list of recorded events for testing."""
    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    return [
        RecordedEvent(
            timestamp=base,
            event_type=RecordedEventType.STEP_TRANSITION,
            session_id=session_id,
            step_id="s1",
            data={"from_status": "PENDING", "to_status": "RUNNING"},
        ),
        RecordedEvent(
            timestamp=base + timedelta(seconds=1),
            event_type=RecordedEventType.MEASUREMENT_RESULT,
            session_id=session_id,
            step_id="s1",
            data={"name": "voltage", "value": 5.0, "unit": "V"},
        ),
        RecordedEvent(
            timestamp=base + timedelta(seconds=2),
            event_type=RecordedEventType.STEP_TRANSITION,
            session_id=session_id,
            step_id="s1",
            data={"from_status": "RUNNING", "to_status": "PASSED"},
        ),
    ]


async def _insert_execution(db_session: AsyncSession, run_id: str) -> None:
    """Insert a minimal Execution record so _verify_execution_exists passes."""
    execution = Execution(id=run_id, sequence_id="seq-1", status="RUNNING")
    db_session.add(execution)
    await db_session.flush()


class TestStartRecording:
    """Tests for POST /api/v1/executions/{id}/record."""

    async def test_start_recording_success(self, db_session: AsyncSession, client: AsyncClient) -> None:
        """POST /record returns 200 with status=recording."""
        await _insert_execution(db_session, "run-record-1")
        mock_nc = _make_mock_nc()

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post("/api/v1/executions/run-record-1/record")

        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "run-record-1"
        assert data["status"] == "recording"
        assert data["subject"] == "ate.execution.run-record-1.events"

    async def test_start_recording_not_found(self, client: AsyncClient) -> None:
        """POST /record returns 404 for unknown execution."""
        response = await client.post("/api/v1/executions/nonexistent/record")
        assert response.status_code == 404

    async def test_start_recording_nats_unavailable(self, db_session: AsyncSession, client: AsyncClient) -> None:
        """POST /record returns 503 when NATS client is unavailable."""
        await _insert_execution(db_session, "run-record-2")

        with patch("ate_cloud.main.get_nats", side_effect=RuntimeError("not connected")):
            response = await client.post("/api/v1/executions/run-record-2/record")

        assert response.status_code == 503

    async def test_start_recording_idempotent(self, db_session: AsyncSession, client: AsyncClient) -> None:
        """POST /record on an already-recording session returns existing status."""
        await _insert_execution(db_session, "run-record-3")
        mock_nc = _make_mock_nc()

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response1 = await client.post("/api/v1/executions/run-record-3/record")
            response2 = await client.post("/api/v1/executions/run-record-3/record")

        assert response1.status_code == 200
        assert response2.status_code == 200
        assert response1.json()["session_id"] == response2.json()["session_id"]


class TestGetRecordingStatus:
    """Tests for GET /api/v1/executions/{id}/recording."""

    async def test_status_not_recording(self, db_session: AsyncSession, client: AsyncClient) -> None:
        """GET /recording returns is_recording=False when no recorder exists."""
        await _insert_execution(db_session, "run-status-1")
        response = await client.get("/api/v1/executions/run-status-1/recording")
        assert response.status_code == 200
        data = response.json()
        assert data["is_recording"] is False
        assert data["event_count"] == 0

    async def test_status_after_start(self, db_session: AsyncSession, client: AsyncClient) -> None:
        """GET /recording returns is_recording=True after recording starts."""
        await _insert_execution(db_session, "run-status-2")
        mock_nc = _make_mock_nc()

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            await client.post("/api/v1/executions/run-status-2/record")
            response = await client.get("/api/v1/executions/run-status-2/recording")

        assert response.status_code == 200
        data = response.json()
        assert data["is_recording"] is True
        assert data["subject"] == "ate.execution.run-status-2.events"

    async def test_status_not_found(self, client: AsyncClient) -> None:
        """GET /recording returns 404 for unknown execution."""
        response = await client.get("/api/v1/executions/nonexistent/recording")
        assert response.status_code == 404


class TestStartReplay:
    """Tests for POST /api/v1/executions/{id}/replay."""

    async def test_replay_returns_events(self, db_session: AsyncSession, client: AsyncClient) -> None:
        """POST /replay returns recorded events in timestamp order."""
        await _insert_execution(db_session, "run-replay-1")
        events = _make_events("run-replay-1")
        mock_nc = _make_mock_nc(events=events)

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions/run-replay-1/replay",
                json={"speed_multiplier": 100.0},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["events_replayed"] == 3
        assert data["events_total"] == 3
        assert len(data["events"]) == 3

    async def test_replay_not_found(self, client: AsyncClient) -> None:
        """POST /replay returns 404 for unknown execution."""
        response = await client.post(
            "/api/v1/executions/nonexistent/replay",
            json={"speed_multiplier": 1.0},
        )
        assert response.status_code == 404

    async def test_replay_max_events_limit(self, db_session: AsyncSession, client: AsyncClient) -> None:
        """POST /replay respects max_events limit."""
        await _insert_execution(db_session, "run-replay-2")
        events = _make_events("run-replay-2")
        mock_nc = _make_mock_nc(events=events)

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions/run-replay-2/replay",
                json={"speed_multiplier": 100.0, "max_events": 2},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["events_replayed"] == 2
        assert data["events_total"] == 3

    async def test_replay_nats_unavailable(self, db_session: AsyncSession, client: AsyncClient) -> None:
        """POST /replay returns 503 when NATS is unavailable."""
        await _insert_execution(db_session, "run-replay-3")

        with patch("ate_cloud.main.get_nats", side_effect=RuntimeError("not connected")):
            response = await client.post(
                "/api/v1/executions/run-replay-3/replay",
                json={"speed_multiplier": 1.0},
            )

        assert response.status_code == 503


class TestListRecordings:
    """Tests for GET /api/v1/executions/{id}/recordings."""

    async def test_list_recordings(self, db_session: AsyncSession, client: AsyncClient) -> None:
        """GET /recordings returns events in timestamp order."""
        await _insert_execution(db_session, "run-list-1")
        events = _make_events("run-list-1")
        mock_nc = _make_mock_nc(events=events)

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.get("/api/v1/executions/run-list-1/recordings")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3
        # Verify sorted by timestamp
        timestamps = [e["timestamp"] for e in data]
        assert timestamps == sorted(timestamps)

    async def test_list_recordings_empty(self, db_session: AsyncSession, client: AsyncClient) -> None:
        """GET /recordings returns empty list when no events exist."""
        await _insert_execution(db_session, "run-list-2")
        mock_nc = _make_mock_nc(events=None)

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.get("/api/v1/executions/run-list-2/recordings")

        assert response.status_code == 200
        assert response.json() == []

    async def test_list_recordings_not_found(self, client: AsyncClient) -> None:
        """GET /recordings returns 404 for unknown execution."""
        response = await client.get("/api/v1/executions/nonexistent/recordings")
        assert response.status_code == 404


class TestReplayDiff:
    """Tests for POST /api/v1/executions/{id}/replay/diff."""

    async def test_compute_diff_identical(self, db_session: AsyncSession, client: AsyncClient) -> None:
        """POST /replay/diff with identical sequences shows no changes."""
        await _insert_execution(db_session, "run-diff-1")
        events = _make_events("run-diff-1")
        mock_nc = _make_mock_nc(events=events)

        original_json = [e.model_dump(mode="json") for e in events]

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions/run-diff-1/replay/diff",
                json=original_json,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["summary"]["added"] == 0
        assert data["summary"]["removed"] == 0
        assert data["summary"]["changed"] == 0

    async def test_compute_diff_not_found(self, client: AsyncClient) -> None:
        """POST /replay/diff returns 404 for unknown execution."""
        response = await client.post(
            "/api/v1/executions/nonexistent/replay/diff",
            json=[],
        )
        assert response.status_code == 404


class TestReplayPauseResume:
    """Tests for POST /replay/pause and POST /replay/resume."""

    async def test_pause_returns_404_when_no_active_replay(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """POST /replay/pause returns 404 when no active replay exists."""
        await _insert_execution(db_session, "run-pause-1")
        response = await client.post("/api/v1/executions/run-pause-1/replay/pause")
        assert response.status_code == 404

    async def test_resume_returns_404_when_no_active_replay(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """POST /replay/resume returns 404 when no active replay exists."""
        await _insert_execution(db_session, "run-resume-1")
        response = await client.post("/api/v1/executions/run-resume-1/replay/resume")
        assert response.status_code == 404

    async def test_pause_resume_with_active_replay(
        self, db_session: AsyncSession, client: AsyncClient, app: FastAPI
    ) -> None:
        """POST /replay/pause and /replay/resume work when a ReplayExecutor is registered."""
        from ate_platform.recorder import ReplayExecutor

        await _insert_execution(db_session, "run-pr-1")

        # Manually register a ReplayExecutor on app.state
        executor = ReplayExecutor(session_id="run-pr-1")
        app.state.replay_executors = {"run-pr-1": executor}

        assert executor.is_paused is False

        response_pause = await client.post("/api/v1/executions/run-pr-1/replay/pause")
        assert response_pause.status_code == 200
        assert response_pause.json()["status"] == "paused"
        assert executor.is_paused is True

        response_resume = await client.post("/api/v1/executions/run-pr-1/replay/resume")
        assert response_resume.status_code == 200
        assert response_resume.json()["status"] == "resumed"
        assert executor.is_paused is False

    async def test_pause_not_found_execution(self, client: AsyncClient) -> None:
        """POST /replay/pause returns 404 for unknown execution."""
        response = await client.post("/api/v1/executions/nonexistent/replay/pause")
        assert response.status_code == 404


class TestReplayStream:
    """Tests for GET /api/v1/executions/{id}/replay/stream (SSE)."""

    async def test_stream_returns_sse_events(self, db_session: AsyncSession, client: AsyncClient) -> None:
        """GET /replay/stream returns SSE-formatted events."""
        await _insert_execution(db_session, "run-stream-1")
        events = _make_events("run-stream-1")
        mock_nc = _make_mock_nc(events=events)

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.get(
                "/api/v1/executions/run-stream-1/replay/stream?speed=100.0",
            )

        assert response.status_code == 200
        body = response.text
        # SSE events have "event:" and "data:" lines
        assert "event:" in body
        assert "data:" in body

    async def test_stream_not_found(self, client: AsyncClient) -> None:
        """GET /replay/stream returns 404 for unknown execution."""
        response = await client.get("/api/v1/executions/nonexistent/replay/stream")
        assert response.status_code == 404

    async def test_stream_registers_executor(
        self, db_session: AsyncSession, client: AsyncClient, app: FastAPI
    ) -> None:
        """GET /replay/stream registers the ReplayExecutor on app.state during streaming."""
        await _insert_execution(db_session, "run-stream-2")
        events = _make_events("run-stream-2")
        mock_nc = _make_mock_nc(events=events)

        if hasattr(app.state, "replay_executors"):
            del app.state.replay_executors

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.get(
                "/api/v1/executions/run-stream-2/replay/stream?speed=100.0",
            )

        assert response.status_code == 200
        # After stream completes, the executor should be cleaned up
        executors = getattr(app.state, "replay_executors", {})
        assert "run-stream-2" not in executors
