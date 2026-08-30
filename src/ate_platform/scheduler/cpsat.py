# mypy: ignore-errors
"""CP-SAT scheduler for global optimal step scheduling.

Uses OR-Tools CP-SAT solver to compute a globally optimal schedule
minimizing makespan, respecting step dependencies and shared resource
constraints. Falls back to topological scheduler on timeout.

Supports multi-objective Pareto optimization via epsilon-constraint method
for makespan, resource utilization, and energy cost.

Usage:
    from ate_platform.scheduler.cpsat import CPSATScheduler

    scheduler = CPSATScheduler(time_limit=5.0)
    schedule = scheduler.schedule(steps)  # steps: list[YamlStep]
    # schedule: dict[str, tuple[int, int, int]] 鈥?{step_id: (start, end, wave)}

    # Multi-objective Pareto frontier
    pareto = scheduler.schedule_pareto(steps)
    # pareto: list[dict] 鈥?each with 'schedule', 'makespan', 'utilization', 'energy'

Edge cases:
    - Empty plan 鈫?{}
    - Single step 鈫?{step_id: (0, duration, 1)}
    - Linear chain 鈫?one wave per step
    - Diamond dependency 鈫?concurrent independent steps share waves
    - Shared resource 鈫?serialized even if otherwise independent
    - Timeout 鈫?returns None (caller falls back to topological)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shared.dsl import YamlStep

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentinel for missing ortools
# ---------------------------------------------------------------------------
_ORTOOLS_AVAILABLE: bool = False
_cp_model: Any = None
_ORTOOLS_IMPORT_ERROR: str | None = None

try:
    from ortools.sat.python import cp_model

    _ORTOOLS_AVAILABLE = True
except ImportError as exc:
    _ORTOOLS_IMPORT_ERROR = str(exc)

# ---------------------------------------------------------------------------
# Default epsilon ratio for epsilon-constraint Pareto method
# ---------------------------------------------------------------------------
_DEFAULT_EPSILON_RATIO = 0.05  # allow 5% relaxation

# ---------------------------------------------------------------------------
# Solver status constants (for clarity)
# ---------------------------------------------------------------------------
_OPTIMAL_OR_FEASIBLE = {cp_model.OPTIMAL, cp_model.FEASIBLE} if _ORTOOLS_AVAILABLE else set()


class CPSATScheduler:
    """Constraint Programming SAT scheduler for global optimal scheduling.

    Maps steps to CP-SAT interval variables with:
    - Duration constraints (step's estimated runtime, default 1)
    - Precedence constraints (step.depends_on 鈫?step)
    - Cumulative constraints (shared resources limit concurrency)
    - Objective: minimize makespan (max end time of all intervals)

    Supports multi-objective Pareto optimization via schedule_pareto().

    If the solver times out (default 5s), returns None so the caller
    can fall back to the existing topological scheduler.
    """

    def __init__(self, time_limit: float = 5.0) -> None:
        """Initialize the CP-SAT scheduler.

        Args:
            time_limit: Solver time limit in seconds. Default 5.0.
                If the solver exceeds this limit, schedule() returns None.
        """
        self._time_limit = time_limit
        if not _ORTOOLS_AVAILABLE:
            logger.warning(
                "OR-Tools not available: %s 鈥?CPSATScheduler will always return None",
                _ORTOOLS_IMPORT_ERROR,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def schedule(
        self,
        steps: list[YamlStep],
        objectives: list[str] | None = None,
        fault_penalty: dict[str, int] | None = None,
        changeover_matrix: dict[tuple[str, str], int] | None = None,
        human_resources: dict[str, Any] | None = None,
    ) -> dict[str, tuple[int, int, int]] | None:
        """Compute an optimal schedule via CP-SAT.

        Args:
            steps: List of YamlStep instances to schedule.
            objectives: Optional list of objective names. Supported:
                "makespan" (default), "utilization", "energy".
                When a single objective is given, only that is optimized.
                For multi-objective, use schedule_pareto() instead.
            fault_penalty: Optional dict mapping step_id → penalty weight.
                When provided, the solver adds a soft constraint that
                penalizes scheduling high-risk steps early. Each weight
                is multiplied by the step's start time in the objective.
            changeover_matrix: Optional dict mapping
                (product_type_a, product_type_b) → transition cost.
                When provided, the solver adds sequence-dependent setup
                costs between steps of different product types. Steps
                carry their product type via ``step.params.get("product_type")``.
                The optimizer minimizes makespan + total changeover cost.
            human_resources: Optional dict with ``"operators"`` and
                ``"robots"`` keys (lists of OperatorSpec / RobotSpec from
                ``human_resource_allocator``). When provided, the solver
                adds cumulative constraints for each operator and robot
                (capacity = max_concurrent_tasks) and alternative-resource
                constraints ensuring each step requiring a skill is
                assigned to an eligible operator. Steps declare their
                required skill via ``step.params.get("required_skill")``.

        Returns:
            Dict mapping step_id → (start_time, end_time, wave_number),
            or None if ortools is unavailable or the solver times out.
        """
        if not steps:
            return {}

        if not _ORTOOLS_AVAILABLE:
            logger.debug("OR-Tools not available — returning None for fallback")
            return None

        objective_list = objectives or ["makespan"]
        # For single objective, optimize directly
        model, state = self._build_model(
            steps,
            objective_list[0],
            fault_penalty,
            changeover_matrix,
            human_resources,
        )
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self._time_limit
        solver.parameters.log_search_progress = False

        status = solver.Solve(model)

        if status in _OPTIMAL_OR_FEASIBLE:
            return self._extract_schedule(
                steps,
                state["step_starts"],
                state["step_ends"],
                state["step_durations"],
                solver,
            )

        if status == cp_model.INFEASIBLE:
            logger.warning("CP-SAT model is infeasible 鈥?returning None for fallback")
            return None

        logger.debug(
            "CP-SAT solver status %s 鈥?returning None for fallback", status
        )
        return None

    def schedule_pareto(
        self,
        steps: list[YamlStep],
        epsilon_ratio: float = _DEFAULT_EPSILON_RATIO,
    ) -> list[dict[str, Any]]:
        """Compute Pareto-optimal schedules across multiple objectives.

        Uses the epsilon-constraint method:
        1. Solve each objective individually to get optimal bounds.
        2. Systematically constrain one objective and optimize another.
        3. Collect unique non-dominated schedules.

        Args:
            steps: List of YamlStep instances to schedule.
            epsilon_ratio: Relaxation ratio for constraint bounds (default 0.05).
                Lower values produce tighter Pareto frontiers but fewer solutions.

        Returns:
            List of Pareto-optimal schedule dicts, each with:
              - 'schedule': dict[str, tuple[int, int, int]]
              - 'makespan': int (total time)
              - 'utilization': float (0.0鈥?.0, average resource utilization)
              - 'energy': float (total power-weighted duration)

            Returns empty list if ortools unavailable or no feasible solutions.
        """
        if not steps:
            return []

        if not _ORTOOLS_AVAILABLE:
            logger.debug("OR-Tools not available 鈥?returning empty Pareto frontier")
            return []

        # ------------------------------------------------------------------
        # Step 1: Compute individual optima for each objective
        # ------------------------------------------------------------------
        objectives = ["makespan", "utilization", "energy"]
        individual_optima: dict[str, tuple[float, dict[str, tuple[int, int, int]]]] = {}

        for obj in objectives:
            model, state = self._build_model(steps, obj)
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = self._time_limit
            solver.parameters.log_search_progress = False
            status = solver.Solve(model)

            if status in _OPTIMAL_OR_FEASIBLE:
                raw = self._extract_raw_schedule(
                    steps, state["step_starts"], state["step_ends"], solver
                )
                schedule = self._extract_schedule(
                    steps,
                    state["step_starts"],
                    state["step_ends"],
                    state["step_durations"],
                    solver,
                )
                scores = self._compute_objective_scores(steps, raw, schedule)
                individual_optima[obj] = (scores[obj], schedule)
            else:
                logger.debug("Solver failed for objective %s: status %s", obj, status)
                individual_optima[obj] = (float("inf") if obj != "utilization" else 0.0, {})

        # If makespan optimum is inf, nothing works
        ms_opt = individual_optima.get("makespan", (float("inf"), {}))
        if ms_opt[0] == float("inf") and not ms_opt[1]:
            return []

        # ------------------------------------------------------------------
        # Step 2: Epsilon-constraint method
        #
        # Strategy: Force the solver to find schedules at progressively
        # larger makespan values by adding a lower bound (makespan >= target).
        # This creates genuine trade-offs:
        #   - smaller makespan 鈫?better utilization, less idle energy
        #   - larger makespan 鈫?worse utilization, more idle energy
        # ------------------------------------------------------------------
        pareto_set: dict[tuple, dict[str, Any]] = {}

        ms_opt_value = int(individual_optima["makespan"][0])
        eps_ms = max(1, int(ms_opt_value * epsilon_ratio))

        def _solve_with_lower_bound(lb: int, ub: int | None = None) -> None:
            """Build feasibility model with makespan in [lb, ub] and solve."""
            model, state = self._build_model(steps, "makespan")
            model.Add(state["makespan"] >= lb)
            if ub is not None:
                model.Add(state["makespan"] <= ub)

            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = self._time_limit
            solver.parameters.log_search_progress = False
            status = solver.Solve(model)

            if status in _OPTIMAL_OR_FEASIBLE:
                raw = self._extract_raw_schedule(
                    steps, state["step_starts"], state["step_ends"], solver
                )
                schedule = self._extract_schedule(
                    steps,
                    state["step_starts"],
                    state["step_ends"],
                    state["step_durations"],
                    solver,
                )
                scores = self._compute_objective_scores(steps, raw, schedule)
                key = (
                    int(scores["makespan"]),
                    round(scores["utilization"], 4),
                    round(scores["energy"], 2),
                )
                if key not in pareto_set:
                    pareto_set[key] = {
                        "schedule": schedule,
                        "makespan": scores["makespan"],
                        "utilization": scores["utilization"],
                        "energy": scores["energy"],
                    }

        # Include the individual optimum solutions
        for obj, (_, sch) in individual_optima.items():
            if sch:
                model, state = self._build_model(steps, obj)
                solver = cp_model.CpSolver()
                solver.parameters.max_time_in_seconds = self._time_limit
                solver.Solve(model)
                raw = self._extract_raw_schedule(
                    steps, state["step_starts"], state["step_ends"], solver
                )
                scores = self._compute_objective_scores(steps, raw, sch)
                key = (
                    int(scores["makespan"]),
                    round(scores["utilization"], 4),
                    round(scores["energy"], 2),
                )
                if key not in pareto_set:
                    pareto_set[key] = {
                        "schedule": sch,
                        "makespan": scores["makespan"],
                        "utilization": scores["utilization"],
                        "energy": scores["energy"],
                    }

        # Explore relaxed makespans: optimum + k*eps for k = 1..N
        num_steps_grid = min(10, max(3, ms_opt_value // eps_ms))
        for k in range(1, num_steps_grid + 1):
            lb = ms_opt_value + k * eps_ms
            _solve_with_lower_bound(lb)

        # Also try tighter windows [lb, lb+eps] for finer granularity
        for k in range(1, num_steps_grid):
            lb = ms_opt_value + k * eps_ms
            _solve_with_lower_bound(lb, lb + eps_ms)

        # ------------------------------------------------------------------
        # Step 3: Filter to true Pareto frontier (non-dominated)
        # ------------------------------------------------------------------
        all_solutions = list(pareto_set.values())
        pareto_frontier = self._filter_pareto(all_solutions)

        return pareto_frontier

    # ------------------------------------------------------------------
    # Model building (extracted for reuse)
    # ------------------------------------------------------------------

    def _build_model(
        self,
        steps: list[YamlStep],
        objective: str = "makespan",
        fault_penalty: dict[str, int] | None = None,
        changeover_matrix: dict[tuple[str, str], int] | None = None,
        human_resources: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Build a CP-SAT model with the specified objective.

        Args:
            steps: List of YamlStep instances.
            objective: One of "makespan", "utilization", "energy".
            fault_penalty: Optional dict mapping step_id → penalty weight.
                When provided, adds a soft constraint penalizing early
                scheduling of high-risk steps (penalty_weight × start_time).
            changeover_matrix: Optional dict mapping
                (product_type_a, product_type_b) → transition cost.
                When provided, adds sequence-dependent setup costs between
                steps of different product types. Steps carry their product
                type via ``step.params.get("product_type")``.
            human_resources: Optional dict with ``"operators"`` and
                ``"robots"`` keys. When provided, adds cumulative constraints
                for operators and robots, plus alternative-resource
                assignment for steps with ``required_skill`` in params.

        Returns:
            Tuple of (model, state_dict) where state_dict contains:
                step_starts, step_ends, step_intervals, step_durations,
                makespan, total_active_time, energy_cost, horizon, num_resources.
        """
        model = cp_model.CpModel()

        # Determine a safe horizon 鈥?sum of all durations + a buffer
        step_durations: dict[str, int] = {}
        for step in steps:
            dur = max(1, step.timeout) if step.timeout > 0 else 1
            step_durations[step.id] = dur

        horizon = sum(step_durations.values()) + len(steps)

        # Count distinct resources
        resource_set: set[str] = set()
        for step in steps:
            if step.resources:
                resource_set.update(step.resources.keys())
        num_resources = max(1, len(resource_set))

        # Create interval variables for each step
        step_starts: dict[str, Any] = {}
        step_ends: dict[str, Any] = {}
        step_intervals: dict[str, Any] = {}

        for step in steps:
            sid = step.id
            dur = step_durations[sid]
            start = model.NewIntVar(0, horizon, f"start_{sid}")
            end = model.NewIntVar(0, horizon, f"end_{sid}")
            duration = model.NewIntVar(dur, dur, f"dur_{sid}")
            interval = model.NewIntervalVar(start, duration, end, f"interval_{sid}")
            step_starts[sid] = start
            step_ends[sid] = end
            step_intervals[sid] = interval

        # Precedence constraints
        step_ids = {s.id for s in steps}
        for step in steps:
            for dep_id in step.preconditions:
                if dep_id not in step_ids:
                    continue
                model.Add(step_starts[step.id] >= step_ends[dep_id])

        # Resource cumulative constraints
        resource_usage: dict[str, list[Any]] = {}
        for step in steps:
            if step.resources:
                for resource_id in step.resources:
                    resource_usage.setdefault(resource_id, []).append(
                        step_intervals[step.id]
                    )

        for _resource_id, intervals in resource_usage.items():
            demands = [1] * len(intervals)
            model.AddCumulative(intervals, demands, 1)

        # ---------------------------------------------------------------
        # Human resource constraints (operators + robots as cumulative
        # resources with alternative-resource assignment)
        # ---------------------------------------------------------------
        if human_resources:
            operators = human_resources.get("operators", [])
            robots_list = human_resources.get("robots", [])

            # Operator assignment: each step with a required_skill gets
            # assigned to exactly one eligible operator. The operator is
            # a cumulative resource with capacity = max_concurrent_tasks.
            step_op_assign: dict[str, dict[str, Any]] = {}

            for step in steps:
                required_skill = (
                    step.params.get("required_skill") if step.params else None
                )
                if not isinstance(required_skill, str) or not required_skill:
                    continue

                eligible_ops = [
                    op
                    for op in operators
                    if required_skill in getattr(op, "skills", [])
                ]
                if not eligible_ops:
                    continue

                step_op_assign[step.id] = {}
                for op in eligible_ops:
                    step_op_assign[step.id][op.id] = model.NewBoolVar(
                        f"hr_op_{step.id}_{op.id}"
                    )
                # Alternative resource: exactly one operator per step
                model.AddExactlyOne(list(step_op_assign[step.id].values()))

            # Cumulative constraint per operator (optional intervals)
            for op in operators:
                op_opt_intervals: list[Any] = []
                op_demands: list[int] = []
                for step in steps:
                    if step.id not in step_op_assign:
                        continue
                    if op.id not in step_op_assign[step.id]:
                        continue
                    b = step_op_assign[step.id][op.id]
                    opt_interval = model.NewOptionalIntervalVar(
                        step_starts[step.id],
                        step_durations[step.id],
                        step_ends[step.id],
                        b,
                        f"hr_opt_{step.id}_{op.id}",
                    )
                    op_opt_intervals.append(opt_interval)
                    op_demands.append(1)

                if op_opt_intervals:
                    capacity = getattr(op, "max_concurrent_tasks", 1)
                    model.AddCumulative(op_opt_intervals, op_demands, capacity)

            # Robot assignment: each step with a required_capability gets
            # assigned to exactly one eligible robot.
            step_rb_assign: dict[str, dict[str, Any]] = {}

            for step in steps:
                required_cap = (
                    step.params.get("required_capability")
                    if step.params
                    else None
                )
                if not isinstance(required_cap, str) or not required_cap:
                    continue

                eligible_robots = [
                    rb
                    for rb in robots_list
                    if required_cap in getattr(rb, "capabilities", [])
                ]
                if not eligible_robots:
                    continue

                step_rb_assign[step.id] = {}
                for rb in eligible_robots:
                    step_rb_assign[step.id][rb.id] = model.NewBoolVar(
                        f"hr_rb_{step.id}_{rb.id}"
                    )
                model.AddExactlyOne(list(step_rb_assign[step.id].values()))

            # Cumulative constraint per robot (optional intervals)
            for rb in robots_list:
                rb_opt_intervals: list[Any] = []
                rb_demands: list[int] = []
                for step in steps:
                    if step.id not in step_rb_assign:
                        continue
                    if rb.id not in step_rb_assign[step.id]:
                        continue
                    b = step_rb_assign[step.id][rb.id]
                    opt_interval = model.NewOptionalIntervalVar(
                        step_starts[step.id],
                        step_durations[step.id],
                        step_ends[step.id],
                        b,
                        f"hr_rbopt_{step.id}_{rb.id}",
                    )
                    rb_opt_intervals.append(opt_interval)
                    rb_demands.append(1)

                if rb_opt_intervals:
                    capacity = getattr(rb, "max_concurrent_tasks", 1)
                    model.AddCumulative(
                        rb_opt_intervals, rb_demands, capacity
                    )

        # Makespan variable
        makespan = model.NewIntVar(0, horizon, "makespan")
        for end in step_ends.values():
            model.Add(makespan >= end)

        # Total active time (sum of all step durations, for utilization)
        total_active_time = sum(step_durations.values())

        # Energy cost = sum(power_i 脳 duration_i) + idle_power 脳 (makespan 脳 num_resources - total_active_time)
        # This creates a genuine trade-off: shorter makespan reduces idle energy
        idle_power = 0.5  # default idle power per resource per time unit
        energy_cost = model.NewIntVar(0, horizon * 1000, "energy_cost")
        # Active energy = sum(power_i 脳 duration_i) 鈥?constant for fixed durations
        power_scale = 10
        active_energy = 0
        for step in steps:
            power = getattr(step, "power", 1.0)
            active_energy += int(power * power_scale) * step_durations[step.id]
        # Idle energy = idle_power 脳 (makespan 脳 num_resources - total_active_time)
        # = idle_power 脳 num_resources 脳 makespan - idle_power 脳 total_active_time
        idle_scale = int(idle_power * power_scale)
        # Energy = active_energy + idle_scale * num_resources * makespan - idle_scale * total_active_time
        total_active_time = sum(step_durations.values())
        # model.Add(energy_cost == active_energy + idle_scale * num_resources
        #           * makespan - idle_scale * total_active_time)
        # Simplify: the constant part doesn't matter for minimization, but matters for Pareto comparison
        energy_constant = active_energy - idle_scale * total_active_time
        model.Add(energy_cost == energy_constant + idle_scale * num_resources * makespan)

        # State dict
        state: dict[str, Any] = {
            "step_starts": step_starts,
            "step_ends": step_ends,
            "step_intervals": step_intervals,
            "step_durations": step_durations,
            "makespan": makespan,
            "total_active_time": total_active_time,
            "energy_cost": energy_cost,
            "horizon": horizon,
            "num_resources": num_resources,
        }

        # Set objective
        # When fault_penalty is provided, add a soft constraint term:
        # total_objective = base_objective + sum(penalty_weight * (horizon - start))
        # The solver minimizes, so high-weight (high-risk) steps get more
        # penalty when starting early (horizon - start is large). This
        # pushes high-risk steps to later start times, prioritizing
        # low-risk steps early in the schedule.
        fault_penalty_term = 0
        has_fault_penalty = False
        if fault_penalty:
            for step in steps:
                weight = fault_penalty.get(step.id, 0)
                if weight > 0:
                    fault_penalty_term += weight * (horizon - step_starts[step.id])
                    has_fault_penalty = True

        # Changeover cost: sequence-dependent setup cost between steps of
        # different product types. For each pair of steps (i, j) that share
        # a resource and are thus serialized, if they belong to different
        # product types, add the transition cost from the changeover matrix.
        # This is modeled as a soft penalty on the objective.
        changeover_cost_term = 0
        has_changeover_cost = False
        if changeover_matrix:
            # Extract product types from step params
            step_products: dict[str, str] = {}
            for step in steps:
                pt = step.params.get("product_type") if step.params else None
                if isinstance(pt, str):
                    step_products[step.id] = pt

            # For steps sharing a resource, add changeover cost based on
            # which product runs first. The solver decides the order.
            resource_to_steps: dict[str, list[str]] = {}
            for step in steps:
                if step.resources:
                    for rid in step.resources:
                        resource_to_steps.setdefault(rid, []).append(step.id)

            for rid, sids in resource_to_steps.items():
                if len(sids) < 2:
                    continue
                for i, sid_i in enumerate(sids):
                    for sid_j in sids[i + 1:]:
                        pt_i = step_products.get(sid_i)
                        pt_j = step_products.get(sid_j)
                        if pt_i is None or pt_j is None or pt_i == pt_j:
                            continue
                        # Cost if i runs before j
                        cost_ij = changeover_matrix.get((pt_i, pt_j), 0)
                        # Cost if j before i
                        cost_ji = changeover_matrix.get((pt_j, pt_i), 0)
                        if cost_ij == 0 and cost_ji == 0:
                            continue
                        # b = 1 iff i runs before j (end_i <= start_j)
                        b = model.NewBoolVar(f"chg_{rid}_{sid_i}_{sid_j}")
                        model.Add(step_ends[sid_i] <= step_starts[sid_j]).OnlyEnforceIf(b)
                        model.Add(step_ends[sid_i] > step_starts[sid_j]).OnlyEnforceIf(b.Not())
                        # Add cost: if i before j, pay cost_ij; if j before i, pay cost_ji
                        changeover_cost_term += cost_ij * b + cost_ji * (1 - b)
                        has_changeover_cost = True

        if has_fault_penalty or has_changeover_cost:
            # Combined objective: base objective + fault penalty + changeover cost
            if objective == "energy":
                base_obj = energy_cost
            else:
                # makespan and utilization both minimize makespan
                base_obj = makespan
            model.Minimize(base_obj + fault_penalty_term + changeover_cost_term)
        elif objective == "makespan":
            model.Minimize(makespan)
        elif objective == "utilization":
            # Maximize utilization = minimize -total_active_time/makespan
            # Equivalent to minimizing makespan for fixed total_active_time
            model.Minimize(makespan)
        elif objective == "energy":
            model.Minimize(energy_cost)
        else:
            model.Minimize(makespan)

        return model, state

    # ------------------------------------------------------------------
    # Objective score computation
    # ------------------------------------------------------------------

    def _compute_objective_scores(
        self,
        steps: list[YamlStep],
        raw: dict[str, tuple[int, int]],
        schedule: dict[str, tuple[int, int, int]],
    ) -> dict[str, float]:
        """Compute makespan, utilization, and energy scores for a schedule.

        Energy = sum(step_power 脳 duration) + idle_power 脳 (makespan 脳 num_resources - total_active)
        This creates a genuine trade-off: longer makespan = more idle energy waste.

        Args:
            steps: The YamlStep list.
            raw: Dict of step_id 鈫?(start_time, end_time).
            schedule: Dict of step_id 鈫?(start_time, end_time, wave).

        Returns:
            Dict with keys: makespan (int), utilization (float 0鈥?), energy (float).
        """
        # Count resources
        resource_set: set[str] = set()
        for step in steps:
            if step.resources:
                resource_set.update(step.resources.keys())
        num_resources = max(1, len(resource_set))

        # Makespan = max end time
        max_end = max(e for _, e in raw.values()) if raw else 0

        # Total active time = sum of all step durations
        step_durations: dict[str, int] = {}
        for step in steps:
            dur = max(1, step.timeout) if step.timeout > 0 else 1
            step_durations[step.id] = dur
        total_active = sum(step_durations.values())

        # Resource utilization = total_active_time / (makespan 脳 num_resources)
        if max_end > 0 and num_resources > 0:
            utilization = total_active / (max_end * num_resources)
        else:
            utilization = 0.0

        # Energy cost = active energy + idle energy
        # active = sum(step_power 脳 duration)
        # idle = idle_power 脳 (makespan 脳 num_resources - total_active_time)
        idle_power = 0.5
        active_energy = 0.0
        for step in steps:
            power = getattr(step, "power", 1.0)
            dur = step_durations[step.id]
            active_energy += power * dur

        idle_energy = idle_power * (max_end * num_resources - total_active)
        energy = active_energy + idle_energy

        return {
            "makespan": float(max_end),
            "utilization": utilization,
            "energy": energy,
        }

    # ------------------------------------------------------------------
    # Pareto filtering
    # ------------------------------------------------------------------

    def _filter_pareto(
        self,
        solutions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Filter a list of solutions to the Pareto frontier.

        A solution is non-dominated if no other solution is strictly better
        in all objectives. For us: lower makespan, higher utilization, lower energy.

        Args:
            solutions: List of solution dicts with makespan/utilization/energy keys.

        Returns:
            Subset of solutions that are Pareto-optimal.
        """
        if not solutions:
            return []

        n = len(solutions)
        dominated = [False] * n

        for i in range(n):
            for j in range(n):
                if i == j or dominated[j]:
                    continue
                si = solutions[i]
                sj = solutions[j]
                # j dominates i if j is better in all objectives and strictly better in at least one
                ms_better = sj["makespan"] <= si["makespan"]
                util_better = sj["utilization"] >= si["utilization"]
                energy_better = sj["energy"] <= si["energy"]
                strictly_better = (
                    sj["makespan"] < si["makespan"]
                    or sj["utilization"] > si["utilization"]
                    or sj["energy"] < si["energy"]
                )
                if ms_better and util_better and energy_better and strictly_better:
                    dominated[i] = True
                    break

        return [s for i, s in enumerate(solutions) if not dominated[i]]

    # ------------------------------------------------------------------
    # Schedule extraction helpers
    # ------------------------------------------------------------------

    def _extract_raw_schedule(
        self,
        steps: list[YamlStep],
        step_starts: dict[str, Any],
        step_ends: dict[str, Any],
        solver: Any,
    ) -> dict[str, tuple[int, int]]:
        """Extract (start, end) from solver solution without wave assignment."""
        raw: dict[str, tuple[int, int]] = {}
        for step in steps:
            sid = step.id
            raw[sid] = (
                solver.Value(step_starts[sid]),
                solver.Value(step_ends[sid]),
            )
        return raw

    def _extract_schedule(
        self,
        steps: list[YamlStep],
        step_starts: dict[str, Any],
        step_ends: dict[str, Any],
        step_durations: dict[str, int],
        solver: Any,
    ) -> dict[str, tuple[int, int, int]]:
        """Extract (start, end, wave) from solver solution.

        Wave assignment uses topological depth in the combined dependency +
        resource-conflict DAG. Steps that can run concurrently (no
        dependency edge and no shared resource) share the same wave.
        """
        # Collect raw (start, end) for each step
        raw: dict[str, tuple[int, int]] = {}
        for step in steps:
            sid = step.id
            raw[sid] = (
                solver.Value(step_starts[sid]),
                solver.Value(step_ends[sid]),
            )

        step_ids = {s.id for s in steps}

        # Build the combined DAG: edges from dependencies + resource sharing
        predecessors: dict[str, set[str]] = {s.id: set() for s in steps}

        # 1) Explicit dependency edges
        for step in steps:
            for dep_id in step.preconditions:
                if dep_id in step_ids:
                    predecessors[step.id].add(dep_id)

        # 2) Implicit resource-conflict edges: if two steps share a resource
        #    and the solver serialized them, the one starting later depends
        #    on the one starting earlier
        resource_steps: dict[str, list[str]] = {}
        for step in steps:
            if step.resources:
                for rid in step.resources:
                    resource_steps.setdefault(rid, []).append(step.id)

        for _rid, sids in resource_steps.items():
            sorted_by_start = sorted(sids, key=lambda sid: raw[sid][0])
            for i in range(1, len(sorted_by_start)):
                predecessors[sorted_by_start[i]].add(sorted_by_start[i - 1])

        # 3) Topological depth: wave = 1 + max(predecessor wave, 0)
        wave_assignment: dict[str, int] = {}
        step_ids_list = list(step_ids)

        while len(wave_assignment) < len(steps):
            for sid in step_ids_list:
                if sid in wave_assignment:
                    continue
                preds = predecessors[sid]
                if preds.issubset(wave_assignment.keys()):
                    wave = 1
                    if preds:
                        wave = 1 + max(wave_assignment[p] for p in preds)
                    wave_assignment[sid] = wave

        return {
            sid: (raw[sid][0], raw[sid][1], wave_assignment[sid])
            for sid in raw
        }
