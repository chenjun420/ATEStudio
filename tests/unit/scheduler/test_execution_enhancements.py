"""Unit tests for execution enhancements: retry/repeat/skip-if/pause/resume/force_next.

Tests cover:
- StepRegistry: StepExecutionConfig storage, retry/repeat counters
- ScannerScheduler: skip_if via registry config, pause/resume, force_next,
  retry loop (handle_step_result), repeat loop (handle_step_result)
- StepExecutionConfig defaults (backward compatibility)
- Verification: max_retries=2 -> fail count increments -> 3 attempts total then stop
"""

from __future__ import annotations

import asyncio

import pytest

from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.scheduler.event_bus import Event, EventBus, EventType
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
from ate_platform.scheduler.step_registry import StepExecutionConfig, StepRegistry
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import StepStatus

# ---------------------------------------------------------------------------
# StepExecutionConfig + StepRegistry tests
# ---------------------------------------------------------------------------


class TestStepExecutionConfig:
    """Tests for StepExecutionConfig dataclass defaults."""

    def test_defaults_all_disabled(self) -> None:
        """Default config should have all features disabled (backward compat)."""
        config = StepExecutionConfig()

        assert config.max_retries == 0
        assert config.retry_delay_ms == 0
        assert config.repeat_on_measurement_fail is False
        assert config.repeat_limit == 0
        assert config.force_repeat is False
        assert config.skip_if is None

    def test_custom_config(self) -> None:
        """Should accept custom values for all fields."""
        config = StepExecutionConfig(
            max_retries=3,
            retry_delay_ms=500,
            repeat_on_measurement_fail=True,
            repeat_limit=5,
            force_repeat=False,
            skip_if="${scope.skip} == True",
        )

        assert config.max_retries == 3
        assert config.retry_delay_ms == 500
        assert config.repeat_on_measurement_fail is True
        assert config.repeat_limit == 5
        assert config.force_repeat is False
        assert config.skip_if == "${scope.skip} == True"

    def test_frozen(self) -> None:
        """StepExecutionConfig should be immutable (frozen=True)."""
        config = StepExecutionConfig(max_retries=2)

        with pytest.raises((AttributeError, Exception)):
            config.max_retries = 5  # type: ignore[misc]


class TestStepRegistryConfig:
    """Tests for StepRegistry config storage and retry/repeat counters."""

    def test_register_with_config(self) -> None:
        """Should store config when provided to register()."""
        registry = StepRegistry()
        config = StepExecutionConfig(max_retries=2, skip_if="True")

        registry.register("step1", config=config)

        stored = registry.get_config("step1")
        assert stored.max_retries == 2
        assert stored.skip_if == "True"

    def test_register_without_config_returns_default(self) -> None:
        """register() without config should return default config (backward compat)."""
        registry = StepRegistry()

        registry.register("step1")

        config = registry.get_config("step1")
        assert config == StepExecutionConfig()

    def test_get_config_unregistered_raises(self) -> None:
        """get_config should raise KeyError for unregistered step."""
        registry = StepRegistry()

        with pytest.raises(KeyError, match="not registered"):
            registry.get_config("unknown")

    def test_retry_count_starts_at_zero(self) -> None:
        """Newly registered step should have retry_count=0."""
        registry = StepRegistry()

        registry.register("step1")

        assert registry.get_retry_count("step1") == 0

    def test_increment_retry_count(self) -> None:
        """increment_retry_count should return new value."""
        registry = StepRegistry()

        registry.register("step1")

        assert registry.increment_retry_count("step1") == 1
        assert registry.increment_retry_count("step1") == 2
        assert registry.get_retry_count("step1") == 2

    def test_reset_retry_count(self) -> None:
        """reset_retry_count should set counter back to 0."""
        registry = StepRegistry()

        registry.register("step1")
        registry.increment_retry_count("step1")
        registry.increment_retry_count("step1")

        registry.reset_retry_count("step1")

        assert registry.get_retry_count("step1") == 0

    def test_repeat_count_starts_at_zero(self) -> None:
        """Newly registered step should have repeat_count=0."""
        registry = StepRegistry()

        registry.register("step1")

        assert registry.get_repeat_count("step1") == 0

    def test_increment_repeat_count(self) -> None:
        """increment_repeat_count should return new value."""
        registry = StepRegistry()

        registry.register("step1")

        assert registry.increment_repeat_count("step1") == 1
        assert registry.increment_repeat_count("step1") == 2

    def test_reset_repeat_count(self) -> None:
        """reset_repeat_count should set counter back to 0."""
        registry = StepRegistry()

        registry.register("step1")
        registry.increment_repeat_count("step1")

        registry.reset_repeat_count("step1")

        assert registry.get_repeat_count("step1") == 0

    def test_clear_resets_counters(self) -> None:
        """clear() should wipe configs and counters."""
        registry = StepRegistry()
        registry.register("step1", config=StepExecutionConfig(max_retries=2))
        registry.increment_retry_count("step1")

        registry.clear()

        # After clear, step1 is gone
        with pytest.raises(KeyError):
            registry.get_config("step1")
        with pytest.raises(KeyError):
            registry.get_retry_count("step1")

    def test_unregister_removes_config_and_counters(self) -> None:
        """unregister should remove config, retry_count, repeat_count."""
        registry = StepRegistry()
        registry.register("step1", config=StepExecutionConfig(max_retries=2))
        registry.increment_retry_count("step1")

        result = registry.unregister("step1")

        assert result is True
        with pytest.raises(KeyError):
            registry.get_config("step1")
        with pytest.raises(KeyError):
            registry.get_retry_count("step1")


# ---------------------------------------------------------------------------
# ScannerScheduler skip_if via registry config
# ---------------------------------------------------------------------------


class TestSkipIfViaRegistryConfig:
    """Tests for skip_if evaluation from StepExecutionConfig (not legacy dict)."""

    @pytest.mark.asyncio
    async def test_skip_if_true_from_config_skips_step(self) -> None:
        """When StepExecutionConfig.skip_if evaluates True, step should be SKIPPED."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        variable_space.set("scope.skip_me", "1")
        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        registry.register(
            "step1",
            config=StepExecutionConfig(skip_if='"${scope.skip_me}" == "1"'),
        )

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        event_bus.subscribe(EventType.STEP_SKIPPED, handler)
        event_bus.subscribe(EventType.STEP_STARTED, handler)

        await scheduler._dispatch_step("step1")
        await asyncio.sleep(0.1)

        skipped = [e for e in received if e.type == EventType.STEP_SKIPPED]
        assert len(skipped) == 1
        assert skipped[0].data["step_id"] == "step1"

        started = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(started) == 0

        assert registry.get_status("step1") == StepStatus.SKIPPED

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_skip_if_false_from_config_dispatches(self) -> None:
        """When StepExecutionConfig.skip_if evaluates False, step dispatches normally."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        variable_space.set("scope.skip_me", "0")
        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        registry.register(
            "step1",
            config=StepExecutionConfig(skip_if='"${scope.skip_me}" == "1"'),
        )

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        event_bus.subscribe(EventType.STEP_STARTED, handler)
        event_bus.subscribe(EventType.STEP_SKIPPED, handler)

        await scheduler._dispatch_step("step1")
        await asyncio.sleep(0.1)

        started = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(started) == 1

        skipped = [e for e in received if e.type == EventType.STEP_SKIPPED]
        assert len(skipped) == 0

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_no_skip_if_in_config_dispatches(self) -> None:
        """When config has no skip_if, step dispatches normally."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        # Config with no skip_if
        registry.register("step1", config=StepExecutionConfig(max_retries=2))

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        event_bus.subscribe(EventType.STEP_STARTED, handler)
        event_bus.subscribe(EventType.STEP_SKIPPED, handler)

        await scheduler._dispatch_step("step1")
        await asyncio.sleep(0.1)

        started = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(started) == 1

        await event_bus.stop()


# ---------------------------------------------------------------------------
# ScannerScheduler pause/resume
# ---------------------------------------------------------------------------


class TestPauseResume:
    """Tests for pause() / resume() via asyncio.Event."""

    def test_pause_sets_paused_flag(self) -> None:
        """pause() should set is_paused to True."""
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

        assert scheduler.is_paused is False

        scheduler.pause()

        assert scheduler.is_paused is True

    def test_resume_clears_paused_flag(self) -> None:
        """resume() should set is_paused to False."""
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

        scheduler.pause()
        assert scheduler.is_paused is True

        scheduler.resume()
        assert scheduler.is_paused is False

    def test_pause_idempotent(self) -> None:
        """Multiple pause() calls should be safe."""
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

        scheduler.pause()
        scheduler.pause()
        scheduler.pause()

        assert scheduler.is_paused is True

    def test_resume_idempotent(self) -> None:
        """Multiple resume() calls should be safe."""
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

        scheduler.resume()
        scheduler.resume()

        assert scheduler.is_paused is False

    @pytest.mark.asyncio
    async def test_paused_dispatch_blocks_until_resume(self) -> None:
        """When paused, _dispatch_step should block until resume() is called."""
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
        event_bus.set_event_loop(asyncio.get_running_loop())

        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        event_bus.subscribe(EventType.STEP_STARTED, handler)

        # Pause before dispatching
        scheduler.pause()

        # Start dispatch in background - it should block
        dispatch_task = asyncio.create_task(scheduler._dispatch_step("step1"))

        # Wait a bit - should not have dispatched
        await asyncio.sleep(0.2)
        started = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(started) == 0

        # Resume - should now dispatch
        scheduler.resume()

        await asyncio.gather(dispatch_task, return_exceptions=True)
        await asyncio.sleep(0.1)

        started = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(started) == 1
        assert started[0].data["step_id"] == "step1"

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_status_includes_paused_field(self) -> None:
        """get_status should include paused field."""
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

        status = scheduler.get_status()
        assert "paused" in status
        assert status["paused"] is False

        scheduler.pause()
        status = scheduler.get_status()
        assert status["paused"] is True

        scheduler.resume()
        status = scheduler.get_status()
        assert status["paused"] is False


# ---------------------------------------------------------------------------
# ScannerScheduler force_next
# ---------------------------------------------------------------------------


class TestForceNext:
    """Tests for force_next - bypass skip_if for the next step."""

    @pytest.mark.asyncio
    async def test_force_next_bypasses_skip_if(self) -> None:
        """force_next should bypass skip_if and dispatch the step."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        variable_space.set("scope.skip_me", "1")
        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        # Register a step with skip_if that evaluates True
        registry.register(
            "step1",
            config=StepExecutionConfig(skip_if='"${scope.skip_me}" == "1"'),
        )

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        event_bus.subscribe(EventType.STEP_STARTED, handler)
        event_bus.subscribe(EventType.STEP_SKIPPED, handler)

        # Set force_next - should bypass skip_if
        scheduler.force_next()

        await scheduler._dispatch_step("step1")
        await asyncio.sleep(0.1)

        started = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(started) == 1
        assert started[0].data["step_id"] == "step1"

        skipped = [e for e in received if e.type == EventType.STEP_SKIPPED]
        assert len(skipped) == 0

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_force_next_is_one_shot(self) -> None:
        """force_next should only apply to the first step, not subsequent ones."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        variable_space.set("scope.skip_me", "1")
        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        # Two steps with skip_if
        registry.register(
            "step1",
            config=StepExecutionConfig(skip_if='"${scope.skip_me}" == "1"'),
        )
        registry.register(
            "step2",
            config=StepExecutionConfig(skip_if='"${scope.skip_me}" == "1"'),
        )

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        event_bus.subscribe(EventType.STEP_STARTED, handler)
        event_bus.subscribe(EventType.STEP_SKIPPED, handler)

        scheduler.force_next()

        # Dispatch step1 - should bypass skip_if
        await scheduler._dispatch_step("step1")
        await asyncio.sleep(0.1)

        # Dispatch step2 - should NOT bypass (one-shot consumed)
        await scheduler._dispatch_step("step2")
        await asyncio.sleep(0.1)

        started = [e for e in received if e.type == EventType.STEP_STARTED]
        started_ids = [e.data["step_id"] for e in started]
        assert "step1" in started_ids
        assert "step2" not in started_ids  # step2 was skipped

        skipped = [e for e in received if e.type == EventType.STEP_SKIPPED]
        skipped_ids = [e.data["step_id"] for e in skipped]
        assert "step2" in skipped_ids

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_status_includes_force_next_pending(self) -> None:
        """get_status should include force_next_pending field."""
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

        status = scheduler.get_status()
        assert "force_next_pending" in status
        assert status["force_next_pending"] is False

        scheduler.force_next()
        status = scheduler.get_status()
        assert status["force_next_pending"] is True


# ---------------------------------------------------------------------------
# ScannerScheduler retry logic (handle_step_result)
# ---------------------------------------------------------------------------


class TestRetryLogic:
    """Tests for retry on ERROR via handle_step_result().

    Verification scenario from task spec:
        max_retries=2 -> fail count increments -> 3 attempts total then stop.
    """

    @pytest.mark.asyncio
    async def test_retry_on_error_increments_count(self) -> None:
        """Given max_retries=2, first ERROR should increment retry_count to 1 and reset to PENDING."""
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

        registry.register(
            "step1",
            config=StepExecutionConfig(max_retries=2, retry_delay_ms=0),
        )

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        # First ERROR -> retry 1
        result = await scheduler.handle_step_result("step1", StepStatus.ERROR)

        assert result is True  # Retry triggered
        assert registry.get_retry_count("step1") == 1
        assert registry.get_status("step1") == StepStatus.PENDING

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_retry_exhausted_leaves_in_error(self) -> None:
        """Given max_retries=2, after 2 retries (3 total attempts) step stays ERROR.

        This is the exact verification scenario from the task spec:
        "设置 max_retries=2->失败计数递增->3 次后停止"
        """
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

        registry.register(
            "step1",
            config=StepExecutionConfig(max_retries=2, retry_delay_ms=0),
        )

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        # Attempt 1: ERROR -> retry (retry_count=1)
        r1 = await scheduler.handle_step_result("step1", StepStatus.ERROR)
        assert r1 is True
        assert registry.get_retry_count("step1") == 1

        # Attempt 2: ERROR -> retry (retry_count=2)
        r2 = await scheduler.handle_step_result("step1", StepStatus.ERROR)
        assert r2 is True
        assert registry.get_retry_count("step1") == 2

        # Attempt 3: ERROR -> no more retries (retry_count == max_retries)
        r3 = await scheduler.handle_step_result("step1", StepStatus.ERROR)
        assert r3 is False
        assert registry.get_retry_count("step1") == 2  # not incremented
        assert registry.get_status("step1") == StepStatus.ERROR

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_no_retry_when_max_retries_zero(self) -> None:
        """When max_retries=0, ERROR should not trigger retry."""
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

        registry.register("step1", config=StepExecutionConfig(max_retries=0))

        await event_bus.start()

        result = await scheduler.handle_step_result("step1", StepStatus.ERROR)

        assert result is False
        assert registry.get_retry_count("step1") == 0

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_passed_resets_retry_count(self) -> None:
        """PASSED status should reset retry_count to 0."""
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

        registry.register("step1", config=StepExecutionConfig(max_retries=3))

        await event_bus.start()

        # Retry once
        await scheduler.handle_step_result("step1", StepStatus.ERROR)
        assert registry.get_retry_count("step1") == 1

        # Pass -> reset
        result = await scheduler.handle_step_result("step1", StepStatus.PASSED)
        assert result is False
        assert registry.get_retry_count("step1") == 0

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_retry_resets_step_to_pending(self) -> None:
        """Retry should reset step status from ERROR to PENDING."""
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

        registry.register("step1", config=StepExecutionConfig(max_retries=3))

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        # Simulate step in ERROR state
        registry.update_status("step1", StepStatus.ERROR)

        # Trigger retry
        result = await scheduler.handle_step_result("step1", StepStatus.ERROR)

        assert result is True
        assert registry.get_status("step1") == StepStatus.PENDING

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_retry_publishes_status_changed_event(self) -> None:
        """Retry should publish STEP_STATUS_CHANGED event (ERROR->PENDING)."""
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

        registry.register("step1", config=StepExecutionConfig(max_retries=3))

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        event_bus.subscribe(EventType.STEP_STATUS_CHANGED, handler)

        registry.update_status("step1", StepStatus.ERROR)
        await asyncio.sleep(0.05)

        await scheduler.handle_step_result("step1", StepStatus.ERROR)
        await asyncio.sleep(0.1)

        # Should have a STEP_STATUS_CHANGED with new_status=PENDING
        status_changes = [
            e for e in received
            if e.type == EventType.STEP_STATUS_CHANGED
            and e.data.get("new_status") == "PENDING"
        ]
        assert len(status_changes) >= 1
        assert status_changes[-1].data["step_id"] == "step1"

        await event_bus.stop()


# ---------------------------------------------------------------------------
# ScannerScheduler repeat logic (handle_step_result)
# ---------------------------------------------------------------------------


class TestRepeatLogic:
    """Tests for repeat on FAILED via handle_step_result()."""

    @pytest.mark.asyncio
    async def test_repeat_on_failed_when_enabled(self) -> None:
        """When repeat_on_measurement_fail=True, FAILED should trigger repeat."""
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

        registry.register(
            "step1",
            config=StepExecutionConfig(
                repeat_on_measurement_fail=True,
                repeat_limit=3,
            ),
        )

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        result = await scheduler.handle_step_result("step1", StepStatus.FAILED)

        assert result is True
        assert registry.get_repeat_count("step1") == 1
        assert registry.get_status("step1") == StepStatus.PENDING

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_repeat_exhausted_leaves_in_failed(self) -> None:
        """After repeat_limit exhausted, FAILED stays FAILED."""
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

        registry.register(
            "step1",
            config=StepExecutionConfig(
                repeat_on_measurement_fail=True,
                repeat_limit=2,
            ),
        )

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        # Repeat 1
        r1 = await scheduler.handle_step_result("step1", StepStatus.FAILED)
        assert r1 is True
        assert registry.get_repeat_count("step1") == 1

        # Repeat 2
        r2 = await scheduler.handle_step_result("step1", StepStatus.FAILED)
        assert r2 is True
        assert registry.get_repeat_count("step1") == 2

        # Repeat 3: limit exhausted
        r3 = await scheduler.handle_step_result("step1", StepStatus.FAILED)
        assert r3 is False
        assert registry.get_repeat_count("step1") == 2
        assert registry.get_status("step1") == StepStatus.FAILED

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_no_repeat_when_disabled(self) -> None:
        """When repeat_on_measurement_fail=False, FAILED should not repeat."""
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

        registry.register(
            "step1",
            config=StepExecutionConfig(
                repeat_on_measurement_fail=False,
                repeat_limit=3,
            ),
        )

        await event_bus.start()

        result = await scheduler.handle_step_result("step1", StepStatus.FAILED)

        assert result is False
        assert registry.get_repeat_count("step1") == 0

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_force_repeat_ignores_limit(self) -> None:
        """force_repeat=True should repeat regardless of repeat_limit."""
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

        registry.register(
            "step1",
            config=StepExecutionConfig(
                repeat_on_measurement_fail=True,
                repeat_limit=1,
                force_repeat=True,
            ),
        )

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        # First FAILED -> repeat (repeat_count=1)
        r1 = await scheduler.handle_step_result("step1", StepStatus.FAILED)
        assert r1 is True
        assert registry.get_repeat_count("step1") == 1

        # Second FAILED -> still repeats (force_repeat ignores limit)
        r2 = await scheduler.handle_step_result("step1", StepStatus.FAILED)
        assert r2 is True
        assert registry.get_repeat_count("step1") == 2

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_error_does_not_trigger_repeat(self) -> None:
        """ERROR status should not trigger repeat (only retry)."""
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

        registry.register(
            "step1",
            config=StepExecutionConfig(
                repeat_on_measurement_fail=True,
                repeat_limit=3,
                max_retries=0,  # No retry
            ),
        )

        await event_bus.start()

        # ERROR should not trigger repeat (repeat is for FAILED only)
        result = await scheduler.handle_step_result("step1", StepStatus.ERROR)

        assert result is False
        assert registry.get_repeat_count("step1") == 0

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_passed_resets_repeat_count(self) -> None:
        """PASSED status should reset repeat_count to 0."""
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

        registry.register(
            "step1",
            config=StepExecutionConfig(
                repeat_on_measurement_fail=True,
                repeat_limit=3,
            ),
        )

        await event_bus.start()

        # Repeat once
        await scheduler.handle_step_result("step1", StepStatus.FAILED)
        assert registry.get_repeat_count("step1") == 1

        # Pass -> reset
        result = await scheduler.handle_step_result("step1", StepStatus.PASSED)
        assert result is False
        assert registry.get_repeat_count("step1") == 0

        await event_bus.stop()


# ---------------------------------------------------------------------------
# Combined retry + repeat scenario
# ---------------------------------------------------------------------------


class TestCombinedRetryRepeat:
    """Tests for combined retry (on ERROR) + repeat (on FAILED) behavior."""

    @pytest.mark.asyncio
    async def test_error_triggers_retry_not_repeat(self) -> None:
        """ERROR should trigger retry, not repeat, even when both are configured."""
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

        registry.register(
            "step1",
            config=StepExecutionConfig(
                max_retries=2,
                repeat_on_measurement_fail=True,
                repeat_limit=3,
            ),
        )

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        # ERROR -> retry, not repeat
        result = await scheduler.handle_step_result("step1", StepStatus.ERROR)

        assert result is True
        assert registry.get_retry_count("step1") == 1
        assert registry.get_repeat_count("step1") == 0

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_failed_triggers_repeat_not_retry(self) -> None:
        """FAILED should trigger repeat, not retry, even when both are configured."""
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

        registry.register(
            "step1",
            config=StepExecutionConfig(
                max_retries=2,
                repeat_on_measurement_fail=True,
                repeat_limit=3,
            ),
        )

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        # FAILED -> repeat, not retry
        result = await scheduler.handle_step_result("step1", StepStatus.FAILED)

        assert result is True
        assert registry.get_retry_count("step1") == 0
        assert registry.get_repeat_count("step1") == 1

        await event_bus.stop()


# ---------------------------------------------------------------------------
# Full integration: max_retries=2 -> 3 attempts total
# ---------------------------------------------------------------------------


class TestFullRetryScenario:
    """Full verification: max_retries=2 -> 3 attempts total then stop.

    This is the exact verification scenario from the task spec:
    "设置 max_retries=2->失败计数递增->3 次后停止"
    """

    @pytest.mark.asyncio
    async def test_three_attempts_then_stop(self) -> None:
        """With max_retries=2, step should attempt 3 times total then stay ERROR."""
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

        registry.register(
            "step1",
            config=StepExecutionConfig(max_retries=2, retry_delay_ms=0),
        )

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        attempt_count = 0
        max_attempts = 10  # Safety limit

        while attempt_count < max_attempts:
            attempt_count += 1

            # Simulate step failing with ERROR
            registry.update_status("step1", StepStatus.ERROR)

            # Ask scheduler if we should retry
            retried = await scheduler.handle_step_result("step1", StepStatus.ERROR)

            if not retried:
                # No more retries - step stays in ERROR
                break

        # Should have attempted exactly 3 times:
        # attempt 1 (initial) + 2 retries = 3 total
        assert attempt_count == 3
        assert registry.get_retry_count("step1") == 2
        assert registry.get_status("step1") == StepStatus.ERROR

        await event_bus.stop()
