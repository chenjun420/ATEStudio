"""Tests for ReplayExecutor (recorder.replay_executor).

Covers:
- In-memory replay (replay_from_events, replay_from_events_iter)
- JetStream replay (replay) with mock NATS
- Pause/resume during replay
- SSE-formatted output (replay_sse_from_events)
- Helper methods: compute_step_durations, compute_diff, JSONL round-trip
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from ate_platform.recorder.replay_executor import ReplayExecutor
from ate_platform.recorder.types import RecordedEvent, RecordedEventType


def _make_event(
    event_type: RecordedEventType,
    session_id: str,
    timestamp: datetime,
    step_id: str | None = None,
    data: dict[str, object] | None = None,
) -> RecordedEvent:
    """Build a RecordedEvent with an explicit timestamp."""
    return RecordedEvent(
        timestamp=timestamp,
        event_type=event_type,
        session_id=session_id,
        step_id=step_id,
        data=data or {},
    )


def _make_sorted_events(session_id: str = "run-1") -> list[RecordedEvent]:
    """Build a small sorted list of events with 0.01s gaps."""
    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    return [
        _make_event(
            RecordedEventType.STEP_TRANSITION, session_id, base,
            step_id="s1", data={"from_status": "PENDING", "to_status": "RUNNING"},
        ),
        _make_event(
            RecordedEventType.MEASUREMENT_RESULT, session_id, base + timedelta(seconds=1),
            step_id="s1", data={"name": "voltage", "value": 5.0, "unit": "V"},
        ),
        _make_event(
            RecordedEventType.STEP_TRANSITION, session_id, base + timedelta(seconds=2),
            step_id="s1", data={"from_status": "RUNNING", "to_status": "PASSED"},
        ),
    ]


def _make_mock_nc_with_events(events: list[RecordedEvent]) -> MagicMock:
    """Build a mock NATS client whose pull_subscribe returns pre-loaded events."""
    msgs = []
    for e in events:
        msg = MagicMock()
        msg.data = e.to_jsonl().encode("utf-8")
        msg.ack = AsyncMock()
        msgs.append(msg)

    psub = MagicMock()
    psub.fetch = AsyncMock(return_value=msgs)
    psub.unsubscribe = AsyncMock()

    js = MagicMock()
    js.pull_subscribe = AsyncMock(return_value=psub)

    nc = MagicMock()
    nc.is_connected = True
    nc.jetstream = MagicMock(return_value=js)
    return nc


def _make_mock_nc_empty() -> MagicMock:
    """Build a mock NATS client with no recorded events."""
    psub = MagicMock()
    psub.fetch = AsyncMock(side_effect=TimeoutError())
    psub.unsubscribe = AsyncMock()

    js = MagicMock()
    js.pull_subscribe = AsyncMock(return_value=psub)

    nc = MagicMock()
    nc.is_connected = True
    nc.jetstream = MagicMock(return_value=js)
    return nc


class TestReplayExecutorInit:
    """Tests for initialization and properties."""

    def test_session_id_property(self) -> None:
        """session_id returns the constructor value."""
        executor = ReplayExecutor(session_id="run-1")
        assert executor.session_id == "run-1"

    def test_subject_property(self) -> None:
        """subject is formatted with session_id."""
        executor = ReplayExecutor(session_id="run-42")
        assert executor.subject == "ate.execution.run-42.events"

    def test_not_paused_initially(self) -> None:
        """is_paused is False before pause() is called."""
        executor = ReplayExecutor(session_id="run-1")
        assert executor.is_paused is False


class TestReplayFromEvents:
    """Tests for in-memory replay (no NATS needed)."""

    async def test_replay_returns_sorted_events(self) -> None:
        """replay_from_events returns all events in timestamp order."""
        events = _make_sorted_events()
        # Reverse them to verify sorting
        executor = ReplayExecutor(session_id="run-1")
        result = await executor.replay_from_events(list(reversed(events)), speed_multiplier=100.0)
        assert len(result) == 3
        assert result[0].timestamp <= result[1].timestamp <= result[2].timestamp

    async def test_replay_invokes_callback(self) -> None:
        """replay_from_events calls the callback for each event."""
        events = _make_sorted_events()
        received: list[RecordedEvent] = []

        async def callback(event: RecordedEvent) -> None:
            received.append(event)

        executor = ReplayExecutor(session_id="run-1")
        await executor.replay_from_events(events, speed_multiplier=100.0, callback=callback)
        assert len(received) == 3

    async def test_replay_invalid_speed_raises(self) -> None:
        """replay_from_events raises ValueError for speed_multiplier <= 0."""
        executor = ReplayExecutor(session_id="run-1")
        with pytest.raises(ValueError, match="speed_multiplier"):
            await executor.replay_from_events([], speed_multiplier=0.0)

    async def test_replay_iter_yields_events(self) -> None:
        """replay_from_events_iter yields events in order."""
        events = _make_sorted_events()
        executor = ReplayExecutor(session_id="run-1")
        result = []
        async for event in executor.replay_from_events_iter(events, speed_multiplier=100.0):
            result.append(event)
        assert len(result) == 3
        assert result[0].step_id == "s1"

    async def test_replay_respects_speed_multiplier(self) -> None:
        """Higher speed_multiplier reduces delay between events."""
        events = _make_sorted_events()  # 1s and 1s gaps
        executor = ReplayExecutor(session_id="run-1")

        # At 100x speed, 1s gap -> 0.01s sleep; total ~0.02s
        import time as time_mod
        start = time_mod.monotonic()
        await executor.replay_from_events(events, speed_multiplier=100.0)
        elapsed_fast = time_mod.monotonic() - start

        # Verify it was fast (well under the 2s real-time gap)
        assert elapsed_fast < 0.5


class TestReplayCancel:
    """Tests for cancel() during replay."""

    async def test_cancel_stops_replay(self) -> None:
        """cancel() stops the replay after the current event."""
        events = _make_sorted_events()
        executor = ReplayExecutor(session_id="run-1")
        # Cancel immediately - should stop after first event
        executor.cancel()
        result = await executor.replay_from_events(events, speed_multiplier=100.0)
        assert len(result) == 0

    async def test_cancel_clears_pause(self) -> None:
        """cancel() also clears the pause flag so a paused replay can terminate."""
        executor = ReplayExecutor(session_id="run-1")
        executor.pause()
        assert executor.is_paused is True
        executor.cancel()
        assert executor.is_paused is False


class TestReplayPauseResume:
    """Tests for pause()/resume() during replay."""

    async def test_pause_blocks_replay(self) -> None:
        """A paused replay blocks until resume() is called."""
        events = _make_sorted_events()
        executor = ReplayExecutor(session_id="run-1")

        # Pause before starting
        executor.pause()
        assert executor.is_paused is True

        # Start replay in background
        task = asyncio.create_task(
            executor.replay_from_events(events, speed_multiplier=100.0)
        )
        await asyncio.sleep(0.05)

        # Task should still be running (blocked on pause)
        assert not task.done()

        # Resume
        executor.resume()
        assert executor.is_paused is False
        result = await task
        assert len(result) == 3

    async def test_resume_is_idempotent(self) -> None:
        """resume() when not paused is a no-op."""
        executor = ReplayExecutor(session_id="run-1")
        executor.resume()  # Not paused
        assert executor.is_paused is False

    async def test_pause_is_idempotent(self) -> None:
        """pause() when already paused is a no-op."""
        executor = ReplayExecutor(session_id="run-1")
        executor.pause()
        executor.pause()
        assert executor.is_paused is True
        executor.resume()

    async def test_pause_resume_mid_replay(self) -> None:
        """Replay continues correctly after a pause/resume cycle mid-stream."""
        base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        events = [
            _make_event(RecordedEventType.STEP_TRANSITION, "run-1", base + timedelta(seconds=i),
                        step_id=f"s{i}", data={"to_status": "RUNNING"})
            for i in range(5)
        ]
        executor = ReplayExecutor(session_id="run-1")
        received: list[RecordedEvent] = []

        async def callback(event: RecordedEvent) -> None:
            received.append(event)
            # Pause after the 2nd event
            if len(received) == 2:
                executor.pause()
                # Resume shortly after
                await asyncio.sleep(0.05)
                executor.resume()

        await executor.replay_from_events(events, speed_multiplier=100.0, callback=callback)
        assert len(received) == 5


class TestReplaySSE:
    """Tests for SSE-formatted replay output."""

    async def test_replay_sse_from_events_yields_dicts(self) -> None:
        """replay_sse_from_events yields dicts with event/data/id keys."""
        events = _make_sorted_events()
        executor = ReplayExecutor(session_id="run-1")
        results = []
        async for sse_dict in executor.replay_sse_from_events(events, speed_multiplier=100.0):
            results.append(sse_dict)
        assert len(results) == 3
        for i, d in enumerate(results):
            assert "event" in d
            assert "data" in d
            assert "id" in d
            assert d["id"] == f"run-1-replay-{i + 1}"

    async def test_replay_sse_data_is_valid_json(self) -> None:
        """The data field in SSE dicts is a valid JSON string."""
        events = _make_sorted_events()
        executor = ReplayExecutor(session_id="run-1")
        async for sse_dict in executor.replay_sse_from_events(events, speed_multiplier=100.0):
            parsed = json.loads(sse_dict["data"])
            assert parsed["session_id"] == "run-1"
            assert "event_type" in parsed
            assert "timestamp" in parsed

    async def test_replay_sse_event_field_matches_type(self) -> None:
        """The event field carries the recorded event type value."""
        events = _make_sorted_events()
        executor = ReplayExecutor(session_id="run-1")
        results = []
        async for sse_dict in executor.replay_sse_from_events(events, speed_multiplier=100.0):
            results.append(sse_dict)
        assert results[0]["event"] == "step_transition"
        assert results[1]["event"] == "measurement_result"

    async def test_replay_sse_invalid_speed_raises(self) -> None:
        """replay_sse_from_events raises ValueError for speed_multiplier <= 0."""
        executor = ReplayExecutor(session_id="run-1")
        with pytest.raises(ValueError, match="speed_multiplier"):
            async for _ in executor.replay_sse_from_events([], speed_multiplier=0.0):
                pass

    async def test_replay_sse_respects_pause(self) -> None:
        """replay_sse_from_events blocks while paused."""
        events = _make_sorted_events()
        executor = ReplayExecutor(session_id="run-1")
        executor.pause()

        task = asyncio.create_task(
            _collect_sse(executor.replay_sse_from_events(events, speed_multiplier=100.0))
        )
        await asyncio.sleep(0.05)
        assert not task.done()
        executor.resume()
        results = await task
        assert len(results) == 3


async def _collect_sse(aiter: AsyncIterator[dict[str, object]]) -> list[dict[str, object]]:
    """Collect all items from an async iterator into a list."""
    results: list[dict[str, object]] = []
    async for item in aiter:
        results.append(item)
    return results


class TestReplayFromJetStream:
    """Tests for replay() reading from a mock JetStream."""

    async def test_replay_from_jetstream(self) -> None:
        """replay() loads events from JetStream and returns them sorted."""
        events = _make_sorted_events()
        nc = _make_mock_nc_with_events(events)
        executor = ReplayExecutor(session_id="run-1", nats_client=nc)
        result = await executor.replay(speed_multiplier=100.0)
        assert len(result) == 3
        assert result[0].timestamp <= result[1].timestamp

    async def test_replay_from_jetstream_empty(self) -> None:
        """replay() returns empty list when no events exist."""
        nc = _make_mock_nc_empty()
        executor = ReplayExecutor(session_id="run-1", nats_client=nc)
        result = await executor.replay(speed_multiplier=100.0)
        assert result == []

    async def test_replay_without_nats_raises(self) -> None:
        """replay() raises RuntimeError when no NATS client is provided."""
        executor = ReplayExecutor(session_id="run-1")
        with pytest.raises(RuntimeError, match="No NATS client"):
            await executor.replay(speed_multiplier=100.0)

    async def test_replay_sse_from_jetstream(self) -> None:
        """replay_sse() yields SSE dicts from JetStream events."""
        events = _make_sorted_events()
        nc = _make_mock_nc_with_events(events)
        executor = ReplayExecutor(session_id="run-1", nats_client=nc)
        results = []
        async for sse_dict in executor.replay_sse(speed_multiplier=100.0):
            results.append(sse_dict)
        assert len(results) == 3


class TestComputeStepDurations:
    """Tests for the compute_step_durations static method."""

    def test_compute_step_durations(self) -> None:
        """Pairs RUNNING transitions with their completion transitions."""
        base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        events = [
            _make_event(RecordedEventType.STEP_TRANSITION, "r", base,
                        step_id="s1", data={"to_status": "RUNNING"}),
            _make_event(RecordedEventType.STEP_TRANSITION, "r", base + timedelta(seconds=2),
                        step_id="s1", data={"to_status": "PASSED"}),
            _make_event(RecordedEventType.STEP_TRANSITION, "r", base + timedelta(seconds=3),
                        step_id="s2", data={"to_status": "RUNNING"}),
            _make_event(RecordedEventType.STEP_TRANSITION, "r", base + timedelta(seconds=5),
                        step_id="s2", data={"to_status": "FAILED"}),
        ]
        durations = ReplayExecutor.compute_step_durations(events)
        assert durations["s1"] == 2.0
        assert durations["s2"] == 2.0

    def test_compute_step_durations_ignores_non_transitions(self) -> None:
        """Non-step_transition events are ignored."""
        base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        events = [
            _make_event(RecordedEventType.MEASUREMENT_RESULT, "r", base, step_id="s1"),
        ]
        durations = ReplayExecutor.compute_step_durations(events)
        assert durations == {}


class TestComputeDiff:
    """Tests for the compute_diff static method."""

    def test_compute_diff_identical(self) -> None:
        """Identical sequences produce no added/removed/changed."""
        events = _make_sorted_events()
        diff = ReplayExecutor.compute_diff(events, events)
        assert diff["summary"]["added"] == 0
        assert diff["summary"]["removed"] == 0
        assert diff["summary"]["changed"] == 0

    def test_compute_diff_added(self) -> None:
        """Events in replayed but not original show as added."""
        original = _make_sorted_events()
        replayed = _make_sorted_events()
        replayed.append(
            _make_event(RecordedEventType.NATS_MESSAGE, "run-1",
                        datetime(2024, 1, 1, 12, 0, 5, tzinfo=UTC),
                        data={"subject": "ate.test"})
        )
        diff = ReplayExecutor.compute_diff(original, replayed)
        assert diff["summary"]["added"] == 1
        assert diff["summary"]["removed"] == 0

    def test_compute_diff_removed(self) -> None:
        """Events in original but not replayed show as removed."""
        base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        original = [
            _make_event(RecordedEventType.STEP_TRANSITION, "r", base,
                        step_id="s1", data={"to_status": "RUNNING"}),
            _make_event(RecordedEventType.MEASUREMENT_RESULT, "r", base + timedelta(seconds=1),
                        step_id="s1", data={"name": "v", "value": 1.0}),
            _make_event(RecordedEventType.NATS_MESSAGE, "r", base + timedelta(seconds=2),
                        step_id="s2", data={"subject": "ate.x"}),
        ]
        replayed = original[:2]
        diff = ReplayExecutor.compute_diff(original, replayed)
        assert diff["summary"]["removed"] == 1
        assert diff["summary"]["added"] == 0

    def test_compute_diff_changed(self) -> None:
        """Same key with different data shows as changed."""
        original = _make_sorted_events()
        replayed = _make_sorted_events()
        # Modify the data of the middle event (same step_id + event_type)
        replayed[1] = _make_event(
            RecordedEventType.MEASUREMENT_RESULT, "run-1",
            datetime(2024, 1, 1, 12, 0, 1, tzinfo=UTC),
            step_id="s1", data={"name": "voltage", "value": 99.0, "unit": "V"},
        )
        diff = ReplayExecutor.compute_diff(original, replayed)
        assert diff["summary"]["changed"] == 1


class TestJsonlHelpers:
    """Tests for events_to_jsonl / events_from_jsonl static methods."""

    def test_events_to_jsonl_round_trip(self) -> None:
        """events_to_jsonl and events_from_jsonl are inverse operations."""
        events = _make_sorted_events()
        text = ReplayExecutor.events_to_jsonl(events)
        restored = ReplayExecutor.events_from_jsonl(text)
        assert len(restored) == len(events)
        for orig, rest in zip(events, restored, strict=True):
            assert orig.event_type == rest.event_type
            assert orig.session_id == rest.session_id

    def test_events_from_jsonl_skips_empty_lines(self) -> None:
        """events_from_jsonl ignores empty lines."""
        events = _make_sorted_events()
        text = ReplayExecutor.events_to_jsonl(events) + "\n\n\n"
        restored = ReplayExecutor.events_from_jsonl(text)
        assert len(restored) == len(events)

    def test_serialize_for_api_returns_sorted(self) -> None:
        """serialize_for_api returns JSON dicts in timestamp order."""
        events = list(reversed(_make_sorted_events()))
        result = ReplayExecutor.serialize_for_api(events)
        assert len(result) == 3
        assert result[0]["timestamp"] <= result[1]["timestamp"]

    def test_parse_payload_data_string(self) -> None:
        """parse_payload_data parses a JSON string into a dict."""
        result = ReplayExecutor.parse_payload_data('{"key": "value"}')
        assert result == {"key": "value"}

    def test_parse_payload_data_bytes(self) -> None:
        """parse_payload_data parses JSON bytes into a dict."""
        result = ReplayExecutor.parse_payload_data(b'{"key": 42}')
        assert result == {"key": 42}
