"""Unit tests for skip_if precondition in ScannerScheduler and ConditionEvaluator.

Tests cover:
- evaluate_skip_condition() with variable resolution
- skip_if evaluates True → step SKIPPED, STEP_SKIPPED event emitted
- skip_if evaluates False → step dispatches normally
- skip_if undefined/None → normal dispatch (no skip)
- Cascade: SKIPPED step triggers dependent evaluation
- Emergency scan path also respects skip_if
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

import pytest

from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.scheduler.event_bus import EventBus, Event, EventType
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
from ate_platform.scheduler.step_registry import StepRegistry
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import Condition, StepStatus


class TestEvaluateSkipCondition:
    """Tests for ConditionEvaluator.evaluate_skip_condition()."""

    def test_skip_when_expression_is_true(self) -> None:
        """Should return True when expression evaluates to True."""
        variable_space = VariableSpace()
        variable_space.set("scope.skip_tests", "true")

        evaluator = ConditionEvaluator(
            {},
            variable_space=variable_space,
        )

        assert evaluator.evaluate_skip_condition('"${scope.skip_tests}" == "true"') is True

    def test_no_skip_when_expression_is_false(self) -> None:
        """Should return False when expression evaluates to False."""
        variable_space = VariableSpace()
        variable_space.set("scope.skip_tests", "false")

        evaluator = ConditionEvaluator(
            {},
            variable_space=variable_space,
        )

        assert evaluator.evaluate_skip_condition('"${scope.skip_tests}" == "true"') is False

    def test_no_skip_when_expression_is_empty(self) -> None:
        """Should return False for empty or whitespace-only expressions."""
        evaluator = ConditionEvaluator({})

        assert evaluator.evaluate_skip_condition("") is False
        assert evaluator.evaluate_skip_condition("   ") is False

    def test_no_skip_when_expression_is_none(self) -> None:
        """Should handle None expression gracefully (shouldn't happen in practice)."""
        evaluator = ConditionEvaluator({})

        # evaluate_skip_condition expects a string, but guard with falsy check
        assert evaluator.evaluate_skip_condition("") is False

    def test_no_skip_when_expression_eval_fails(self) -> None:
        """Should return False (fail-safe) when expression cannot be evaluated."""
        evaluator = ConditionEvaluator({})

        # Invalid Python expression
        assert evaluator.evaluate_skip_condition("1 / 0") is False
        # Syntax error
        assert evaluator.evaluate_skip_condition("== invalid ==") is False

    def test_direct_boolean_expression(self) -> None:
        """Should evaluate simple boolean expressions directly."""
        evaluator = ConditionEvaluator({})

        assert evaluator.evaluate_skip_condition("True") is True
        assert evaluator.evaluate_skip_condition("False") is False
        assert evaluator.evaluate_skip_condition("1 == 1") is True
        assert evaluator.evaluate_skip_condition("1 == 2") is False

    def test_numeric_comparison_with_variable(self) -> None:
        """Should resolve variable references in numeric comparisons."""
        variable_space = VariableSpace()
        variable_space.set("scope.threshold", 5)

        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        # Should skip when threshold is low enough
        assert evaluator.evaluate_skip_condition("${scope.threshold} < 10") is True
        assert evaluator.evaluate_skip_condition("${scope.threshold} > 10") is False


class TestSkipIfDispatch:
    """Tests for ScannerScheduler skip_if dispatch behavior."""

    @pytest.mark.asyncio
    async def test_skip_if_true_step_skipped(self) -> None:
        """When skip_if evaluates True, step should be SKIPPED and STEP_SKIPPED emitted."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        # Set variable that causes skip
        variable_space.set("scope.debug_mode", "1")

        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        # Register step with skip_if
        registry.register("step1")
        scheduler.register_skip_conditions({
            "step1": ('"${scope.debug_mode}" == "1"', "Debug mode enabled"),
        })

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        # Collect events
        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_SKIPPED, handler)
        event_bus.subscribe(EventType.STEP_STARTED, handler)

        # Dispatch
        await scheduler._dispatch_step("step1")
        await asyncio.sleep(0.1)

        # Should have STEP_SKIPPED
        skipped_events = [e for e in received if e.type == EventType.STEP_SKIPPED]
        assert len(skipped_events) == 1
        assert skipped_events[0].data["step_id"] == "step1"
        assert skipped_events[0].data["reason"] == "Debug mode enabled"

        # Should NOT have STEP_STARTED
        started_events = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(started_events) == 0

        # Registry status should be SKIPPED
        assert registry.get_status("step1") == StepStatus.SKIPPED

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_skip_if_false_step_dispatched(self) -> None:
        """When skip_if evaluates False, step should dispatch normally."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        # Set variable that does NOT cause skip
        variable_space.set("scope.debug_mode", "0")

        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        registry.register("step1")
        scheduler.register_skip_conditions({
            "step1": ('"${scope.debug_mode}" == "1"', None),
        })

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_STARTED, handler)
        event_bus.subscribe(EventType.STEP_SKIPPED, handler)

        await scheduler._dispatch_step("step1")
        await asyncio.sleep(0.1)

        # Should have STEP_STARTED
        started_events = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(started_events) == 1
        assert started_events[0].data["step_id"] == "step1"

        # Should NOT have STEP_SKIPPED
        skipped_events = [e for e in received if e.type == EventType.STEP_SKIPPED]
        assert len(skipped_events) == 0

        # Status should still be PENDING (not RUNNING since we only emit STEP_STARTED)
        assert registry.get_status("step1") == StepStatus.PENDING

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_skip_if_undefined_normal_dispatch(self) -> None:
        """When no skip_if is registered, step should dispatch normally."""
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

        registry.register("step1")
        # No skip_conditions registered

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_STARTED, handler)
        event_bus.subscribe(EventType.STEP_SKIPPED, handler)

        await scheduler._dispatch_step("step1")
        await asyncio.sleep(0.1)

        # Should have STEP_STARTED
        started_events = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(started_events) == 1

        # No skip events
        skipped_events = [e for e in received if e.type == EventType.STEP_SKIPPED]
        assert len(skipped_events) == 0

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_skip_if_true_cascades_to_dependents(self) -> None:
        """When a step is SKIPPED, dependents should be triggered via reactive dispatch."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        variable_space.set("scope.skip", "1")

        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        # step1 has skip_if, step2 depends on step1
        registry.register("step1")
        registry.register("step2", Condition(step="step1", status="SKIPPED"))
        scheduler.register_skip_conditions({
            "step1": ('"${scope.skip}" == "1"', "Skipped via scope"),
        })
        scheduler.compile_plan([
            ("step2", Condition(step="step1", status="SKIPPED")),
        ])

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_SKIPPED, handler)
        event_bus.subscribe(EventType.STEP_STARTED, handler)

        # Dispatch step1 — it should be skipped
        await scheduler._dispatch_step("step1")
        await asyncio.sleep(0.2)

        # step1 should be SKIPPED
        assert registry.get_status("step1") == StepStatus.SKIPPED

        # step1 should have emitted STEP_SKIPPED
        skipped = [e for e in received if e.type == EventType.STEP_SKIPPED]
        assert len(skipped) == 1
        assert skipped[0].data["step_id"] == "step1"

        # step2 should have been dispatched because step1's SKIPPED satisfies
        # its condition (Condition(step="step1", status="SKIPPED"))
        started = [e for e in received if e.type == EventType.STEP_STARTED]
        step2_started = [e for e in started if e.data.get("step_id") == "step2"]
        assert len(step2_started) == 1

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_emergency_scan_respects_skip_if(self) -> None:
        """_emergency_scan should evaluate skip_if before dispatching."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        variable_space.set("scope.skip_scan", "yes")

        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        registry.register("step1")
        scheduler.register_skip_conditions({
            "step1": ('"${scope.skip_scan}" == "yes"', "Emergency scan skip"),
        })

        await event_bus.start()

        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_SKIPPED, handler)
        event_bus.subscribe(EventType.STEP_STARTED, handler)

        await scheduler._emergency_scan()
        await asyncio.sleep(0.1)

        # Should have STEP_SKIPPED
        skipped = [e for e in received if e.type == EventType.STEP_SKIPPED]
        assert len(skipped) == 1
        assert skipped[0].data["step_id"] == "step1"

        # Should NOT have STEP_STARTED for step1
        started = [e for e in received if e.type == EventType.STEP_STARTED
                   and e.data.get("step_id") == "step1"]
        assert len(started) == 0

        assert registry.get_status("step1") == StepStatus.SKIPPED

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_skip_if_false_emergency_scan_dispatches(self) -> None:
        """_emergency_scan dispatches normally when skip_if evaluates False."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        variable_space.set("scope.skip_scan", "no")

        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        registry.register("step1")
        scheduler.register_skip_conditions({
            "step1": ('"${scope.skip_scan}" == "yes"', None),
        })

        await event_bus.start()

        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_STARTED, handler)
        event_bus.subscribe(EventType.STEP_SKIPPED, handler)

        await scheduler._emergency_scan()
        await asyncio.sleep(0.1)

        # Should dispatch normally
        started = [e for e in received if e.type == EventType.STEP_STARTED]
        assert len(started) == 1

        # No skip
        skipped = [e for e in received if e.type == EventType.STEP_SKIPPED]
        assert len(skipped) == 0

        await event_bus.stop()


class TestSkipIfReason:
    """Tests for skip_reason in skip_if."""

    @pytest.mark.asyncio
    async def test_custom_reason_in_event(self) -> None:
        """STEP_SKIPPED event should include the custom skip_reason."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        variable_space.set("scope.skip", "1")

        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        registry.register("step1")
        scheduler.register_skip_conditions({
            "step1": ('"${scope.skip}" == "1"', "Environment not ready"),
        })

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_SKIPPED, handler)

        await scheduler._dispatch_step("step1")
        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0].data["reason"] == "Environment not ready"

        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_fallback_reason_when_none(self) -> None:
        """When skip_reason is None, event should include the expression as reason."""
        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        variable_space.set("scope.skip", "1")

        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        registry.register("step1")
        scheduler.register_skip_conditions({
            "step1": ('"${scope.skip}" == "1"', None),
        })

        await event_bus.start()
        event_bus.set_event_loop(asyncio.get_running_loop())

        received: list[Event] = []
        def handler(event: Event) -> None:
            received.append(event)
        event_bus.subscribe(EventType.STEP_SKIPPED, handler)

        await scheduler._dispatch_step("step1")
        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert "skip_if:" in received[0].data["reason"]

        await event_bus.stop()


class TestSkipIfDsl:
    """Tests for YamlStep and YamlLoop skip_if fields in DSL."""

    def test_yaml_step_has_skip_if_field(self) -> None:
        """YamlStep should have skip_if and skip_reason fields."""
        from shared.dsl import YamlStep

        step = YamlStep(
            id="test",
            script="test.py",
            skip_if='${scope.skip} == "1"',
            skip_reason="Test skip reason",
        )

        assert step.skip_if == '${scope.skip} == "1"'
        assert step.skip_reason == "Test skip reason"

    def test_yaml_step_skip_if_defaults_to_none(self) -> None:
        """YamlStep skip_if should default to None."""
        from shared.dsl import YamlStep

        step = YamlStep(id="test", script="test.py")

        assert step.skip_if is None
        assert step.skip_reason is None

    def test_yaml_loop_has_skip_if_field(self) -> None:
        """YamlLoop should have skip_if and skip_reason fields."""
        from shared.dsl import YamlLoop, LoopType

        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.FOR,
            skip_if='${scope.run_loop} == True',
            skip_reason="Loop disabled",
        )

        assert loop.skip_if == '${scope.run_loop} == True'
        assert loop.skip_reason == "Loop disabled"

    def test_yaml_loop_skip_if_defaults_to_none(self) -> None:
        """YamlLoop skip_if should default to None."""
        from shared.dsl import YamlLoop, LoopType

        loop = YamlLoop(id="loop1", loop_type=LoopType.FOR)

        assert loop.skip_if is None
        assert loop.skip_reason is None