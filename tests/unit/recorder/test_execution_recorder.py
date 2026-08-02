"""Tests for ExecutionRecorder (recorder.execution_recorder)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ate_platform.recorder.execution_recorder import ExecutionRecorder
from ate_platform.recorder.types import RecordedEvent, RecordedEventType


def _make_mock_nc(connected: bool = True) -> MagicMock:
    """Build a mock NATS client with a connected JetStream context."""
    mock_js = MagicMock()
    mock_js.publish = AsyncMock(return_value=MagicMock(seq=1))
    mock_nc = MagicMock()
    mock_nc.is_connected = connected
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc


class TestExecutionRecorderInit:
    """Tests for ExecutionRecorder initialization and properties."""

    def test_init_sets_session_id(self) -> None:
        """session_id property returns the constructor value."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        assert recorder.session_id == "run-1"

    def test_init_sets_subject(self) -> None:
        """subject property is formatted with the session_id."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-42", nats_client=nc)
        assert recorder.subject == "ate.execution.run-42.events"

    def test_not_running_before_start(self) -> None:
        """is_running is False before start() is called."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        assert recorder.is_running is False

    def test_event_count_starts_at_zero(self) -> None:
        """event_count is 0 before any events are published."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        assert recorder.event_count == 0


class TestExecutionRecorderStartStop:
    """Tests for start() and stop() lifecycle."""

    async def test_start_sets_running(self) -> None:
        """start() flips is_running to True."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        await recorder.start()
        assert recorder.is_running is True
        await recorder.stop()

    async def test_start_raises_when_not_connected(self) -> None:
        """start() raises RuntimeError when NATS client is not connected."""
        nc = _make_mock_nc(connected=False)
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        with pytest.raises(RuntimeError, match="not connected"):
            await recorder.start()

    async def test_start_idempotent(self) -> None:
        """Calling start() twice does not create a second flush task."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        await recorder.start()
        await recorder.start()
        assert recorder.is_running is True
        await recorder.stop()

    async def test_stop_sets_not_running(self) -> None:
        """stop() flips is_running to False."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        await recorder.start()
        await recorder.stop()
        assert recorder.is_running is False

    async def test_stop_idempotent(self) -> None:
        """Calling stop() twice is safe."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        await recorder.start()
        await recorder.stop()
        await recorder.stop()
        assert recorder.is_running is False


class TestExecutionRecorderRecord:
    """Tests for the record() and convenience methods."""

    async def test_record_enqueues_event(self) -> None:
        """record() puts the event on the internal queue."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        await recorder.start()
        event = RecordedEvent(
            event_type=RecordedEventType.STEP_TRANSITION,
            session_id="run-1",
            step_id="s1",
        )
        await recorder.record(event)
        await asyncio.sleep(0.05)
        # The flush task should have published it
        js = nc.jetstream.return_value
        assert js.publish.await_count >= 1
        await recorder.stop()

    async def test_record_rejects_mismatched_session(self) -> None:
        """record() raises ValueError if event.session_id != recorder.session_id."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        event = RecordedEvent(
            event_type=RecordedEventType.STEP_TRANSITION,
            session_id="run-other",
        )
        with pytest.raises(ValueError, match="does not match"):
            await recorder.record(event)

    async def test_record_step_transition(self) -> None:
        """record_step_transition builds a step_transition event."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        await recorder.start()
        await recorder.record_step_transition("s1", "PENDING", "RUNNING")
        await asyncio.sleep(0.05)
        js = nc.jetstream.return_value
        payload = js.publish.call_args.args[1]
        assert b"step_transition" in payload
        await recorder.stop()

    async def test_record_measurement_result(self) -> None:
        """record_measurement_result builds a measurement_result event."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        await recorder.start()
        await recorder.record_measurement_result("s1", "voltage", 5.0, "V")
        await asyncio.sleep(0.05)
        js = nc.jetstream.return_value
        payload = js.publish.call_args.args[1]
        assert b"measurement_result" in payload
        assert b"voltage" in payload
        await recorder.stop()

    async def test_record_operator_interaction(self) -> None:
        """record_operator_interaction builds an operator_interaction event."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        await recorder.start()
        await recorder.record_operator_interaction("button_press", {"button": "start"})
        await asyncio.sleep(0.05)
        js = nc.jetstream.return_value
        payload = js.publish.call_args.args[1]
        assert b"operator_interaction" in payload
        assert b"button_press" in payload
        await recorder.stop()

    async def test_record_scheduler_decision(self) -> None:
        """record_scheduler_decision builds a scheduler_decision event."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        await recorder.start()
        await recorder.record_scheduler_decision("reschedule", {"reason": "resource_free"})
        await asyncio.sleep(0.05)
        js = nc.jetstream.return_value
        payload = js.publish.call_args.args[1]
        assert b"scheduler_decision" in payload
        await recorder.stop()

    async def test_record_nats_message(self) -> None:
        """record_nats_message builds a nats_message event."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc)
        await recorder.start()
        await recorder.record_nats_message(
            "ate.status.run-1.STEP_STARTED",
            {"step_id": "s1"},
            direction="publish",
        )
        await asyncio.sleep(0.05)
        js = nc.jetstream.return_value
        payload = js.publish.call_args.args[1]
        assert b"nats_message" in payload
        await recorder.stop()

    async def test_event_count_increments(self) -> None:
        """event_count reflects the number of published events."""
        nc = _make_mock_nc()
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc, flush_interval=0.01)
        await recorder.start()
        for i in range(5):
            await recorder.record_step_transition(f"s{i}", "PENDING", "RUNNING")
        await asyncio.sleep(0.2)
        assert recorder.event_count == 5
        await recorder.stop()

    async def test_publish_failure_does_not_increment_count(self) -> None:
        """event_count stays 0 when JetStream publish raises."""
        nc = _make_mock_nc()
        nc.jetstream.return_value.publish = AsyncMock(side_effect=Exception("NATS down"))
        recorder = ExecutionRecorder(session_id="run-1", nats_client=nc, flush_interval=0.01)
        await recorder.start()
        await recorder.record_step_transition("s1", "PENDING", "RUNNING")
        await asyncio.sleep(0.05)
        assert recorder.event_count == 0
        await recorder.stop()
