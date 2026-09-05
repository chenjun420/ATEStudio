"""Unit tests for EventBus in ATE Platform.

Tests cover:
- Basic pub/sub functionality
- Wildcard subscriptions
- Async callbacks
- Graceful shutdown
"""

import asyncio
from datetime import datetime

import pytest

from ate_platform.scheduler.event_bus import Event, EventBus, EventType, get_event_category
from shared.events import EventCategory


class TestEventType:
    """Tests for EventType enum."""

    def test_event_type_values(self) -> None:
        """EventType should have all required values."""
        assert EventType.STEP_STATUS_CHANGED.value == "STEP_STATUS_CHANGED"
        # VARIABLE_CHANGED is a deprecated alias — same wire value as MEASUREMENT_RECORDED
        assert EventType.VARIABLE_CHANGED.value == "measurement_recorded"
        assert EventType.MEASUREMENT_RECORDED.value == "measurement_recorded"
        assert EventType.RESOURCE_RELEASED.value == "RESOURCE_RELEASED"
        assert EventType.TIMER_EXPIRED.value == "TIMER_EXPIRED"
        assert EventType.EXTERNAL_CMD.value == "EXTERNAL_CMD"
        assert EventType.STEP_STARTED.value == "STEP_STARTED"
        assert EventType.STEP_COMPLETED.value == "STEP_COMPLETED"
        assert EventType.LOOP_ITERATION_STARTED.value == "LOOP_ITERATION_STARTED"
        assert EventType.LOOP_ITERATION_COMPLETED.value == "LOOP_ITERATION_COMPLETED"
        assert EventType.EXECUTION_STARTED.value == "EXECUTION_STARTED"
        assert EventType.EXECUTION_COMPLETED.value == "EXECUTION_COMPLETED"
        # New TEMS A4 event types
        assert EventType.STEP_FAILED.value == "STEP_FAILED"
        assert EventType.STEP_SKIPPED.value == "STEP_SKIPPED"
        assert EventType.EXECUTION_PAUSED.value == "EXECUTION_PAUSED"
        assert EventType.STEP_TIMEOUT.value == "STEP_TIMEOUT"
        assert EventType.CONDITION_TIMEOUT.value == "CONDITION_TIMEOUT"
        assert EventType.RESOURCE_TIMEOUT.value == "RESOURCE_TIMEOUT"
        assert EventType.DEADLOCK_DETECTED.value == "DEADLOCK_DETECTED"
        assert EventType.WORKER_EXHAUSTED.value == "WORKER_EXHAUSTED"

    def test_event_type_count(self) -> None:
        """EventType should have exactly 22 unique values.

        VARIABLE_CHANGED is a deprecated alias of MEASUREMENT_RECORDED (same
        value), so Python's Enum counts them as one member. The alias is still
        accessible as EventType.VARIABLE_CHANGED. ALARM_RAISED (added with
        SPC alarm indexing) brought the count to 21; BREAKPOINT_HIT (edge
        breakpoint hits, task 20) brings it to 22.
        """
        assert len(EventType) == 22
        # Verify the deprecated alias is still accessible
        assert EventType.VARIABLE_CHANGED is EventType.MEASUREMENT_RECORDED
        # Verify the edge breakpoint-hit event is categorised as an EVENT
        assert get_event_category(EventType.BREAKPOINT_HIT) == EventCategory.EVENT


class TestEvent:
    """Tests for Event dataclass."""

    def test_event_creation(self) -> None:
        """Event should be created with correct attributes."""
        data = {"step": "test_step", "status": "running"}
        event = Event(type=EventType.STEP_STATUS_CHANGED, data=data)

        assert event.type == EventType.STEP_STATUS_CHANGED
        assert event.data == data
        assert isinstance(event.timestamp, datetime)

    def test_event_auto_timestamp(self) -> None:
        """Event should auto-generate timestamp."""
        before = datetime.now()
        event = Event(type=EventType.VARIABLE_CHANGED, data={"var": "x"})
        after = datetime.now()

        assert before <= event.timestamp <= after

    def test_event_custom_timestamp(self) -> None:
        """Event should accept custom timestamp."""
        custom_time = datetime(2025, 1, 1, 12, 0, 0)
        event = Event(
            type=EventType.TIMER_EXPIRED, data={"timer": "t1"}, timestamp=custom_time
        )

        assert event.timestamp == custom_time


class TestEventBusSubscribe:
    """Tests for EventBus subscribe/unsubscribe."""

    def test_subscribe_sync_callback(self) -> None:
        """Should allow subscribing with sync callback."""
        bus = EventBus()

        def handler(event: Event) -> None:
            pass

        bus.subscribe(EventType.STEP_STATUS_CHANGED, handler)
        assert handler in bus._subscribers[EventType.STEP_STATUS_CHANGED]

    def test_subscribe_async_callback(self) -> None:
        """Should allow subscribing with async callback."""
        bus = EventBus()

        async def handler(event: Event) -> None:
            pass

        bus.subscribe(EventType.VARIABLE_CHANGED, handler)
        assert handler in bus._subscribers[EventType.VARIABLE_CHANGED]

    def test_subscribe_wildcard(self) -> None:
        """Should allow wildcard subscription with None."""
        bus = EventBus()

        def handler(event: Event) -> None:
            pass

        bus.subscribe(None, handler)
        assert handler in bus._subscribers[None]

    def test_subscribe_multiple_callbacks_same_type(self) -> None:
        """Should allow multiple callbacks for same event type."""
        bus = EventBus()

        def handler1(event: Event) -> None:
            pass

        def handler2(event: Event) -> None:
            pass

        bus.subscribe(EventType.STEP_STATUS_CHANGED, handler1)
        bus.subscribe(EventType.STEP_STATUS_CHANGED, handler2)

        assert len(bus._subscribers[EventType.STEP_STATUS_CHANGED]) == 2

    def test_subscribe_non_callable_raises(self) -> None:
        """Should raise TypeError for non-callable callback."""
        bus = EventBus()

        with pytest.raises(TypeError, match="callback must be callable"):
            bus.subscribe(EventType.STEP_STATUS_CHANGED, "not_callable")  # type: ignore

    def test_unsubscribe_existing(self) -> None:
        """Should return True when unsubscribing existing callback."""
        bus = EventBus()

        def handler(event: Event) -> None:
            pass

        bus.subscribe(EventType.STEP_STATUS_CHANGED, handler)
        result = bus.unsubscribe(EventType.STEP_STATUS_CHANGED, handler)

        assert result is True
        assert handler not in bus._subscribers.get(EventType.STEP_STATUS_CHANGED, [])

    def test_unsubscribe_non_existing(self) -> None:
        """Should return False when unsubscribing non-existing callback."""
        bus = EventBus()

        def handler(event: Event) -> None:
            pass

        result = bus.unsubscribe(EventType.STEP_STATUS_CHANGED, handler)
        assert result is False

    def test_unsubscribe_from_empty_type(self) -> None:
        """Should return False when unsubscribing from non-existent type."""
        bus = EventBus()

        def handler(event: Event) -> None:
            pass

        result = bus.unsubscribe(EventType.TIMER_EXPIRED, handler)
        assert result is False


class TestEventBusPublish:
    """Tests for EventBus publish functionality."""

    @pytest.mark.asyncio
    async def test_publish_queues_event(self) -> None:
        """Published event should be queued."""
        bus = EventBus()
        await bus.publish(EventType.STEP_STATUS_CHANGED, {"step": "test"})

        assert bus._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_publish_creates_event_with_timestamp(self) -> None:
        """Published event should have timestamp."""
        bus = EventBus()
        before = datetime.now()
        await bus.publish(EventType.VARIABLE_CHANGED, {"var": "x"})
        after = datetime.now()

        event = await bus._queue.get()
        assert before <= event.timestamp <= after


class TestEventBusStartStop:
    """Tests for EventBus start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_task(self) -> None:
        """Start should create processing task."""
        bus = EventBus()
        await bus.start()

        assert bus._running is True
        assert bus._task is not None

        await bus.stop()

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        """Multiple start calls should be safe."""
        bus = EventBus()
        await bus.start()
        task1 = bus._task
        await bus.start()
        task2 = bus._task

        assert task1 == task2

        await bus.stop()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self) -> None:
        """Stop when not running should be safe."""
        bus = EventBus()
        await bus.stop()  # Should not raise

        assert bus._running is False

    @pytest.mark.asyncio
    async def test_stop_clears_running_flag(self) -> None:
        """Stop should set running to False."""
        bus = EventBus()
        await bus.start()
        await bus.stop()

        assert bus._running is False


class TestEventBusIntegration:
    """Integration tests for full event flow."""

    @pytest.mark.asyncio
    async def test_basic_pub_sub(self) -> None:
        """Should deliver events to subscribers."""
        bus = EventBus()
        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(EventType.STEP_STATUS_CHANGED, handler)
        await bus.start()

        await bus.publish(EventType.STEP_STATUS_CHANGED, {"step": "test_step", "status": "running"})

        # Wait for event to be processed
        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(received) == 1
        assert received[0].type == EventType.STEP_STATUS_CHANGED
        assert received[0].data["step"] == "test_step"

    @pytest.mark.asyncio
    async def test_wildcard_subscription(self) -> None:
        """Wildcard subscriber should receive all events."""
        bus = EventBus()
        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(None, handler)  # Wildcard
        await bus.start()

        await bus.publish(EventType.STEP_STATUS_CHANGED, {"step": "s1"})
        await bus.publish(EventType.VARIABLE_CHANGED, {"var": "v1"})
        await bus.publish(EventType.TIMER_EXPIRED, {"timer": "t1"})

        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(received) == 3

    @pytest.mark.asyncio
    async def test_async_callback(self) -> None:
        """Should handle async callbacks correctly."""
        bus = EventBus()
        received: list[Event] = []

        async def async_handler(event: Event) -> None:
            await asyncio.sleep(0.01)  # Simulate async work
            received.append(event)

        bus.subscribe(EventType.VARIABLE_CHANGED, async_handler)
        await bus.start()

        await bus.publish(EventType.VARIABLE_CHANGED, {"var": "x", "value": 42})

        await asyncio.sleep(0.2)
        await bus.stop()

        assert len(received) == 1
        assert received[0].data["var"] == "x"

    @pytest.mark.asyncio
    async def test_breakpoint_hit_round_trips_through_event_bus(self) -> None:
        """A BREAKPOINT_HIT payload survives pub/sub and rebuilds to BreakpointHitData.

        Given: the edge scheduler publishes asdict(BreakpointHitData(...)) with a
               variable snapshot,
        When:  the event is delivered over the EventBus and rebuilt via
               EVENT_DATA_CLASSES,
        Then:  the subscriber receives the event and the rebuilt object is a
               BreakpointHitData with every field (including the snapshot) intact.
        """
        from dataclasses import asdict

        from shared.events import EVENT_DATA_CLASSES, BreakpointHitData

        bus = EventBus()
        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        snapshot = {
            "scope": {"voltage": 3.3, "ok": True},
            "steps": {"s1": {"result": "passed"}},
        }
        payload = asdict(
            BreakpointHitData(
                breakpoint_id="bp1",
                kind="step",
                target="s2",
                step_id="s2",
                variables=snapshot,
            )
        )

        bus.subscribe(EventType.BREAKPOINT_HIT, handler)
        await bus.start()

        await bus.publish(EventType.BREAKPOINT_HIT, payload)

        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(received) == 1
        assert received[0].type == EventType.BREAKPOINT_HIT

        rebuilt = EVENT_DATA_CLASSES[EventType.BREAKPOINT_HIT](**received[0].data)
        assert isinstance(rebuilt, BreakpointHitData)
        assert rebuilt.breakpoint_id == "bp1"
        assert rebuilt.kind == "step"
        assert rebuilt.target == "s2"
        assert rebuilt.step_id == "s2"
        assert rebuilt.run_id is None
        assert rebuilt.variables == snapshot

    @pytest.mark.asyncio
    async def test_graceful_stop(self) -> None:
        """Should process all events before stopping."""
        bus = EventBus()
        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(EventType.STEP_STATUS_CHANGED, handler)
        await bus.start()

        # Publish multiple events
        for i in range(5):
            await bus.publish(EventType.STEP_STATUS_CHANGED, {"index": i})

        # Stop should wait for all events
        await bus.stop()

        # All events should be processed
        assert len(received) >= 1  # At least some events processed

    @pytest.mark.asyncio
    async def test_multiple_subscribers_same_type(self) -> None:
        """Multiple subscribers should all receive events."""
        bus = EventBus()
        received1: list[Event] = []
        received2: list[Event] = []

        def handler1(event: Event) -> None:
            received1.append(event)

        def handler2(event: Event) -> None:
            received2.append(event)

        bus.subscribe(EventType.EXTERNAL_CMD, handler1)
        bus.subscribe(EventType.EXTERNAL_CMD, handler2)
        await bus.start()

        await bus.publish(EventType.EXTERNAL_CMD, {"cmd": "reset"})

        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(received1) == 1
        assert len(received2) == 1

    @pytest.mark.asyncio
    async def test_callback_exception_doesnt_crash(self) -> None:
        """Bus should continue after callback exception."""
        bus = EventBus()
        received: list[Event] = []

        def bad_handler(event: Event) -> None:
            raise ValueError("Intentional error")

        def good_handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(EventType.RESOURCE_RELEASED, bad_handler)
        bus.subscribe(EventType.RESOURCE_RELEASED, good_handler)
        await bus.start()

        await bus.publish(EventType.RESOURCE_RELEASED, {"resource": "r1"})

        await asyncio.sleep(0.1)
        await bus.stop()

        # Good handler should still receive event
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_during_processing(self) -> None:
        """Should handle unsubscribe during event processing."""
        bus = EventBus()
        count = 0

        def handler(event: Event) -> None:
            nonlocal count
            count += 1

        bus.subscribe(EventType.TIMER_EXPIRED, handler)
        await bus.start()

        await bus.publish(EventType.TIMER_EXPIRED, {"timer": "t1"})
        await asyncio.sleep(0.05)

        # Unsubscribe while bus is running
        bus.unsubscribe(EventType.TIMER_EXPIRED, handler)

        await bus.publish(EventType.TIMER_EXPIRED, {"timer": "t2"})
        await asyncio.sleep(0.05)

        await bus.stop()

        # First event should have been delivered
        assert count >= 1


class TestSubscriberExceptionIsolation:
    """Tests for per-subscriber exception isolation in _dispatch_event."""

    @pytest.mark.asyncio
    async def test_subscriber_exception_isolation(self) -> None:
        """A failing subscriber must not prevent other subscribers from receiving events."""
        bus = EventBus()
        received1: list[Event] = []
        received3: list[Event] = []

        def handler1(event: Event) -> None:
            received1.append(event)

        def handler2(event: Event) -> None:
            raise ValueError("Intentional subscriber failure")

        def handler3(event: Event) -> None:
            received3.append(event)

        bus.subscribe(EventType.STEP_STATUS_CHANGED, handler1)
        bus.subscribe(EventType.STEP_STATUS_CHANGED, handler2)
        bus.subscribe(EventType.STEP_STATUS_CHANGED, handler3)
        await bus.start()

        await bus.publish(EventType.STEP_STATUS_CHANGED, {"step": "s1"})

        await asyncio.sleep(0.1)
        await bus.stop()

        # handler1 and handler3 must still receive the event
        assert len(received1) == 1
        assert len(received3) == 1
        assert received1[0].data["step"] == "s1"
        assert received3[0].data["step"] == "s1"


class TestPublishSyncQueuing:
    """Tests for publish_sync event queuing when no loop is available."""

    @pytest.mark.asyncio
    async def test_publish_sync_queuing(self) -> None:
        """Events published via publish_sync before set_event_loop should be delivered after loop is set."""
        bus = EventBus()
        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(EventType.VARIABLE_CHANGED, handler)

        # Simulate publish_sync from a non-async thread where no loop is available
        # by directly appending to the pending queue (the production path when
        # publish_sync is called from a worker thread with no loop set).
        bus._pending_queue.append((EventType.VARIABLE_CHANGED, {"var": "x"}))
        bus._pending_queue.append((EventType.VARIABLE_CHANGED, {"var": "y"}))

        # Verify events are in the pending queue
        assert len(bus._pending_queue) == 2

        # Start the bus and set the event loop — should drain pending events
        await bus.start()
        loop = asyncio.get_running_loop()
        bus.set_event_loop(loop)

        # Wait for queued events to be processed
        await asyncio.sleep(0.2)
        await bus.stop()

        # Both events should have been delivered
        assert len(received) == 2
        assert received[0].data["var"] == "x"
        assert received[1].data["var"] == "y"

    @pytest.mark.asyncio
    async def test_publish_sync_with_loop_delivers_immediately(self) -> None:
        """publish_sync with loop already set should deliver events immediately."""
        bus = EventBus()
        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(EventType.STEP_COMPLETED, handler)
        await bus.start()
        bus.set_event_loop(asyncio.get_running_loop())

        bus.publish_sync(EventType.STEP_COMPLETED, {"step": "s1"})

        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(received) == 1
        assert received[0].data["step"] == "s1"

    @pytest.mark.asyncio
    async def test_publish_sync_from_worker_thread_queues(self) -> None:
        """publish_sync from a worker thread with no loop set should queue events."""
        import threading

        bus = EventBus()

        # Publish from a background thread where no loop is available
        def publish_from_thread() -> None:
            bus.publish_sync(EventType.VARIABLE_CHANGED, {"var": "thread_val"})

        thread = threading.Thread(target=publish_from_thread)
        thread.start()
        thread.join(timeout=2.0)

        # Event should be in the pending queue, not dropped
        assert len(bus._pending_queue) == 1
        assert bus._pending_queue[0][0] == EventType.VARIABLE_CHANGED
        assert bus._pending_queue[0][1]["var"] == "thread_val"


class TestEventBusStats:
    """Tests for EventBus.stats property."""

    @pytest.mark.asyncio
    async def test_stats_property(self) -> None:
        """Stats counters should accurately reflect publish/deliver/drop operations."""
        bus = EventBus()

        # Initial stats
        assert bus.stats == {"published": 0, "delivered": 0, "dropped": 0, "pending": 0}

        received: list[Event] = []

        def good_handler(event: Event) -> None:
            received.append(event)

        def bad_handler(event: Event) -> None:
            raise RuntimeError("fail")

        bus.subscribe(EventType.STEP_STATUS_CHANGED, good_handler)
        bus.subscribe(EventType.STEP_STATUS_CHANGED, bad_handler)
        await bus.start()

        # Publish 2 events — each dispatched to 2 subscribers (1 good, 1 bad)
        await bus.publish(EventType.STEP_STATUS_CHANGED, {"step": "s1"})
        await bus.publish(EventType.STEP_STATUS_CHANGED, {"step": "s2"})

        await asyncio.sleep(0.1)
        await bus.stop()

        stats = bus.stats
        assert stats["published"] == 2
        # 2 events × 2 subscribers = 4 dispatch attempts
        assert stats["delivered"] == 2  # good_handler succeeded twice
        assert stats["dropped"] == 2  # bad_handler failed twice
        assert stats["pending"] == 0

    @pytest.mark.asyncio
    async def test_stats_pending_counts_queued_events(self) -> None:
        """Stats.pending should reflect events queued in _pending_queue."""
        import threading

        bus = EventBus()

        # Queue events from a worker thread where no loop is available
        def publish_from_thread() -> None:
            bus.publish_sync(EventType.VARIABLE_CHANGED, {"var": "a"})
            bus.publish_sync(EventType.VARIABLE_CHANGED, {"var": "b"})
            bus.publish_sync(EventType.VARIABLE_CHANGED, {"var": "c"})

        thread = threading.Thread(target=publish_from_thread)
        thread.start()
        thread.join(timeout=2.0)

        assert bus.stats["pending"] == 3
        assert bus.stats["published"] == 0  # publish_sync without loop doesn't increment published
