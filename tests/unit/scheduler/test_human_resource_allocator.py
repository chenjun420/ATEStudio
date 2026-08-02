"""Unit tests for HumanResourceAllocator.

Tests cover:
- Skill matching: only operators with the required skill are eligible
- Cumulative constraint: operator capacity is respected (max_concurrent_tasks)
- Alternative resources: when multiple operators can do a task, exactly one is assigned
- Parallel task allocation: 2 tasks assigned to 2 operators run concurrently
- Robot capability matching and cumulative constraint
- Operator + robot combined allocation
- Feasibility check (pre-solver)
- Infeasible problem (no eligible operator)
- Empty task list returns empty dict
- OR-Tools unavailable skip
"""

from __future__ import annotations

import pytest

from ate_platform.scheduler.human_resource_allocator import (
    HumanResourceAllocator,
    OperatorSpec,
    RobotSpec,
    TaskSpec,
)

# ---------------------------------------------------------------------------
# Skip if OR-Tools is not installed
# ---------------------------------------------------------------------------
ortools_available = False
try:
    from ortools.sat.python import cp_model  # noqa: F401

    ortools_available = True
except ImportError:
    pass

pytestmark = pytest.mark.skipif(
    not ortools_available,
    reason="OR-Tools not installed",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_operator(
    op_id: str = "op1",
    skills: list[str] | None = None,
    max_concurrent: int = 1,
    available_from: int = 0,
    available_to: int = 10000,
) -> OperatorSpec:
    """Factory for OperatorSpec."""
    return OperatorSpec(
        id=op_id,
        skills=skills or [],
        max_concurrent_tasks=max_concurrent,
        available_from=available_from,
        available_to=available_to,
    )


def _make_robot(
    robot_id: str = "rb1",
    robot_type: str = "handler",
    capabilities: list[str] | None = None,
    speed: float = 1.0,
    max_concurrent: int = 1,
) -> RobotSpec:
    """Factory for RobotSpec."""
    return RobotSpec(
        id=robot_id,
        robot_type=robot_type,
        capabilities=capabilities or [],
        speed=speed,
        max_concurrent_tasks=max_concurrent,
    )


def _make_task(
    task_id: str = "t1",
    required_skill: str = "",
    required_capability: str = "",
    base_duration: int = 5,
    requires_operator: bool = True,
    requires_robot: bool = False,
) -> TaskSpec:
    """Factory for TaskSpec."""
    return TaskSpec(
        id=task_id,
        required_skill=required_skill,
        required_capability=required_capability,
        base_duration=base_duration,
        requires_operator=requires_operator,
        requires_robot=requires_robot,
    )


# ---------------------------------------------------------------------------
# Tests: basic allocation
# ---------------------------------------------------------------------------


class TestBasicAllocation:
    """Tests for basic operator allocation."""

    def test_single_task_single_operator(self) -> None:
        """Given: 1 operator with skill 'soldering' + 1 task requiring 'soldering'.
        When: allocate.
        Then: returns valid result with operator assigned.
        """
        allocator = HumanResourceAllocator(time_limit=3.0)
        result = allocator.allocate(
            operators=[_make_operator("op1", skills=["soldering"])],
            robots=[],
            tasks=[_make_task("t1", required_skill="soldering", base_duration=5)],
        )
        assert result is not None
        assert len(result) == 1
        assert result["t1"].operator_id == "op1"
        assert result["t1"].robot_id is None
        assert result["t1"].duration == 5
        assert result["t1"].start_time == 0
        assert result["t1"].end_time == 5

    def test_empty_tasks_returns_empty_dict(self) -> None:
        """Given: no tasks. When: allocate. Then: returns empty dict."""
        allocator = HumanResourceAllocator(time_limit=3.0)
        result = allocator.allocate(
            operators=[_make_operator("op1", skills=["soldering"])],
            robots=[],
            tasks=[],
        )
        assert result == {}

    def test_task_without_required_skill_assigns_any_operator(self) -> None:
        """Given: task with no required_skill + operator with no skills.
        When: allocate.
        Then: operator assigned (no skill filter).
        """
        allocator = HumanResourceAllocator(time_limit=3.0)
        result = allocator.allocate(
            operators=[_make_operator("op1", skills=[])],
            robots=[],
            tasks=[
                _make_task("t1", required_skill="", base_duration=3),
            ],
        )
        assert result is not None
        assert result["t1"].operator_id == "op1"


# ---------------------------------------------------------------------------
# Tests: skill matching
# ---------------------------------------------------------------------------


class TestSkillMatching:
    """Tests for skill-based operator eligibility."""

    def test_operator_without_skill_not_assigned(self) -> None:
        """Given: operator without 'rf_cal' + task requiring 'rf_cal'.
        When: allocate.
        Then: raises ValueError (no eligible operator).
        """
        allocator = HumanResourceAllocator(time_limit=3.0)
        with pytest.raises(ValueError, match="No eligible operator"):
            allocator.allocate(
                operators=[_make_operator("op1", skills=["soldering"])],
                robots=[],
                tasks=[
                    _make_task("t1", required_skill="rf_cal", base_duration=3),
                ],
            )

    def test_operator_with_matching_skill_assigned(self) -> None:
        """Given: operator with 'rf_cal' + task requiring 'rf_cal'.
        When: allocate.
        Then: operator assigned.
        """
        allocator = HumanResourceAllocator(time_limit=3.0)
        result = allocator.allocate(
            operators=[
                _make_operator("op1", skills=["soldering"]),
                _make_operator("op2", skills=["rf_cal"]),
            ],
            robots=[],
            tasks=[
                _make_task("t1", required_skill="rf_cal", base_duration=3),
            ],
        )
        assert result is not None
        assert result["t1"].operator_id == "op2"


# ---------------------------------------------------------------------------
# Tests: cumulative constraint (operator capacity)
# ---------------------------------------------------------------------------


class TestCumulativeConstraint:
    """Tests for cumulative resource capacity constraint."""

    def test_capacity_1_serializes_tasks(self) -> None:
        """Given: operator with capacity=1 + 2 tasks requiring same skill.
        When: allocate.
        Then: tasks do not overlap (serialized by cumulative constraint).
        """
        allocator = HumanResourceAllocator(time_limit=5.0)
        result = allocator.allocate(
            operators=[_make_operator("op1", skills=["test"], max_concurrent=1)],
            robots=[],
            tasks=[
                _make_task("t1", required_skill="test", base_duration=5),
                _make_task("t2", required_skill="test", base_duration=5),
            ],
        )
        assert result is not None
        t1_end = result["t1"].end_time
        t2_start = result["t2"].start_time
        t1_start = result["t1"].start_time
        t2_end = result["t2"].end_time
        # Either t1 before t2 or t2 before t1 — no overlap
        assert t1_end <= t2_start or t2_end <= t1_start

    def test_capacity_2_allows_parallel_tasks(self) -> None:
        """Given: operator with capacity=2 + 2 tasks requiring same skill.
        When: allocate.
        Then: tasks can run in parallel (overlap allowed).
        """
        allocator = HumanResourceAllocator(time_limit=5.0)
        result = allocator.allocate(
            operators=[_make_operator("op1", skills=["test"], max_concurrent=2)],
            robots=[],
            tasks=[
                _make_task("t1", required_skill="test", base_duration=5),
                _make_task("t2", required_skill="test", base_duration=5),
            ],
        )
        assert result is not None
        # With capacity=2, both can start at 0 and run in parallel
        assert result["t1"].start_time == 0
        assert result["t2"].start_time == 0
        assert result["t1"].end_time == 5
        assert result["t2"].end_time == 5


# ---------------------------------------------------------------------------
# Tests: alternative resources (multiple eligible operators)
# ---------------------------------------------------------------------------


class TestAlternativeResources:
    """Tests for alternative resource selection."""

    def test_two_operators_two_tasks_run_in_parallel(self) -> None:
        """Given: 2 operators (each capacity=1) with same skill + 2 tasks.
        When: allocate.
        Then: each task gets a different operator, tasks run in parallel.
        This is the key verification: allocating operators to 2 parallel
        tasks produces a schedule satisfying skill requirements.
        """
        allocator = HumanResourceAllocator(time_limit=5.0)
        result = allocator.allocate(
            operators=[
                _make_operator("op1", skills=["test"], max_concurrent=1),
                _make_operator("op2", skills=["test"], max_concurrent=1),
            ],
            robots=[],
            tasks=[
                _make_task("t1", required_skill="test", base_duration=5),
                _make_task("t2", required_skill="test", base_duration=5),
            ],
        )
        assert result is not None
        # Each task assigned to a different operator
        assert result["t1"].operator_id != result["t2"].operator_id
        # Both tasks can run in parallel (start at 0)
        assert result["t1"].start_time == 0
        assert result["t2"].start_time == 0
        assert result["t1"].end_time == 5
        assert result["t2"].end_time == 5

    def test_three_tasks_two_operators_one_serialized(self) -> None:
        """Given: 2 operators (capacity=1) + 3 tasks.
        When: allocate.
        Then: 2 tasks run in parallel, 1 waits. All 3 get assigned.
        """
        allocator = HumanResourceAllocator(time_limit=5.0)
        result = allocator.allocate(
            operators=[
                _make_operator("op1", skills=["test"], max_concurrent=1),
                _make_operator("op2", skills=["test"], max_concurrent=1),
            ],
            robots=[],
            tasks=[
                _make_task("t1", required_skill="test", base_duration=3),
                _make_task("t2", required_skill="test", base_duration=3),
                _make_task("t3", required_skill="test", base_duration=3),
            ],
        )
        assert result is not None
        assert len(result) == 3
        # Makespan should be 6 (2 waves of 3 time units)
        max_end = max(r.end_time for r in result.values())
        assert max_end == 6


# ---------------------------------------------------------------------------
# Tests: robot allocation
# ---------------------------------------------------------------------------


class TestRobotAllocation:
    """Tests for robot resource allocation."""

    def test_robot_capability_matching(self) -> None:
        """Given: robot with 'pick' capability + task requiring 'pick'.
        When: allocate.
        Then: robot assigned.
        """
        allocator = HumanResourceAllocator(time_limit=3.0)
        result = allocator.allocate(
            operators=[],
            robots=[
                _make_robot("rb1", capabilities=["place"]),
                _make_robot("rb2", capabilities=["pick"]),
            ],
            tasks=[
                _make_task(
                    "t1",
                    required_skill="",
                    required_capability="pick",
                    base_duration=4,
                    requires_operator=False,
                    requires_robot=True,
                ),
            ],
        )
        assert result is not None
        assert result["t1"].robot_id == "rb2"
        assert result["t1"].operator_id is None
        assert result["t1"].duration == 4

    def test_robot_without_capability_not_assigned(self) -> None:
        """Given: robot without 'scan' capability + task requiring 'scan'.
        When: allocate.
        Then: raises ValueError (no eligible robot).
        """
        allocator = HumanResourceAllocator(time_limit=3.0)
        with pytest.raises(ValueError, match="No eligible robot"):
            allocator.allocate(
                operators=[],
                robots=[_make_robot("rb1", capabilities=["pick"])],
                tasks=[
                    _make_task(
                        "t1",
                        required_capability="scan",
                        base_duration=3,
                        requires_operator=False,
                        requires_robot=True,
                    ),
                ],
            )

    def test_robot_cumulative_capacity(self) -> None:
        """Given: robot with capacity=1 + 2 tasks requiring same capability.
        When: allocate.
        Then: tasks serialized (no overlap).
        """
        allocator = HumanResourceAllocator(time_limit=5.0)
        result = allocator.allocate(
            operators=[],
            robots=[
                _make_robot(
                    "rb1",
                    capabilities=["pick"],
                    max_concurrent=1,
                ),
            ],
            tasks=[
                _make_task(
                    "t1",
                    required_capability="pick",
                    base_duration=4,
                    requires_operator=False,
                    requires_robot=True,
                ),
                _make_task(
                    "t2",
                    required_capability="pick",
                    base_duration=4,
                    requires_operator=False,
                    requires_robot=True,
                ),
            ],
        )
        assert result is not None
        t1_end = result["t1"].end_time
        t2_start = result["t2"].start_time
        t1_start = result["t1"].start_time
        t2_end = result["t2"].end_time
        assert t1_end <= t2_start or t2_end <= t1_start


# ---------------------------------------------------------------------------
# Tests: combined operator + robot allocation
# ---------------------------------------------------------------------------


class TestCombinedAllocation:
    """Tests for combined operator and robot allocation."""

    def test_operator_and_robot_both_required(self) -> None:
        """Given: task requiring both operator skill and robot capability.
        When: allocate.
        Then: both operator and robot assigned, task scheduled.
        """
        allocator = HumanResourceAllocator(time_limit=5.0)
        result = allocator.allocate(
            operators=[_make_operator("op1", skills=["assembly"])],
            robots=[_make_robot("rb1", capabilities=["pick"])],
            tasks=[
                _make_task(
                    "t1",
                    required_skill="assembly",
                    required_capability="pick",
                    base_duration=5,
                    requires_operator=True,
                    requires_robot=True,
                ),
            ],
        )
        assert result is not None
        assert result["t1"].operator_id == "op1"
        assert result["t1"].robot_id == "rb1"
        assert result["t1"].duration == 5

    def test_parallel_tasks_with_combined_resources(self) -> None:
        """Given: 2 operators + 2 robots + 2 tasks requiring both.
        When: allocate.
        Then: tasks run in parallel with different operator/robot pairs.
        """
        allocator = HumanResourceAllocator(time_limit=5.0)
        result = allocator.allocate(
            operators=[
                _make_operator("op1", skills=["assembly"], max_concurrent=1),
                _make_operator("op2", skills=["assembly"], max_concurrent=1),
            ],
            robots=[
                _make_robot("rb1", capabilities=["pick"], max_concurrent=1),
                _make_robot("rb2", capabilities=["pick"], max_concurrent=1),
            ],
            tasks=[
                _make_task(
                    "t1",
                    required_skill="assembly",
                    required_capability="pick",
                    base_duration=5,
                    requires_operator=True,
                    requires_robot=True,
                ),
                _make_task(
                    "t2",
                    required_skill="assembly",
                    required_capability="pick",
                    base_duration=5,
                    requires_operator=True,
                    requires_robot=True,
                ),
            ],
        )
        assert result is not None
        # Different operators and different robots
        assert result["t1"].operator_id != result["t2"].operator_id
        assert result["t1"].robot_id != result["t2"].robot_id
        # Both can run in parallel
        assert result["t1"].start_time == 0
        assert result["t2"].start_time == 0


# ---------------------------------------------------------------------------
# Tests: feasibility check
# ---------------------------------------------------------------------------


class TestFeasibilityCheck:
    """Tests for pre-solver feasibility checking."""

    def test_feasible_problem_no_errors(self) -> None:
        """Given: operators with required skills + tasks.
        When: check_feasibility.
        Then: returns empty list (no errors).
        """
        allocator = HumanResourceAllocator(time_limit=3.0)
        errors = allocator.check_feasibility(
            operators=[_make_operator("op1", skills=["soldering"])],
            robots=[_make_robot("rb1", capabilities=["pick"])],
            tasks=[
                _make_task("t1", required_skill="soldering"),
                _make_task(
                    "t2",
                    required_capability="pick",
                    requires_operator=False,
                    requires_robot=True,
                ),
            ],
        )
        assert errors == []

    def test_infeasible_skill_returns_error(self) -> None:
        """Given: operator without required skill + task.
        When: check_feasibility.
        Then: returns error message.
        """
        allocator = HumanResourceAllocator(time_limit=3.0)
        errors = allocator.check_feasibility(
            operators=[_make_operator("op1", skills=["soldering"])],
            robots=[],
            tasks=[_make_task("t1", required_skill="rf_cal")],
        )
        assert len(errors) == 1
        assert "rf_cal" in errors[0]

    def test_infeasible_capability_returns_error(self) -> None:
        """Given: robot without required capability + task.
        When: check_feasibility.
        Then: returns error message.
        """
        allocator = HumanResourceAllocator(time_limit=3.0)
        errors = allocator.check_feasibility(
            operators=[],
            robots=[_make_robot("rb1", capabilities=["pick"])],
            tasks=[
                _make_task(
                    "t1",
                    required_capability="scan",
                    requires_operator=False,
                    requires_robot=True,
                ),
            ],
        )
        assert len(errors) == 1
        assert "scan" in errors[0]


# ---------------------------------------------------------------------------
# Tests: availability window
# ---------------------------------------------------------------------------


class TestAvailabilityWindow:
    """Tests for operator/robot availability window constraints."""

    def test_operator_availability_window_respected(self) -> None:
        """Given: operator available from 10 + task with duration 5.
        When: allocate.
        Then: task starts at or after 10.
        """
        allocator = HumanResourceAllocator(time_limit=5.0)
        result = allocator.allocate(
            operators=[
                _make_operator(
                    "op1",
                    skills=["test"],
                    available_from=10,
                    available_to=100,
                ),
            ],
            robots=[],
            tasks=[
                _make_task("t1", required_skill="test", base_duration=5),
            ],
        )
        assert result is not None
        assert result["t1"].start_time >= 10
        assert result["t1"].end_time <= 100
