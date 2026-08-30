"""Unit tests for WatchDog health monitor in ATE Platform.

Tests cover:
- Normal operation: heartbeat increments, no alarm
- Heartbeat lost: 3 missed heartbeats → HEARTBEAT_LOST alarm + emergency shutdown
- Deadlock detection: 100 consecutive no-progress → DEADLOCK_DETECTED alarm
- Start/stop lifecycle: task created on start, cancelled on stop
- Recovery: heartbeat resumes after transient misses
"""

import asyncio

import pytest

from ate_platform.scheduler.event_bus import Event, EventBus, EventType
from ate_platform.scheduler.watchdog import WatchDog
from shared.events import HeartbeatLostData

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def heartbeat_counter():
    """Returns a mutable counter and a callable that reads it."""
    counter = {"value": 0}

    def get() -> int:
        return counter["value"]

    return counter, get


# ── Lifecycle Tests ────────────────────────────────────────────────────


class TestWatchDogLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_task(self, heartbeat_counter) -> None:
        """start() should create an asyncio task."""
        _, get_heartbeat = heartbeat_counter
        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.1,
        )

        watchdog.start()

        assert watchdog.is_running is True
        assert watchdog._task is not None

        await watchdog.stop()

    @pytest.mark.asyncio
    async def test_start_idempotent(self, heartbeat_counter) -> None:
        """Multiple start() calls should be safe."""
        _, get_heartbeat = heartbeat_counter
        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.1,
        )

        watchdog.start()
        task1 = watchdog._task
        watchdog.start()
        task2 = watchdog._task

        assert task1 is task2

        await watchdog.stop()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self, heartbeat_counter) -> None:
        """stop() when not running should be safe."""
        _, get_heartbeat = heartbeat_counter
        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.1,
        )

        await watchdog.stop()  # Should not raise

        assert watchdog.is_running is False

    @pytest.mark.asyncio
    async def test_stop_clears_running_flag(self, heartbeat_counter) -> None:
        """stop() should set running to False and cancel task."""
        _, get_heartbeat = heartbeat_counter
        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.1,
        )

        watchdog.start()
        assert watchdog.is_running is True

        await watchdog.stop()
        assert watchdog.is_running is False
        assert watchdog._task is None

    @pytest.mark.asyncio
    async def test_task_is_cancelled_on_stop(self, heartbeat_counter) -> None:
        """The watchdog task should be cancelled on stop()."""
        _, get_heartbeat = heartbeat_counter
        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.1,
        )

        watchdog.start()
        task = watchdog._task
        assert task is not None
        assert not task.done()

        await watchdog.stop()
        # After stop, task should be None (cleaned up)
        assert watchdog._task is None


# ── Normal Operation Tests ──────────────────────────────────────────────


class TestWatchDogNormalOperation:
    """Tests for normal heartbeat monitoring."""

    @pytest.mark.asyncio
    async def test_heartbeat_incrementing_no_alarm(self, heartbeat_counter) -> None:
        """When heartbeat increments, no alarm should be raised."""
        counter, get_heartbeat = heartbeat_counter
        event_bus = EventBus()

        received_alarms: list[Event] = []

        def handler(event: Event) -> None:
            received_alarms.append(event)

        event_bus.subscribe(EventType.HEARTBEAT_LOST, handler)

        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.05,
            event_bus=event_bus,
        )

        await event_bus.start()
        watchdog.start()

        # Simulate heartbeat incrementing over time
        for _ in range(10):
            counter["value"] += 1
            await asyncio.sleep(0.06)

        await watchdog.stop()
        await event_bus.stop()

        # No HEARTBEAT_LOST alarms should have been fired
        assert len(received_alarms) == 0

    @pytest.mark.asyncio
    async def test_consecutive_misses_reset_on_recovery(self, heartbeat_counter) -> None:
        """Consecutive misses should reset when heartbeat resumes."""
        counter, get_heartbeat = heartbeat_counter
        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.05,
        )

        watchdog.start()

        # Let it run with 2 misses (below threshold)
        counter["value"] = 1
        await asyncio.sleep(0.12)  # ~2 checks, misses should be 2
        assert watchdog.consecutive_misses <= 2

        # Resume heartbeat
        counter["value"] = 2
        await asyncio.sleep(0.06)
        assert watchdog.consecutive_misses == 0

        await watchdog.stop()

    @pytest.mark.asyncio
    async def test_initial_snapshot_prevents_immediate_alarm(self, heartbeat_counter) -> None:
        """WatchDog should snapshot initial heartbeat and not alarm immediately."""
        counter, get_heartbeat = heartbeat_counter
        event_bus = EventBus()

        received_alarms: list[Event] = []

        def handler(event: Event) -> None:
            received_alarms.append(event)

        event_bus.subscribe(EventType.HEARTBEAT_LOST, handler)

        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.1,
            event_bus=event_bus,
        )

        await event_bus.start()

        # Start watchdog with counter at 0, don't increment
        counter["value"] = 0
        watchdog.start()

        # Wait for exactly 2 checks: first check at t≈0, second at t≈0.1.
        # Sleep 0.19s ensures the 3rd check (at t≈0.2) doesn't fire yet.
        await asyncio.sleep(0.19)

        await watchdog.stop()
        await event_bus.stop()

        # No alarm should fire for 2 misses (threshold is 3)
        assert len(received_alarms) == 0


# ── Heartbeat Lost Tests ────────────────────────────────────────────────


class TestWatchDogHeartbeatLost:
    """Tests for heartbeat lost detection."""

    @pytest.mark.asyncio
    async def test_three_missed_heartbeats_triggers_alarm(self, heartbeat_counter) -> None:
        """3 consecutive missed heartbeats should trigger HEARTBEAT_LOST alarm."""
        counter, get_heartbeat = heartbeat_counter
        event_bus = EventBus()

        received_alarms: list[Event] = []

        def handler(event: Event) -> None:
            received_alarms.append(event)

        event_bus.subscribe(EventType.HEARTBEAT_LOST, handler)

        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.05,
            event_bus=event_bus,
        )

        await event_bus.start()

        # Start with counter at 0
        counter["value"] = 0
        watchdog.start()

        # Don't increment — wait for 3+ checks
        await asyncio.sleep(0.25)

        await watchdog.stop()
        await event_bus.stop()

        # Should have received at least 1 HEARTBEAT_LOST alarm
        assert len(received_alarms) >= 1
        assert received_alarms[0].type == EventType.HEARTBEAT_LOST
        assert received_alarms[0].data["severity"] == "critical"
        assert received_alarms[0].data["recoverable"] is False
        assert received_alarms[0].data["missed_checks"] >= 3

    @pytest.mark.asyncio
    async def test_heartbeat_lost_calls_emergency_shutdown(self, heartbeat_counter) -> None:
        """HEARTBEAT_LOST should call emergency_shutdown_callback."""
        counter, get_heartbeat = heartbeat_counter

        shutdown_called = {"called": False}

        async def emergency_shutdown() -> None:
            shutdown_called["called"] = True

        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.05,
            emergency_shutdown_callback=emergency_shutdown,
        )

        # Start with counter at 0
        counter["value"] = 0
        watchdog.start()

        # Don't increment — wait for 3+ checks
        await asyncio.sleep(0.25)

        await watchdog.stop()

        assert shutdown_called["called"] is True

    @pytest.mark.asyncio
    async def test_heartbeat_lost_sync_callback(self, heartbeat_counter) -> None:
        """HEARTBEAT_LOST should handle sync emergency_shutdown_callback."""
        counter, get_heartbeat = heartbeat_counter

        shutdown_called = {"called": False}

        def emergency_shutdown() -> None:
            shutdown_called["called"] = True

        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.05,
            emergency_shutdown_callback=emergency_shutdown,
        )

        counter["value"] = 0
        watchdog.start()

        await asyncio.sleep(0.25)

        await watchdog.stop()

        assert shutdown_called["called"] is True

    @pytest.mark.asyncio
    async def test_heartbeat_lost_event_data(self, heartbeat_counter) -> None:
        """HEARTBEAT_LOST event should contain correct metadata."""
        counter, get_heartbeat = heartbeat_counter
        event_bus = EventBus()

        received_alarms: list[Event] = []

        def handler(event: Event) -> None:
            received_alarms.append(event)

        event_bus.subscribe(EventType.HEARTBEAT_LOST, handler)

        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.05,
            event_bus=event_bus,
        )

        await event_bus.start()

        counter["value"] = 0
        watchdog.start()

        await asyncio.sleep(0.25)

        await watchdog.stop()
        await event_bus.stop()

        assert len(received_alarms) >= 1
        alarm = received_alarms[0]
        assert "last_heartbeat" in alarm.data
        assert "missed_checks" in alarm.data
        assert "scan_interval" in alarm.data
        assert alarm.data["scan_interval"] == 0.05

    @pytest.mark.asyncio
    async def test_no_alarm_when_event_bus_is_none(self, heartbeat_counter) -> None:
        """WatchDog should not crash when event_bus is None."""
        counter, get_heartbeat = heartbeat_counter

        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.05,
            event_bus=None,
        )

        counter["value"] = 0
        watchdog.start()

        # Wait for heartbeat lost (no event_bus — should not crash)
        await asyncio.sleep(0.25)

        await watchdog.stop()
        # Should not have crashed
        assert watchdog.is_running is False


# ── Deadlock Detection Tests ───────────────────────────────────────────


class TestWatchDogDeadlockDetection:
    """Tests for deadlock detection (100 consecutive no-progress checks)."""

    @pytest.mark.asyncio
    async def test_deadlock_detection_emits_event(self, heartbeat_counter) -> None:
        """100 consecutive no-progress checks should emit DEADLOCK_DETECTED."""
        counter, get_heartbeat = heartbeat_counter
        event_bus = EventBus()

        received_events: list[Event] = []

        def handler(event: Event) -> None:
            received_events.append(event)

        event_bus.subscribe(EventType.DEADLOCK_DETECTED, handler)

        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.005,  # Very fast for testing
            event_bus=event_bus,
        )

        # Override deadlock threshold to a low value for testing
        watchdog.DEADLOCK_THRESHOLD = 10

        await event_bus.start()

        counter["value"] = 0
        watchdog.start()

        # Wait for 10+ checks with no progress
        await asyncio.sleep(0.15)

        await watchdog.stop()
        await event_bus.stop()

        # Should have received at least 1 DEADLOCK_DETECTED event
        deadlock_events = [
            e for e in received_events if e.type == EventType.DEADLOCK_DETECTED
        ]
        assert len(deadlock_events) >= 1

    @pytest.mark.asyncio
    async def test_deadlock_counter_resets_after_detection(self, heartbeat_counter) -> None:
        """Deadlock counter should reset after emitting DEADLOCK_DETECTED."""
        counter, get_heartbeat = heartbeat_counter

        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.005,
        )

        watchdog.DEADLOCK_THRESHOLD = 10

        counter["value"] = 0
        watchdog.start()

        # Wait for deadlock detection
        await asyncio.sleep(0.1)

        # Counter should have been reset after detection
        assert watchdog.consecutive_no_progress < watchdog.DEADLOCK_THRESHOLD

        await watchdog.stop()

    @pytest.mark.asyncio
    async def test_no_deadlock_when_heartbeat_increments(self, heartbeat_counter) -> None:
        """Deadlock should NOT fire when heartbeat increments regularly."""
        counter, get_heartbeat = heartbeat_counter
        event_bus = EventBus()

        received_events: list[Event] = []

        def handler(event: Event) -> None:
            received_events.append(event)

        event_bus.subscribe(EventType.DEADLOCK_DETECTED, handler)

        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.01,
            event_bus=event_bus,
        )

        watchdog.DEADLOCK_THRESHOLD = 15

        await event_bus.start()

        counter["value"] = 0
        watchdog.start()

        # Increment heartbeat every few checks
        for _ in range(5):
            await asyncio.sleep(0.04)
            counter["value"] += 1

        await watchdog.stop()
        await event_bus.stop()

        # No deadlock should be detected
        deadlock_events = [
            e for e in received_events if e.type == EventType.DEADLOCK_DETECTED
        ]
        assert len(deadlock_events) == 0


# ── Integration Tests ───────────────────────────────────────────────────


class TestWatchDogProperties:
    """Tests for WatchDog properties and state."""

    def test_default_thresholds(self) -> None:
        """WatchDog should have expected default threshold constants."""
        assert WatchDog.HEARTBEAT_LOST_THRESHOLD == 3
        assert WatchDog.DEADLOCK_THRESHOLD == 100

    @pytest.mark.asyncio
    async def test_consecutive_misses_property(self, heartbeat_counter) -> None:
        """consecutive_misses property should reflect current state."""
        counter, get_heartbeat = heartbeat_counter

        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.05,
        )

        counter["value"] = 0
        watchdog.start()

        # Wait for a few misses
        await asyncio.sleep(0.12)
        assert watchdog.consecutive_misses > 0

        # Resume heartbeat
        counter["value"] += 1
        await asyncio.sleep(0.07)
        assert watchdog.consecutive_misses == 0

        await watchdog.stop()

    @pytest.mark.asyncio
    async def test_is_running_property(self, heartbeat_counter) -> None:
        """is_running should track lifecycle."""
        _, get_heartbeat = heartbeat_counter

        watchdog = WatchDog(
            heartbeat_counter=get_heartbeat,
            scan_interval=0.1,
        )

        assert watchdog.is_running is False

        watchdog.start()
        assert watchdog.is_running is True

        await watchdog.stop()
        assert watchdog.is_running is False


# ── HeartbeatLostData Tests ────────────────────────────────────────────


class TestHeartbeatLostData:
    """Tests for HeartbeatLostData event data class."""

    def test_defaults(self) -> None:
        """HeartbeatLostData should have expected defaults."""
        data = HeartbeatLostData()
        assert data.last_heartbeat == 0
        assert data.missed_checks == 0
        assert data.scan_interval == 0.0
        assert data.severity == "critical"
        assert data.recoverable is False
        assert data.run_id is None

    def test_custom_values(self) -> None:
        """HeartbeatLostData should accept custom values."""
        data = HeartbeatLostData(
            last_heartbeat=42,
            missed_checks=5,
            scan_interval=5.0,
            severity="critical",
            recoverable=False,
            run_id="run-123",
        )
        assert data.last_heartbeat == 42
        assert data.missed_checks == 5
        assert data.scan_interval == 5.0
        assert data.severity == "critical"
        assert data.recoverable is False
        assert data.run_id == "run-123"


# ── EventType Enum Test ─────────────────────────────────────────────────


class TestEventTypeHeartbeatLost:
    """Tests for HEARTBEAT_LOST in EventType enum."""

    def test_heartbeat_lost_exists(self) -> None:
        """EventType should have HEARTBEAT_LOST member."""
        assert hasattr(EventType, "HEARTBEAT_LOST")
        assert EventType.HEARTBEAT_LOST.value == "HEARTBEAT_LOST"

    def test_heartbeat_lost_is_alarm(self) -> None:
        """HEARTBEAT_LOST should be in the ALARM category."""
        from shared.events import EventCategory, get_event_category
        assert get_event_category(EventType.HEARTBEAT_LOST) == EventCategory.ALARM
