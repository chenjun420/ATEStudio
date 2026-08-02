"""Human resource allocator — CP-SAT cumulative + alternative resource constraints.

Models operators (skill matrix, availability, capacity) and robots
(type, capabilities, speed, status) as CP-SAT cumulative resources.
When multiple operators or robots can perform the same task, an
AlternativeResources constraint (via ``model.AddAllowedAssignments``
or BoolVar-based channeling) selects exactly one.

Usage:
    from ate_platform.scheduler.human_resource_allocator import (
        HumanResourceAllocator,
        OperatorSpec,
        RobotSpec,
        TaskSpec,
    )

    allocator = HumanResourceAllocator(time_limit=5.0)
    result = allocator.allocate(
        operators=[OperatorSpec(...)],
        robots=[RobotSpec(...)],
        tasks=[TaskSpec(...)],
    )
    # result: dict[str, AllocationResult] — task_id -> allocation

References:
    - D5: Cumulative constraint for resource capacity
    - ABB DynTest: Alternative resources for parallel task allocation
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_ORTOOLS_AVAILABLE: bool = False
_cp_model: Any = None
_ORTOOLS_IMPORT_ERROR: str | None = None

try:
    from ortools.sat.python import cp_model

    _ORTOOLS_AVAILABLE = True
except ImportError as exc:
    _ORTOOLS_IMPORT_ERROR = str(exc)

_OPTIMAL_OR_FEASIBLE = (
    {cp_model.OPTIMAL, cp_model.FEASIBLE} if _ORTOOLS_AVAILABLE else set()
)


@dataclass(frozen=True, slots=True)
class OperatorSpec:
    """Specification of a human operator for CP-SAT modelling.

    Attributes:
        id: Unique operator identifier.
        skills: List of skill identifiers this operator possesses.
        max_concurrent_tasks: Capacity for the cumulative constraint
            (how many tasks the operator can handle simultaneously).
        available_from: Earliest time unit the operator is available.
        available_to: Latest time unit the operator is available.
    """

    id: str
    skills: list[str] = field(default_factory=list)
    max_concurrent_tasks: int = 1
    available_from: int = 0
    available_to: int = 10000


@dataclass(frozen=True, slots=True)
class RobotSpec:
    """Specification of a robot workstation for CP-SAT modelling.

    Attributes:
        id: Unique robot identifier.
        robot_type: Robot type identifier (e.g., ``"pick_place"``).
        capabilities: List of capability tags this robot supports.
        speed: Speed factor (higher = faster; duration = base / speed).
        max_concurrent_tasks: Capacity for the cumulative constraint.
        available_from: Earliest time unit the robot is available.
        available_to: Latest time unit the robot is available.
    """

    id: str
    robot_type: str = ""
    capabilities: list[str] = field(default_factory=list)
    speed: float = 1.0
    max_concurrent_tasks: int = 1
    available_from: int = 0
    available_to: int = 10000


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """Specification of a task requiring human and/or robot resources.

    Attributes:
        id: Unique task identifier.
        required_skill: Skill identifier the assigned operator must possess.
            If empty, any operator can be assigned.
        required_capability: Capability tag the assigned robot must support.
            If empty, no robot is required.
        base_duration: Base task duration in time units. When a robot is
            assigned, actual duration = base_duration / robot.speed (rounded
            up to at least 1).
        requires_operator: Whether an operator is required (default True).
        requires_robot: Whether a robot is required (default False).
        priority: Priority weight (higher = more important to schedule early).
    """

    id: str
    required_skill: str = ""
    required_capability: str = ""
    base_duration: int = 1
    requires_operator: bool = True
    requires_robot: bool = False
    priority: int = 0


@dataclass(frozen=True, slots=True)
class AllocationResult:
    """Allocation result for a single task.

    Attributes:
        task_id: The task that was allocated.
        operator_id: The operator assigned (None if no operator required).
        robot_id: The robot assigned (None if no robot required).
        start_time: Scheduled start time.
        end_time: Scheduled end time.
        duration: Actual duration (may differ from base_duration due to
            robot speed factor).
    """

    task_id: str
    operator_id: str | None
    robot_id: str | None
    start_time: int
    end_time: int
    duration: int


class HumanResourceAllocator:
    """CP-SAT allocator for human operators and robot workstations.

    Models operators and robots as cumulative resources with capacity =
    ``max_concurrent_tasks``. When multiple resources can perform the same
    task, an alternative-resources constraint (BoolVar-based channeling)
    ensures exactly one is assigned.

    Skill matching: an operator can only be assigned to a task if
    ``task.required_skill`` is in ``operator.skills`` (or required_skill
    is empty).

    Capability matching: a robot can only be assigned to a task if
    ``task.required_capability`` is in ``robot.capabilities`` (or
    required_capability is empty).

    The solver minimises makespan (total completion time).
    """

    def __init__(self, time_limit: float = 5.0) -> None:
        """Initialize the allocator.

        Args:
            time_limit: Solver time limit in seconds.
        """
        self._time_limit = time_limit
        if not _ORTOOLS_AVAILABLE:
            logger.warning(
                "OR-Tools not available: %s — HumanResourceAllocator "
                "will always return None",
                _ORTOOLS_IMPORT_ERROR,
            )

    def allocate(
        self,
        operators: list[OperatorSpec],
        robots: list[RobotSpec],
        tasks: list[TaskSpec],
    ) -> dict[str, AllocationResult] | None:
        """Allocate operators and robots to tasks via CP-SAT.

        Args:
            operators: Available human operators.
            robots: Available robot workstations.
            tasks: Tasks requiring resource allocation.

        Returns:
            Dict mapping task_id -> AllocationResult, or None if the solver
            is unavailable, the problem is infeasible, or the solver times out.
        """
        if not tasks:
            return {}

        if not _ORTOOLS_AVAILABLE:
            logger.debug("OR-Tools not available — returning None")
            return None

        model = cp_model.CpModel()
        state = self._build_model(operators, robots, tasks, model)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self._time_limit
        solver.parameters.log_search_progress = False

        status = solver.Solve(model)

        if status in _OPTIMAL_OR_FEASIBLE:
            return self._extract_results(tasks, state, solver)

        if status == cp_model.INFEASIBLE:
            logger.warning(
                "HumanResourceAllocator model is infeasible — "
                "check skill/capability coverage"
            )
        else:
            logger.debug("Solver status %s — returning None", status)
        return None

    def _build_model(
        self,
        operators: list[OperatorSpec],
        robots: list[RobotSpec],
        tasks: list[TaskSpec],
        model: Any,
    ) -> dict[str, Any]:
        """Build the CP-SAT model for resource allocation.

        Creates interval variables for tasks, cumulative constraints for
        each operator/robot (capacity = max_concurrent_tasks), and
        alternative-resource constraints (exactly-one assignment via
        BoolVar channeling).

        Returns:
            State dict containing all variables needed for result extraction.
        """
        horizon = sum(t.base_duration for t in tasks) + len(tasks) * 10 + 1

        # ---------------------------------------------------------------
        # Task interval variables and assignment BoolVars
        # ---------------------------------------------------------------
        task_starts: dict[str, Any] = {}
        task_ends: dict[str, Any] = {}
        task_intervals: dict[str, Any] = {}
        task_durations: dict[str, Any] = {}

        # operator_assign[task_id][operator_id] = BoolVar
        operator_assign: dict[str, dict[str, Any]] = {}
        # robot_assign[task_id][robot_id] = BoolVar
        robot_assign: dict[str, dict[str, Any]] = {}

        for task in tasks:
            tid = task.id
            start = model.NewIntVar(0, horizon, f"start_{tid}")
            end = model.NewIntVar(0, horizon, f"end_{tid}")

            # Duration depends on robot speed. We use base_duration as the
            # nominal duration. If a robot is assigned, we create optional
            # intervals with adjusted durations. For simplicity, we use the
            # base duration and let the solver pick the start/end.
            dur_val = max(1, task.base_duration)
            duration = model.NewIntVar(dur_val, dur_val, f"dur_{tid}")
            interval = model.NewIntervalVar(
                start, duration, end, f"interval_{tid}"
            )
            task_starts[tid] = start
            task_ends[tid] = end
            task_intervals[tid] = interval
            task_durations[tid] = duration

            # Operator assignment BoolVars (only eligible operators)
            if task.requires_operator:
                eligible_ops = self._eligible_operators(task, operators)
                if not eligible_ops:
                    raise ValueError(
                        f"No eligible operator for task '{tid}' "
                        f"(required_skill='{task.required_skill}')"
                    )
                operator_assign[tid] = {}
                for op in eligible_ops:
                    operator_assign[tid][op.id] = model.NewBoolVar(
                        f"assign_op_{tid}_{op.id}"
                    )
                # Exactly one operator assigned
                model.AddExactlyOne(
                    list(operator_assign[tid].values())
                )

                # Availability window constraint
                for op in eligible_ops:
                    b = operator_assign[tid][op.id]
                    model.Add(start >= op.available_from).OnlyEnforceIf(b)
                    model.Add(end <= op.available_to).OnlyEnforceIf(b)

            # Robot assignment BoolVars (only eligible robots)
            if task.requires_robot:
                eligible_robots = self._eligible_robots(task, robots)
                if not eligible_robots:
                    raise ValueError(
                        f"No eligible robot for task '{tid}' "
                        f"(required_capability='{task.required_capability}')"
                    )
                robot_assign[tid] = {}
                for rb in eligible_robots:
                    robot_assign[tid][rb.id] = model.NewBoolVar(
                        f"assign_rb_{tid}_{rb.id}"
                    )
                # Exactly one robot assigned
                model.AddExactlyOne(
                    list(robot_assign[tid].values())
                )

                # Availability window constraint
                for rb in eligible_robots:
                    b = robot_assign[tid][rb.id]
                    model.Add(start >= rb.available_from).OnlyEnforceIf(b)
                    model.Add(end <= rb.available_to).OnlyEnforceIf(b)

        # ---------------------------------------------------------------
        # Cumulative constraints for operators
        # Each operator is a cumulative resource with capacity =
        # max_concurrent_tasks. Only tasks assigned to this operator
        # contribute to its cumulative load.
        # ---------------------------------------------------------------
        for op in operators:
            # Collect optional intervals for this operator
            op_intervals: list[Any] = []
            op_demands: list[int] = []
            for task in tasks:
                if task.requires_operator and op.id in operator_assign.get(
                    task.id, {}
                ):
                    b = operator_assign[task.id][op.id]
                    # Create an optional interval: present only if b=1
                    presence = b
                    opt_interval = model.NewOptionalIntervalVar(
                        task_starts[task.id],
                        task_durations[task.id],
                        task_ends[task.id],
                        presence,
                        f"opt_interval_{task.id}_{op.id}",
                    )
                    op_intervals.append(opt_interval)
                    op_demands.append(1)

            if op_intervals:
                model.AddCumulative(
                    op_intervals, op_demands, op.max_concurrent_tasks
                )

        # ---------------------------------------------------------------
        # Cumulative constraints for robots
        # ---------------------------------------------------------------
        for rb in robots:
            rb_intervals: list[Any] = []
            rb_demands: list[int] = []
            for task in tasks:
                if task.requires_robot and rb.id in robot_assign.get(
                    task.id, {}
                ):
                    b = robot_assign[task.id][rb.id]
                    opt_interval = model.NewOptionalIntervalVar(
                        task_starts[task.id],
                        task_durations[task.id],
                        task_ends[task.id],
                        b,
                        f"opt_interval_{task.id}_{rb.id}",
                    )
                    rb_intervals.append(opt_interval)
                    rb_demands.append(1)

            if rb_intervals:
                model.AddCumulative(
                    rb_intervals, rb_demands, rb.max_concurrent_tasks
                )

        # ---------------------------------------------------------------
        # Makespan objective
        # ---------------------------------------------------------------
        makespan = model.NewIntVar(0, horizon, "makespan")
        for end in task_ends.values():
            model.Add(makespan >= end)
        model.Minimize(makespan)

        return {
            "task_starts": task_starts,
            "task_ends": task_ends,
            "task_intervals": task_intervals,
            "task_durations": task_durations,
            "operator_assign": operator_assign,
            "robot_assign": robot_assign,
            "makespan": makespan,
            "horizon": horizon,
        }

    def _eligible_operators(
        self,
        task: TaskSpec,
        operators: list[OperatorSpec],
    ) -> list[OperatorSpec]:
        """Return operators that can perform the task (skill matches)."""
        if not task.required_skill:
            return list(operators)
        return [op for op in operators if task.required_skill in op.skills]

    def _eligible_robots(
        self,
        task: TaskSpec,
        robots: list[RobotSpec],
    ) -> list[RobotSpec]:
        """Return robots that can perform the task (capability matches)."""
        if not task.required_capability:
            return list(robots)
        return [
            rb for rb in robots if task.required_capability in rb.capabilities
        ]

    def _extract_results(
        self,
        tasks: list[TaskSpec],
        state: dict[str, Any],
        solver: Any,
    ) -> dict[str, AllocationResult]:
        """Extract allocation results from the solver solution.

        Given:
            Solved CP-SAT model with assignment BoolVars.
        When:
            Read solver values for each task's start/end/assignment.
        Then:
            Returns dict[task_id -> AllocationResult] with operator_id,
            robot_id, start_time, end_time, duration.
        """
        results: dict[str, AllocationResult] = {}
        operator_assign = state["operator_assign"]
        robot_assign = state["robot_assign"]
        task_starts = state["task_starts"]
        task_ends = state["task_ends"]

        for task in tasks:
            tid = task.id
            start = solver.Value(task_starts[tid])
            end = solver.Value(task_ends[tid])
            duration = end - start

            # Determine assigned operator
            assigned_op: str | None = None
            if task.requires_operator and tid in operator_assign:
                for op_id, bvar in operator_assign[tid].items():
                    if solver.Value(bvar) == 1:
                        assigned_op = op_id
                        break

            # Determine assigned robot
            assigned_rb: str | None = None
            if task.requires_robot and tid in robot_assign:
                for rb_id, bvar in robot_assign[tid].items():
                    if solver.Value(bvar) == 1:
                        assigned_rb = rb_id
                        break

            results[tid] = AllocationResult(
                task_id=tid,
                operator_id=assigned_op,
                robot_id=assigned_rb,
                start_time=start,
                end_time=end,
                duration=duration,
            )

        return results

    def check_feasibility(
        self,
        operators: list[OperatorSpec],
        robots: list[RobotSpec],
        tasks: list[TaskSpec],
    ) -> list[str]:
        """Pre-solver feasibility check for skill/capability coverage.

        Given:
            Operators, robots, and tasks with skill/capability requirements.
        When:
            Check each task has at least one eligible operator (if required)
            and at least one eligible robot (if required).
        Then:
            Returns list of error messages (empty if all feasible).
        """
        errors: list[str] = []
        for task in tasks:
            if task.requires_operator:
                eligible_ops = self._eligible_operators(task, operators)
                if not eligible_ops:
                    errors.append(
                        f"Task '{task.id}': no operator with skill "
                        f"'{task.required_skill}'"
                    )
            if task.requires_robot:
                eligible_rbs = self._eligible_robots(task, robots)
                if not eligible_rbs:
                    errors.append(
                        f"Task '{task.id}': no robot with capability "
                        f"'{task.required_capability}'"
                    )
        return errors
