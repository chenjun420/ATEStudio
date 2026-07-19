"""Unit tests for ScannerScheduler in ATE Platform.

Tests cover:
- Start/stop lifecycle
- Event subscriptions
- Step readiness detection
- STEP_READY event emission
- Deadlock detection
"""

import asyncio

import pytest

from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.scheduler.event_bus import EventBus, Event, EventType
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.scanner_scheduler import (
    DeadlockDetectedError,
    ScannerScheduler,
)
from ate_platform.scheduler.step_registry import StepRegistry
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import Condition, StepStatus


class TestScannerSchedulerInit:
    """Tests for ScannerScheduler initialization."""

    def test_init_with_defaults(self) -> None:
        """Should initialize with default scan interval."""
        event_bus = EventBus()
        registry = StepRegistry()
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        assert scheduler._scan_interval == ScannerScheduler.DEFAULT_SCAN_INTERVAL
        assert scheduler._running is False
        assert scheduler._scan_task is None

    def test_init_with_custom_interval(self) -> None:
        """Should accept custom scan interval."""
        event_bus = EventBus()
        registry = StepRegistry()
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
            scan_interval=0.5,
        )

        assert scheduler._scan_interval == 0.5

    def test_default_constants(self) -> None:
        """Should have expected default constants."""
        assert ScannerScheduler.DEFAULT_SCAN_INTERVAL == 0.1
        assert ScannerScheduler.DEADLOCK_THRESHOLD == 100


class TestScannerSchedulerLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_task(self) -> None:
        """Start should create scanning task."""
        event_bus = EventBus()
        registry = StepRegistry()
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        await scheduler.start()

        assert scheduler._running is True
        assert scheduler._scan_task is not None

        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        """Multiple start calls should be safe."""
        event_bus = EventBus()
        registry = StepRegistry()
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        await scheduler.start()
        task1 = scheduler._scan_task
        await scheduler.start()
        task2 = scheduler._scan_task

        assert task1 == task2

        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self) -> None:
        """Stop when not running should be safe."""
        event_bus = EventBus()
        registry = StepRegistry()
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        await scheduler.stop()  # Should not raise

        assert scheduler._running is False

    @pytest.mark.asyncio
    async def test_stop_clears_running_flag(self) -> None:
        """Stop should set running to False."""
        event_bus = EventBus()
        registry = StepRegistry()
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        await scheduler.start()
        await scheduler.stop()

        assert scheduler._running is False

    @pytest.mark.asyncio
    async def test_graceful_stop_with_event_bus(self) -> None:
        """Should properly unsubscribe from event bus on stop."""
        event_bus = EventBus()
        registry = StepRegistry()
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        await event_bus.start()
        await scheduler.start()

        # Should have subscribed
        assert len(scheduler._handlers) == 3

        await scheduler.stop()
        await event_bus.stop()

        # Should have unsubscribed
        assert len(scheduler._handlers) == 0


class TestScannerSchedulerStepReady:
    """Tests for step readiness detection."""

    @pytest.mark.asyncio
    async def test_emits_step_ready_for_unconditional_step(self) -> None:
        """Should emit STEP_READY for step with no conditions."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        # Register a step with no condition
        registry.register("step1")

        # Track events
        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        event_bus.subscribe(EventType.STEP_STATUS_CHANGED, handler)

        await event_bus.start()
        await scheduler.start()

        # Wait for scan
        await asyncio.sleep(0.3)

        await scheduler.stop()
        await event_bus.stop()

        # Should have received STEP_READY event
        step_ready_events = [e for e in received if e.data.get("event") == "STEP_READY"]
        assert len(step_ready_events) == 1
        assert step_ready_events[0].data["step_id"] == "step1"

    @pytest.mark.asyncio
    async def test_does_not_emit_for_non_pending_step(self) -> None:
        """Should not emit STEP_READY for non-pending steps."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        # Register a step and set it to RUNNING
        registry.register("step1")
        registry.update_status("step1", StepStatus.RUNNING)

        # Track events
        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        event_bus.subscribe(EventType.STEP_STATUS_CHANGED, handler)

        await event_bus.start()
        await scheduler.start()

        # Wait for scan
        await asyncio.sleep(0.3)

        await scheduler.stop()
        await event_bus.stop()

        # Should not have received STEP_READY event
        step_ready_events = [e for e in received if e.data.get("event") == "STEP_READY"]
        assert len(step_ready_events) == 0

    @pytest.mark.asyncio
    async def test_emits_step_ready_after_condition_met(self) -> None:
        """Should emit STEP_READY after condition becomes true."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)

        # Set up step results with a completed prerequisite
        step_results: dict[str, dict[str, object]] = {
            "step1": {"status": StepStatus.PASSED, "outputs": {}}
        }
        evaluator = ConditionEvaluator(step_results, None, None)  # type: ignore[arg-type]
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        # Register step1 as completed
        registry.register("step1")
        registry.update_status("step1", StepStatus.PASSED)

        # Register step2 that depends on step1
        registry.register("step2", Condition(step="step1", status="PASSED"))

        # Track events
        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        event_bus.subscribe(EventType.STEP_STATUS_CHANGED, handler)

        await event_bus.start()
        await scheduler.start()

        # Wait for scan
        await asyncio.sleep(0.3)

        await scheduler.stop()
        await event_bus.stop()

        # Should have received STEP_READY for step2
        step_ready_events = [e for e in received if e.data.get("event") == "STEP_READY"]
        step_ids = [e.data["step_id"] for e in step_ready_events]
        assert "step2" in step_ids

    @pytest.mark.asyncio
    async def test_does_not_duplicate_notifications(self) -> None:
        """Should not emit duplicate STEP_READY events."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        # Register a step
        registry.register("step1")

        # Track events
        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        event_bus.subscribe(EventType.STEP_STATUS_CHANGED, handler)

        await event_bus.start()
        await scheduler.start()

        # Wait for multiple scans
        await asyncio.sleep(0.5)

        await scheduler.stop()
        await event_bus.stop()

        # Should have received exactly one STEP_READY event
        step_ready_events = [e for e in received if e.data.get("event") == "STEP_READY"]
        assert len(step_ready_events) == 1


class TestScannerSchedulerDeadlockDetection:
    """Tests for deadlock detection."""

    @pytest.mark.asyncio
    async def test_deadlock_detection_emits_event(self) -> None:
        """Should emit event when deadlock detected."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        # Use very low threshold for testing
        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
            scan_interval=0.01,  # Very fast scanning
        )

        # Force deadlock threshold to low value
        scheduler.DEADLOCK_THRESHOLD = 5

        # Register a step that can never be ready (no step_results for dependency)
        registry.register("step1", Condition(step="missing_step", status="PASSED"))

        # Track events
        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        event_bus.subscribe(EventType.EXTERNAL_CMD, handler)

        await event_bus.start()
        await scheduler.start()

        # Wait for deadlock detection
        await asyncio.sleep(0.2)

        await scheduler.stop()
        await event_bus.stop()

        # Should have received DEADLOCK_DETECTED event
        deadlock_events = [e for e in received if e.data.get("event") == "DEADLOCK_DETECTED"]
        assert len(deadlock_events) >= 1


class TestScannerSchedulerStatus:
    """Tests for get_status method."""

    @pytest.mark.asyncio
    async def test_get_status_returns_correct_info(self) -> None:
        """get_status should return current scheduler state."""
        event_bus = EventBus()
        registry = StepRegistry()
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
            scan_interval=0.5,
        )

        status = scheduler.get_status()

        assert status["running"] is False
        assert status["scan_interval"] == 0.5
        assert status["consecutive_no_progress"] == 0
        assert status["last_ready_count"] == 0
        assert status["notified_ready_count"] == 0

    @pytest.mark.asyncio
    async def test_get_status_after_start(self) -> None:
        """get_status should reflect running state after start."""
        event_bus = EventBus()
        registry = StepRegistry()
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        await scheduler.start()

        status = scheduler.get_status()
        assert status["running"] is True

        await scheduler.stop()

        status = scheduler.get_status()
        assert status["running"] is False


class TestScannerSchedulerEventHandlers:
    """Tests for event handler behavior."""

    @pytest.mark.asyncio
    async def test_event_handlers_subscribed_on_start(self) -> None:
        """Event handlers should be subscribed after start."""
        event_bus = EventBus()
        registry = StepRegistry()
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        await scheduler.start()

        assert len(scheduler._handlers) == 3
        assert EventType.VARIABLE_CHANGED in scheduler._handlers
        assert EventType.STEP_STATUS_CHANGED in scheduler._handlers
        assert EventType.RESOURCE_RELEASED in scheduler._handlers

        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_step_completion_removes_from_notified(self) -> None:
        """Completed step should be removed from notified_ready."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        # Register a step and simulate it being ready
        registry.register("step1")
        scheduler._notified_ready.add("step1")

        # Set up event handler
        await event_bus.start()
        scheduler._setup_event_handlers()

        # Publish step completion event
        await event_bus.publish(
            EventType.STEP_STATUS_CHANGED,
            {"step_id": "step1", "new_status": "PASSED"},
        )

        # Wait for event processing
        await asyncio.sleep(0.1)

        # Should have removed from notified_ready
        assert "step1" not in scheduler._notified_ready

        await scheduler.stop()
        await event_bus.stop()


class TestDeadlockDetectedError:
    """Tests for DeadlockDetectedError exception."""

    def test_exception_can_be_raised(self) -> None:
        """Should be able to raise DeadlockDetectedError."""
        with pytest.raises(DeadlockDetectedError):
            raise DeadlockDetectedError("Test deadlock")

    def test_exception_message(self) -> None:
        """Should preserve exception message."""
        error = DeadlockDetectedError("Test deadlock message")
        assert str(error) == "Test deadlock message"