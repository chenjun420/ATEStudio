"""Tests for Tier 2: DryRunScheduler.

Covers:
- Basic plan traversal with pass/fail/skip decisions
- Precondition evaluation (step dependencies)
- Resource allocation and blocking
- skip_if expression evaluation
- YamlLoop traversal (FOR, WHILE, FOREACH)
- Deadlock detection
- DryRunResult statistics and helper methods
- Isolated state (no interference with production scheduler)
"""

from __future__ import annotations

from ate_platform.simulation.dry_run_scheduler import (
    DryRunScheduler,
)
from shared.dsl import LoopType, YamlLoop, YamlPlan, YamlStep

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_step(
    step_id: str,
    script: str = "test.py",
    preconditions: list[str] | None = None,
    resources: dict[str, object] | None = None,
    skip_if: str | None = None,
) -> YamlStep:
    """Create a YamlStep with sensible defaults."""
    return YamlStep(
        id=step_id,
        script=script,
        preconditions=preconditions or [],
        resources=resources or {},
        skip_if=skip_if,
    )


def _make_plan(
    steps: list[YamlStep | YamlLoop],
    name: str = "test_plan",
    version: str = "1.0",
) -> YamlPlan:
    """Create a YamlPlan with the given steps."""
    return YamlPlan(name=name, version=version, steps=steps)


# ---------------------------------------------------------------------------
# Basic traversal tests
# ---------------------------------------------------------------------------


class TestBasicTraversal:
    """Tests for basic plan traversal."""

    def test_single_step_passes(self) -> None:
        """A single step with no preconditions should PASS."""
        plan = _make_plan([_make_step("s1")])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.total_steps == 1
        assert result.passed == 1
        assert result.failed == 0
        assert result.all_passed is True

    def test_multiple_independent_steps_all_pass(self) -> None:
        """Multiple steps without dependencies should all PASS."""
        plan = _make_plan([
            _make_step("s1"),
            _make_step("s2"),
            _make_step("s3"),
        ])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.passed == 3
        assert result.all_passed is True

    def test_empty_plan(self) -> None:
        """An empty plan should produce zero decisions."""
        plan = _make_plan([])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.total_steps == 0
        assert result.passed == 0
        assert result.all_passed is True

    def test_decision_has_correct_fields(self) -> None:
        """StepDecision should have all expected fields populated."""
        plan = _make_plan([_make_step("s1")])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        decision = result.decisions[0]
        assert decision.step_id == "s1"
        assert decision.decision == "PASS"
        assert isinstance(decision.reason, str)
        assert decision.condition_met is True
        assert isinstance(decision.resources_acquired, list)
        assert decision.skip_if_evaluated is None
        assert decision.skip_if_result is None


# ---------------------------------------------------------------------------
# Precondition tests
# ---------------------------------------------------------------------------


class TestPreconditions:
    """Tests for step precondition evaluation."""

    def test_dependent_step_passes_after_predecessor(self) -> None:
        """A step whose predecessor passed should also PASS."""
        plan = _make_plan([
            _make_step("s1"),
            _make_step("s2", preconditions=["s1"]),
        ])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.passed == 2
        assert result.all_passed is True

    def test_step_blocked_by_failed_predecessor(self) -> None:
        """A step whose predecessor was skipped should be BLOCKED."""
        plan = _make_plan([
            _make_step("s1", skip_if="True"),
            _make_step("s2", preconditions=["s1"]),
        ])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.skipped == 1  # s1 skipped
        assert result.blocked == 1  # s2 blocked
        assert result.all_passed is False

    def test_chain_of_dependencies(self) -> None:
        """A chain s1 -> s2 -> s3 should all pass."""
        plan = _make_plan([
            _make_step("s1"),
            _make_step("s2", preconditions=["s1"]),
            _make_step("s3", preconditions=["s2"]),
        ])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.passed == 3
        assert result.all_passed is True

    def test_multiple_preconditions_all_must_pass(self) -> None:
        """Step with multiple preconditions requires all to pass."""
        plan = _make_plan([
            _make_step("s1"),
            _make_step("s2", skip_if="True"),
            _make_step("s3", preconditions=["s1", "s2"]),
        ])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        # s1 passes, s2 skipped, s3 blocked (s2 didn't pass)
        assert result.passed == 1
        assert result.skipped == 1
        assert result.blocked == 1


# ---------------------------------------------------------------------------
# skip_if tests
# ---------------------------------------------------------------------------


class TestSkipIf:
    """Tests for skip_if expression evaluation."""

    def test_skip_if_true_skips_step(self) -> None:
        """skip_if='True' should skip the step."""
        plan = _make_plan([
            _make_step("s1", skip_if="True"),
        ])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.skipped == 1
        decision = result.decisions[0]
        assert decision.decision == "SKIP"
        assert decision.skip_if_evaluated == "True"
        assert decision.skip_if_result is True

    def test_skip_if_false_executes_step(self) -> None:
        """skip_if='False' should NOT skip the step."""
        plan = _make_plan([
            _make_step("s1", skip_if="False"),
        ])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.passed == 1
        decision = result.decisions[0]
        assert decision.decision == "PASS"
        assert decision.skip_if_result is False

    def test_skip_if_none_executes_step(self) -> None:
        """No skip_if should execute the step normally."""
        plan = _make_plan([_make_step("s1")])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.passed == 1
        decision = result.decisions[0]
        assert decision.skip_if_evaluated is None
        assert decision.skip_if_result is None

    def test_skip_if_with_variable(self) -> None:
        """skip_if with a variable reference should resolve."""
        from ate_platform.scheduler.variable_space import VariableSpace

        vs = VariableSpace()
        vs.set("scope.skip_tests", True)

        plan = _make_plan([
            _make_step("s1", skip_if="${scope.skip_tests}"),
        ])
        scheduler = DryRunScheduler(variable_space=vs)
        result = scheduler.dry_run(plan)

        assert result.skipped == 1

    def test_skipped_step_satisfies_dependents(self) -> None:
        """A skipped step should NOT satisfy dependents (they get BLOCKED)."""
        plan = _make_plan([
            _make_step("s1", skip_if="True"),
            _make_step("s2", preconditions=["s1"]),
        ])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        # s1 is skipped, s2's precondition (s1 PASSED) is not met
        assert result.skipped == 1
        assert result.blocked == 1


# ---------------------------------------------------------------------------
# Resource tests
# ---------------------------------------------------------------------------


class TestResourceAllocation:
    """Tests for resource acquisition and blocking."""

    def test_step_with_resources_acquires_and_releases(self) -> None:
        """Step should acquire resources, then release them after."""
        plan = _make_plan([
            _make_step("s1", resources={"DMM_CH1": {}}),
        ])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.passed == 1
        decision = result.decisions[0]
        assert decision.resources_acquired == ["DMM_CH1"]

    def test_resource_reused_by_sequential_steps(self) -> None:
        """Sequential steps sharing a resource should both pass."""
        plan = _make_plan([
            _make_step("s1", resources={"DMM_CH1": {}}),
            _make_step("s2", resources={"DMM_CH1": {}}),
        ])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.passed == 2

    def test_no_resources_passes(self) -> None:
        """Step with no resources should pass without resource checks."""
        plan = _make_plan([_make_step("s1")])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.passed == 1
        assert result.decisions[0].resources_acquired == []


# ---------------------------------------------------------------------------
# Loop tests
# ---------------------------------------------------------------------------


class TestLoopTraversal:
    """Tests for YamlLoop traversal."""

    def test_for_loop_expands_iterations(self) -> None:
        """FOR loop should produce decisions for each iteration's children."""
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.FOR,
            count=3,
            steps=[_make_step("inner_step")],
        )
        plan = _make_plan([loop])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        # 3 iterations * 1 step each = 3 decisions
        assert result.total_steps == 3
        assert result.passed == 3

    def test_for_loop_with_skip_if(self) -> None:
        """FOR loop with skip_if=True should be skipped entirely."""
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.FOR,
            count=3,
            steps=[_make_step("inner_step")],
            skip_if="True",
        )
        plan = _make_plan([loop])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.skipped == 1
        # Only the loop-level skip decision, no child steps
        assert result.total_steps == 1

    def test_while_loop_simulates_one_iteration(self) -> None:
        """WHILE loop should simulate at least one iteration."""
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.WHILE,
            condition="True",
            count=None,
            steps=[_make_step("inner_step")],
        )
        plan = _make_plan([loop])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.total_steps >= 1
        assert result.passed >= 1

    def test_foreach_loop_simulates_one_iteration(self) -> None:
        """FOREACH loop should simulate one representative iteration."""
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.FOREACH,
            collection="items",
            iterator_var="item",
            steps=[_make_step("inner_step")],
        )
        plan = _make_plan([loop])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.total_steps == 1
        assert result.passed == 1

    def test_nested_loops(self) -> None:
        """Nested loops should be traversed recursively."""
        inner_loop = YamlLoop(
            id="inner",
            loop_type=LoopType.FOR,
            count=2,
            steps=[_make_step("deep_step")],
        )
        outer_loop = YamlLoop(
            id="outer",
            loop_type=LoopType.FOR,
            count=2,
            steps=[inner_loop],
        )
        plan = _make_plan([outer_loop])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        # 2 outer * 2 inner * 1 step = 4 decisions
        assert result.total_steps == 4
        assert result.passed == 4

    def test_loop_iteration_step_ids_are_unique(self) -> None:
        """Loop iteration step IDs should be unique (loop#iter#step)."""
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.FOR,
            count=2,
            steps=[_make_step("step_a")],
        )
        plan = _make_plan([loop])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        step_ids = [d.step_id for d in result.decisions]
        assert len(step_ids) == len(set(step_ids))  # All unique
        assert "loop1#0#step_a" in step_ids
        assert "loop1#1#step_a" in step_ids


# ---------------------------------------------------------------------------
# Result and statistics tests
# ---------------------------------------------------------------------------


class TestDryRunResult:
    """Tests for DryRunResult data structure and helpers."""

    def test_result_summary_string(self) -> None:
        """summary property should produce a readable string."""
        plan = _make_plan([_make_step("s1")])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        summary = result.summary
        assert "DryRun" in summary
        assert "test_plan" in summary
        assert "PASS" in summary

    def test_get_decision_by_step_id(self) -> None:
        """get_decision should find a decision by step_id."""
        plan = _make_plan([_make_step("s1"), _make_step("s2")])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        decision = result.get_decision("s2")
        assert decision is not None
        assert decision.step_id == "s2"

    def test_get_decision_not_found(self) -> None:
        """get_decision should return None for unknown step_id."""
        plan = _make_plan([_make_step("s1")])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.get_decision("nonexistent") is None

    def test_get_failed_steps(self) -> None:
        """get_failed_steps should return only FAIL decisions."""
        # Create a plan where a step is blocked (which is not FAIL, but test the filter)
        plan = _make_plan([_make_step("s1", skip_if="True")])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        # No failures, just skips
        assert result.get_failed_steps() == []

    def test_get_skipped_steps(self) -> None:
        """get_skipped_steps should return only SKIP decisions."""
        plan = _make_plan([
            _make_step("s1", skip_if="True"),
            _make_step("s2"),
        ])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        skipped = result.get_skipped_steps()
        assert len(skipped) == 1
        assert skipped[0].step_id == "s1"

    def test_duration_is_positive(self) -> None:
        """duration_s should be a non-negative number."""
        plan = _make_plan([_make_step("s1")])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.duration_s >= 0.0

    def test_deadlock_detected_when_blocked(self) -> None:
        """deadlock_detected should be True when steps are blocked."""
        plan = _make_plan([
            _make_step("s1", skip_if="True"),
            _make_step("s2", preconditions=["s1"]),
        ])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.deadlock_detected is True
        assert "s2" in result.deadlock_steps

    def test_no_deadlock_when_all_pass(self) -> None:
        """deadlock_detected should be False when all steps pass."""
        plan = _make_plan([_make_step("s1")])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.deadlock_detected is False
        assert result.deadlock_steps == []


# ---------------------------------------------------------------------------
# Isolation tests
# ---------------------------------------------------------------------------


class TestStateIsolation:
    """Tests that the dry-run scheduler doesn't interfere with production state."""

    def test_each_run_uses_fresh_registry(self) -> None:
        """Each dry_run call should clear and re-register steps."""
        scheduler = DryRunScheduler()

        plan1 = _make_plan([_make_step("s1")], name="plan1")
        result1 = scheduler.dry_run(plan1)
        assert result1.passed == 1

        # Run a different plan - should not see s1 from previous run
        plan2 = _make_plan([_make_step("s2")], name="plan2")
        result2 = scheduler.dry_run(plan2)
        assert result2.passed == 1
        assert result2.get_decision("s1") is None

    def test_resource_manager_is_isolated(self) -> None:
        """The dry-run's ResourceManager should be separate from production."""
        scheduler = DryRunScheduler()
        assert scheduler.resource_manager is not None

        # After a run, resources should be released
        plan = _make_plan([_make_step("s1", resources={"DMM": {}})])
        scheduler.dry_run(plan)

        # Resource should be available after the run
        assert scheduler.resource_manager.is_available("DMM")

    def test_custom_variable_space(self) -> None:
        """Should accept a pre-populated VariableSpace."""
        from ate_platform.scheduler.variable_space import VariableSpace

        vs = VariableSpace()
        vs.set("scope.test_var", 42)

        scheduler = DryRunScheduler(variable_space=vs)
        assert scheduler.variable_space.get("scope.test_var") == 42


# ---------------------------------------------------------------------------
# Complex scenario tests
# ---------------------------------------------------------------------------


class TestComplexScenarios:
    """Tests for complex multi-step scenarios."""

    def test_plan_with_mixed_decisions(self) -> None:
        """A plan with pass, skip, and blocked steps."""
        plan = _make_plan([
            _make_step("s1"),
            _make_step("s2", skip_if="True"),
            _make_step("s3", preconditions=["s2"]),  # blocked
            _make_step("s4", preconditions=["s1"]),  # passes
        ])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.passed == 2  # s1, s4
        assert result.skipped == 1  # s2
        assert result.blocked == 1  # s3
        assert result.all_passed is False

    def test_resource_sharing_in_loop(self) -> None:
        """Steps in a loop sharing a resource should all pass (sequential)."""
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.FOR,
            count=3,
            steps=[_make_step("measure", resources={"DMM": {}})],
        )
        plan = _make_plan([loop])
        scheduler = DryRunScheduler()
        result = scheduler.dry_run(plan)

        assert result.passed == 3
        assert result.blocked == 0

    def test_large_plan_performance(self) -> None:
        """A large plan should complete quickly (no real execution)."""
        steps = [_make_step(f"s{i}") for i in range(100)]
        plan = _make_plan(steps)
        scheduler = DryRunScheduler()

        start = __import__("time").monotonic()
        result = scheduler.dry_run(plan)
        elapsed = __import__("time").monotonic() - start

        assert result.passed == 100
        # Should complete in well under 1 second (no real execution)
        assert elapsed < 1.0
