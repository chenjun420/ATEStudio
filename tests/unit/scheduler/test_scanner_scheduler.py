"""Unit tests for ScannerScheduler in ATE Platform.

Tests cover:
- Start/stop lifecycle
- Event subscriptions
- Step readiness detection
- STEP_READY event emission
- Deadlock detection
- Reactive dispatch (event-driven step evaluation)
- Dependency index building
- Pending dispatch deduplication
- Emergency scan (watchdog fallback)
- Variable/resource change triggers
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
        """Should initialize with default scan interval (5.0s)."""
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
        assert ScannerScheduler.DEFAULT_SCAN_INTERVAL == 5.0
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
        """Should emit STEP_STARTED for step with no conditions."""
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

        event_bus.subscribe(EventType.STEP_STARTED, handler)

        await event_bus.start()
        await scheduler.start()

        # Wait for scan
        await asyncio.sleep(0.3)

        await scheduler.stop()
        await event_bus.stop()

        # Should have received STEP_STARTED event
        step_started_events = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(step_started_events) == 1
        assert step_started_events[0].data["step_id"] == "step1"

    @pytest.mark.asyncio
    async def test_does_not_emit_for_non_pending_step(self) -> None:
        """Should not emit STEP_STARTED for non-pending steps."""
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

        event_bus.subscribe(EventType.STEP_STARTED, handler)

        await event_bus.start()
        await scheduler.start()

        # Wait for scan
        await asyncio.sleep(0.3)

        await scheduler.stop()
        await event_bus.stop()

        # Should not have received STEP_STARTED event
        step_started_events = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(step_started_events) == 0

    @pytest.mark.asyncio
    async def test_emits_step_ready_after_condition_met(self) -> None:
        """Should emit STEP_STARTED after condition becomes true."""
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

        event_bus.subscribe(EventType.STEP_STARTED, handler)

        await event_bus.start()
        await scheduler.start()

        # Wait for scan
        await asyncio.sleep(0.3)

        await scheduler.stop()
        await event_bus.stop()

        # Should have received STEP_STARTED for step2
        step_started_events = [e for e in received if e.type == EventType.STEP_STARTED]
        step_ids = [e.data["step_id"] for e in step_started_events]
        assert "step2" in step_ids

    @pytest.mark.asyncio
    async def test_does_not_duplicate_notifications(self) -> None:
        """Should not emit duplicate STEP_STARTED events."""
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

        event_bus.subscribe(EventType.STEP_STARTED, handler)

        await event_bus.start()
        await scheduler.start()

        # Wait for multiple scans
        await asyncio.sleep(0.5)

        await scheduler.stop()
        await event_bus.stop()

        # Should have received exactly one STEP_STARTED event
        step_started_events = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(step_started_events) == 1


class TestScannerSchedulerDeadlockDetection:
    """Tests for deadlock detection (moved to WatchDog)."""

    @pytest.mark.asyncio
    async def test_watchdog_created_on_start(self) -> None:
        """WatchDog should be created and started when scheduler starts."""
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

        await event_bus.start()
        await scheduler.start()

        # WatchDog should be created and running
        assert scheduler._watchdog is not None
        assert scheduler._watchdog.is_running is True

        await scheduler.stop()
        await event_bus.stop()

        # WatchDog should be stopped
        assert scheduler._watchdog is None

    @pytest.mark.asyncio
    async def test_heartbeat_increments_on_scan_loop(self) -> None:
        """Heartbeat counter should increment each scan loop iteration."""
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
            scan_interval=0.05,  # Fast for testing
        )

        await event_bus.start()
        await scheduler.start()

        # Wait for a few scan iterations
        await asyncio.sleep(0.2)

        await scheduler.stop()
        await event_bus.stop()

        # Heartbeat should have incremented multiple times
        assert scheduler._heartbeat >= 2

    @pytest.mark.asyncio
    async def test_get_status_includes_heartbeat_and_watchdog(self) -> None:
        """get_status should include heartbeat and watchdog_running fields."""
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

        status = scheduler.get_status()
        assert "heartbeat" in status
        assert "watchdog_running" in status
        assert status["watchdog_running"] is True

        await scheduler.stop()
        await event_bus.stop()

        status = scheduler.get_status()
        assert status["watchdog_running"] is False


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
        assert status["heartbeat"] == 0
        assert status["notified_ready_count"] == 0
        assert status["watchdog_running"] is False

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

        await event_bus.start()
        await scheduler.start()

        status = scheduler.get_status()
        assert status["running"] is True
        assert status["watchdog_running"] is True

        await scheduler.stop()
        await event_bus.stop()

        status = scheduler.get_status()
        assert status["running"] is False
        assert status["watchdog_running"] is False


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
        assert EventType.MEASUREMENT_RECORDED in scheduler._handlers
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

        # Publish step completion event with normalized schema
        await event_bus.publish(
            EventType.STEP_STATUS_CHANGED,
            {"step_id": "step1", "old_status": "RUNNING", "new_status": "PASSED"},
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


# ── New tests for reactive dispatch (Task 2) ──────────────────────────


class TestDependencyIndex:
    """Tests for compile_plan() dependency index building."""

    def test_build_index_with_step_dependency(self) -> None:
        """compile_plan should index step_id → dependent steps."""
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

        steps: list[tuple[str, Condition | None]] = [
            ("step1", None),
            ("step2", Condition(step="step1", status="PASSED")),
            ("step3", Condition(step="step1", status="PASSED")),
        ]
        scheduler.compile_plan(steps)

        index = scheduler._dependency_index
        assert "step1" in index
        assert index["step1"] == {"step2", "step3"}

    def test_build_index_with_variable_dependency(self) -> None:
        """compile_plan should index variable → dependent steps."""
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

        steps: list[tuple[str, Condition | None]] = [
            ("step1", Condition(expression="${scope.voltage} > 3.0")),
            ("step2", Condition(expression="${scope.temperature} < 100")),
        ]
        scheduler.compile_plan(steps)

        index = scheduler._dependency_index
        assert "scope.voltage" in index
        assert index["scope.voltage"] == {"step1"}
        assert "scope.temperature" in index
        assert index["scope.temperature"] == {"step2"}

    def test_build_index_with_resource_dependency(self) -> None:
        """compile_plan should index resource → dependent steps."""
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

        steps: list[tuple[str, Condition | None]] = [
            ("step1", Condition(resource_available=["DMM_CH1"])),
            ("step2", Condition(resource_available=["DMM_CH1", "PSU_CH1"])),
        ]
        scheduler.compile_plan(steps)

        index = scheduler._dependency_index
        assert "DMM_CH1" in index
        assert index["DMM_CH1"] == {"step1", "step2"}
        assert "PSU_CH1" in index
        assert index["PSU_CH1"] == {"step2"}

    def test_build_index_no_condition(self) -> None:
        """compile_plan should not index steps without conditions."""
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

        steps: list[tuple[str, Condition | None]] = [
            ("step1", None),
            ("step2", None),
        ]
        scheduler.compile_plan(steps)

        assert len(scheduler._dependency_index) == 0

    def test_build_index_clears_previous(self) -> None:
        """compile_plan should clear previous index before building."""
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

        steps1: list[tuple[str, Condition | None]] = [
            ("step1", Condition(step="step0", status="PASSED")),
        ]
        scheduler.compile_plan(steps1)
        assert len(scheduler._dependency_index) == 1

        steps2: list[tuple[str, Condition | None]] = [
            ("stepA", Condition(step="stepB", status="PASSED")),
        ]
        scheduler.compile_plan(steps2)
        assert "step0" not in scheduler._dependency_index
        assert "stepB" in scheduler._dependency_index


class TestReactiveDispatch:
    """Tests for reactive dispatch via event handlers."""

    @pytest.mark.asyncio
    async def test_step_status_change_triggers_dependent_dispatch(self) -> None:
        """on_step_status_changed should trigger dispatch for dependent steps."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace(event_bus=event_bus)
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        # Register step1 (prerequisite) and step2 (depends on step1)
        registry.register("step1")
        registry.register("step2", Condition(step="step1", status="PASSED"))

        # Build dependency index
        scheduler.compile_plan([
            ("step1", None),
            ("step2", Condition(step="step1", status="PASSED")),
        ])

        # Set up event bus loop for reactive dispatch
        await event_bus.start()

        # Start scheduler (subscribes handlers)
        scheduler._setup_event_handlers()

        # Track STEP_STARTED events
        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_STARTED, handler)

        # Mark step1 as PASSED — this should trigger reactive dispatch for step2
        registry.update_status("step1", StepStatus.PASSED)

        # Wait for event processing
        await asyncio.sleep(0.2)

        # step2 should have been dispatched
        step_started = [e for e in received if e.type == EventType.STEP_STARTED]
        step_ids = [e.data.get("step_id") for e in step_started]
        assert "step2" in step_ids

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_variable_change_triggers_dispatch(self) -> None:
        """on_variable_changed should trigger dispatch for variable-dependent steps."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace(event_bus=event_bus)
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        # Register a step that depends on a variable expression
        registry.register("step_voltage", Condition(expression="${scope.voltage} > 3.0"))

        # Build dependency index
        scheduler.compile_plan([
            ("step_voltage", Condition(expression="${scope.voltage} > 3.0")),
        ])

        await event_bus.start()
        scheduler._setup_event_handlers()

        # Track STEP_STARTED events
        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_STARTED, handler)

        # Set the variable — should fire VARIABLE_CHANGED and trigger dispatch
        variable_space.set("scope.voltage", 5.0)

        # Wait for event processing
        await asyncio.sleep(0.2)

        # step_voltage should have been dispatched
        step_started = [e for e in received if e.type == EventType.STEP_STARTED]
        step_ids = [e.data.get("step_id") for e in step_started]
        assert "step_voltage" in step_ids

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_resource_release_triggers_dispatch(self) -> None:
        """on_resource_released should trigger dispatch for resource-blocked steps."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager(event_bus=event_bus)

        # Give ConditionEvaluator access to resource_manager
        evaluator_with_rm = ConditionEvaluator(
            {}, resource_manager=resource_manager, variable_space=None,
        )

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator_with_rm,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        # Register a step that needs DMM_CH1
        registry.register("step_resource", Condition(resource_available=["DMM_CH1"]))

        # Build dependency index
        scheduler.compile_plan([
            ("step_resource", Condition(resource_available=["DMM_CH1"])),
        ])

        # Acquire and then release the resource to trigger event
        resource_manager.acquire("DMM_CH1", "other_step")

        await event_bus.start()
        scheduler._setup_event_handlers()

        # Track STEP_STARTED events
        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_STARTED, handler)

        # Release resource — should fire RESOURCE_RELEASED and trigger dispatch
        resource_manager.release("DMM_CH1", "other_step")

        # Wait for event processing
        await asyncio.sleep(0.2)

        # step_resource should have been dispatched
        step_started = [e for e in received if e.type == EventType.STEP_STARTED]
        step_ids = [e.data.get("step_id") for e in step_started]
        assert "step_resource" in step_ids

        await event_bus.stop()


class TestPendingDispatchDedup:
    """Tests for _pending_dispatch deduplication."""

    @pytest.mark.asyncio
    async def test_pending_dispatch_prevents_duplicate(self) -> None:
        """Steps in _pending_dispatch should not be re-dispatched."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace(event_bus=event_bus)
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        # Register step2 that depends on step1
        registry.register("step1")
        registry.register("step2", Condition(step="step1", status="PASSED"))

        scheduler.compile_plan([
            ("step1", None),
            ("step2", Condition(step="step1", status="PASSED")),
        ])

        # Manually add step2 to pending_dispatch
        scheduler._pending_dispatch.add("step2")

        await event_bus.start()
        scheduler._setup_event_handlers()

        # Track events
        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_STARTED, handler)

        # Mark step1 as PASSED — handler should see step2 already pending
        registry.update_status("step1", StepStatus.PASSED)

        # Wait for event processing
        await asyncio.sleep(0.2)

        # step2 should NOT be dispatched because it was in _pending_dispatch
        step_started = [e for e in received if e.type == EventType.STEP_STARTED
                        and e.data.get("step_id") == "step2"]
        assert len(step_started) == 0

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_dispatch_removes_from_pending(self) -> None:
        """After dispatch, step should be removed from _pending_dispatch."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace(event_bus=event_bus)
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        registry.register("step1")
        scheduler.compile_plan([("step1", None)])

        await event_bus.start()
        scheduler._setup_event_handlers()

        # Add step1 to pending, then dispatch it
        scheduler._pending_dispatch.add("step1")
        await scheduler._dispatch_step("step1")

        # Should have been removed from pending_dispatch
        assert "step1" not in scheduler._pending_dispatch
        # Should be in notified_ready now
        assert "step1" in scheduler._notified_ready

        await event_bus.stop()


class TestEmergencyScan:
    """Tests for the emergency scan (watchdog fallback)."""

    @pytest.mark.asyncio
    async def test_emergency_scan_dispatches_unconditional_step(self) -> None:
        """_emergency_scan should dispatch ready steps."""
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

        registry.register("step1")

        await event_bus.start()

        # Track events
        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_STARTED, handler)

        # Run emergency scan directly
        await scheduler._emergency_scan()

        # Wait for event processing
        await asyncio.sleep(0.1)

        step_started = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(step_started) >= 1
        assert any(e.data.get("step_id") == "step1" for e in step_started)

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_emergency_scan_skips_notified_steps(self) -> None:
        """_emergency_scan should skip steps already notified."""
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

        registry.register("step1")
        scheduler._notified_ready.add("step1")

        await event_bus.start()

        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_STARTED, handler)

        await scheduler._emergency_scan()
        await asyncio.sleep(0.1)

        # No new STEP_STARTED for step1
        step_started = [e for e in received if e.type == EventType.STEP_STARTED
                        and e.data.get("step_id") == "step1"]
        assert len(step_started) == 0

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_emergency_scan_skips_pending_dispatch(self) -> None:
        """_emergency_scan should skip steps in _pending_dispatch."""
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

        registry.register("step1")
        scheduler._pending_dispatch.add("step1")

        await event_bus.start()

        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_STARTED, handler)

        await scheduler._emergency_scan()
        await asyncio.sleep(0.1)

        step_started = [e for e in received if e.type == EventType.STEP_STARTED
                        and e.data.get("step_id") == "step1"]
        assert len(step_started) == 0

        await event_bus.stop()


class TestScanLoopWatchdog:
    """Tests for the watchdog scan loop behavior."""

    @pytest.mark.asyncio
    async def test_scan_interval_is_5_seconds_default(self) -> None:
        """Default scan interval should be 5.0 seconds."""
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

        assert scheduler._scan_interval == 5.0

    @pytest.mark.asyncio
    async def test_scan_loop_stops_promptly(self) -> None:
        """The scan loop should stop promptly when stop() is called."""
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
            scan_interval=0.1,  # Fast for test
        )

        await scheduler.start()
        # Stop should complete quickly (within timeout)
        await scheduler.stop()

        assert scheduler._running is False


class TestForceScan:
    """Tests for force_scan()."""

    @pytest.mark.asyncio
    async def test_force_scan_triggers_emergency_scan(self) -> None:
        """force_scan should schedule an emergency scan."""
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

        registry.register("step1")

        await event_bus.start()

        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_STARTED, handler)

        # Store the loop reference on event bus so force_scan can use it
        event_bus.set_event_loop(asyncio.get_running_loop())

        # Trigger force scan
        scheduler.force_scan()

        await asyncio.sleep(0.2)

        step_started = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(step_started) >= 1
        assert any(e.data.get("step_id") == "step1" for e in step_started)

        await event_bus.stop()


class TestScannerSchedulerStatusNewFields:
    """Tests for new status fields added in Task 2."""

    @pytest.mark.asyncio
    async def test_status_includes_new_fields(self) -> None:
        """get_status should include pending_dispatch_count and dependency_index_size."""
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

        # Build index
        scheduler.compile_plan([
            ("step1", Condition(step="step0", status="PASSED")),
        ])

        status = scheduler.get_status()
        assert "pending_dispatch_count" in status
        assert status["pending_dispatch_count"] == 0
        assert "dependency_index_size" in status
        assert status["dependency_index_size"] == 1
        assert "last_dispatch_time" in status
        assert status["last_dispatch_time"] == 0.0
        assert "heartbeat" in status
        assert status["heartbeat"] == 0
        assert "watchdog_running" in status
        assert status["watchdog_running"] is False