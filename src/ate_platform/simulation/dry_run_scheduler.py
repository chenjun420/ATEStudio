"""Tier 2: Dry-Run Scheduler - full scheduling graph traversal without execution.

The DryRunScheduler traverses the complete scheduling graph:
- Evaluates all step preconditions via ConditionEvaluator
- Acquires/releases resources via ResourceManager
- Handles skip_if expressions
- Traverses YamlLoop structures (FOR/WHILE/FOREACH)
- Records pass/fail/skip decisions for every step

It does NOT:
- Call any StepExecutor or real instrument
- Spawn subprocesses or threads
- Modify the production scheduler's state

The scheduler produces a DryRunResult containing per-step StepDecision
records. Each decision captures the step_id, decision (PASS/FAIL/SKIP/
BLOCKED/ERROR), the reason, and timing information.

This is useful for:
- Validating test plan correctness before deployment
- Identifying deadlocks or unsatisfiable preconditions
- Verifying resource allocation sequences
- CI/CD pre-flight checks on test plans
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.scheduler.event_bus import EventBus
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.step_registry import StepRegistry
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import Condition, StepStatus
from shared.dsl import YamlLoop, YamlPlan, YamlStep

logger = logging.getLogger(__name__)

# Decision type for a single step in the dry run
StepDecisionType = Literal["PASS", "FAIL", "SKIP", "BLOCKED", "ERROR", "NOT_REACHED"]


@dataclass(slots=True)
class StepDecision:
    """Record of the dry-run decision for a single step.

    Attributes:
        step_id: The step identifier from the plan.
        decision: The dry-run decision type.
        reason: Human-readable explanation of the decision.
        condition_met: Whether the step's preconditions were satisfied.
        resources_acquired: List of resource IDs acquired for this step.
        skip_if_evaluated: The skip_if expression if one was present, None otherwise.
        skip_if_result: True if skip_if evaluated to True (step skipped), False otherwise.
            None if no skip_if was present.
        timestamp: Monotonic timestamp when the decision was recorded.
    """

    step_id: str
    decision: StepDecisionType
    reason: str
    condition_met: bool
    resources_acquired: list[str]
    skip_if_evaluated: str | None
    skip_if_result: bool | None
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class DryRunResult:
    """Aggregate result of a dry-run scheduling traversal.

    Attributes:
        plan_name: Name of the plan that was dry-run.
        plan_version: Version string of the plan.
        decisions: Ordered list of StepDecision records, one per step traversed.
        total_steps: Total number of steps in the plan (including loop children).
        passed: Count of steps with PASS decision.
        failed: Count of steps with FAIL decision.
        skipped: Count of steps with SKIP decision.
        blocked: Count of steps with BLOCKED decision.
        errors: Count of steps with ERROR decision.
        not_reached: Count of steps with NOT_REACHED decision.
        duration_s: Total wall-clock duration of the dry run in seconds.
        deadlock_detected: True if a resource deadlock was detected during traversal.
        deadlock_steps: List of step_ids involved in the detected deadlock.
    """

    plan_name: str
    plan_version: str
    decisions: list[StepDecision]
    total_steps: int
    passed: int
    failed: int
    skipped: int
    blocked: int
    errors: int
    not_reached: int
    duration_s: float
    deadlock_detected: bool
    deadlock_steps: list[str]

    @property
    def all_passed(self) -> bool:
        """True if no steps failed, errored, or were blocked."""
        return self.failed == 0 and self.errors == 0 and self.blocked == 0

    @property
    def summary(self) -> str:
        """Human-readable one-line summary of the dry run."""
        return (
            f"DryRun('{self.plan_name}'): "
            f"{self.passed} pass, {self.failed} fail, {self.skipped} skip, "
            f"{self.blocked} blocked, {self.errors} error, "
            f"{self.not_reached} not_reached "
            f"({'PASS' if self.all_passed else 'FAIL'}) "
            f"in {self.duration_s:.3f}s"
        )

    def get_decision(self, step_id: str) -> StepDecision | None:
        """Look up the decision for a specific step.

        Args:
            step_id: The step identifier to look up.

        Returns:
            The StepDecision for this step, or None if not found.
        """
        for decision in self.decisions:
            if decision.step_id == step_id:
                return decision
        return None

    def get_failed_steps(self) -> list[StepDecision]:
        """Get all steps with FAIL decision.

        Returns:
            List of StepDecision records for failed steps.
        """
        return [d for d in self.decisions if d.decision == "FAIL"]

    def get_skipped_steps(self) -> list[StepDecision]:
        """Get all steps with SKIP decision.

        Returns:
            List of StepDecision records for skipped steps.
        """
        return [d for d in self.decisions if d.decision == "SKIP"]


class DryRunScheduler:
    """Traverses the scheduling graph without executing real steps.

    The DryRunScheduler simulates the full scheduling pipeline:
    1. Register all steps from a YamlPlan into a StepRegistry
    2. Build preconditions from step.preconditions
    3. Traverse steps in order, evaluating conditions via ConditionEvaluator
    4. Acquire/release resources via ResourceManager
    5. Evaluate skip_if expressions
    6. Traverse YamlLoop structures (expanding FOR iterations)
    7. Record decisions for each step

    Unlike the production ScannerScheduler, this scheduler:
    - Runs synchronously (no asyncio event loop needed)
    - Does not emit events or start a scan loop
    - Does not call any StepExecutor
    - Uses its own isolated StepRegistry/ResourceManager instances

    Resource handling:
        Steps declare resources in their `resources` dict (keys are resource IDs).
        The dry-run acquires each resource before the step and releases after.
        If acquisition fails (resource held by another step), the step is
        marked BLOCKED. Deadlock is detected when all remaining steps are blocked.

    Loop handling:
        YamlLoop steps are expanded:
        - FOR loops: each iteration's child steps are traversed sequentially
        - WHILE loops: condition is evaluated; if True, children are traversed
          (up to max_iterations to prevent infinite loops)
        - FOREACH loops: not expanded (collection resolution requires runtime
          data); children are traversed once as a representative iteration

    Example:
        >>> plan = YamlPlan(name="test", version="1.0", steps=[
        ...     YamlStep(id="s1", script="check.py"),
        ...     YamlStep(id="s2", script="measure.py", preconditions=["s1"]),
        ... ])
        >>> scheduler = DryRunScheduler()
        >>> result = scheduler.dry_run(plan)
        >>> print(result.summary)
        DryRun('test'): 2 pass, 0 fail, 0 skip, 0 blocked, 0 error, 0 not_reached (PASS) in 0.001s
    """

    # Maximum iterations for WHILE loop expansion in dry run
    MAX_WHILE_ITERATIONS: int = 100

    def __init__(
        self,
        event_bus: EventBus | None = None,
        variable_space: VariableSpace | None = None,
    ) -> None:
        """Initialize the dry-run scheduler with isolated state.

        Creates fresh StepRegistry, ResourceManager, and VariableSpace
        instances so the dry run does not interfere with any production
        scheduler state.

        Args:
            event_bus: Optional EventBus. Not actively used for dispatch
                but passed to StepRegistry for consistency. Defaults to None.
            variable_space: Optional pre-populated VariableSpace for
                resolving ${} references in conditions and skip_if.
                Defaults to a fresh empty VariableSpace.
        """
        self._event_bus: EventBus | None = event_bus
        self._variable_space: VariableSpace = variable_space or VariableSpace()

        # Isolated state - does not touch production scheduler
        self._registry: StepRegistry = StepRegistry(
            event_bus=event_bus,
            condition_evaluator=None,
        )
        self._resource_manager: ResourceManager = ResourceManager(
            event_bus=event_bus,
        )

    @property
    def registry(self) -> StepRegistry:
        """Access the dry-run's isolated StepRegistry."""
        return self._registry

    @property
    def resource_manager(self) -> ResourceManager:
        """Access the dry-run's isolated ResourceManager."""
        return self._resource_manager

    @property
    def variable_space(self) -> VariableSpace:
        """Access the VariableSpace used for condition resolution."""
        return self._variable_space

    def dry_run(
        self,
        plan: YamlPlan,
        assume_pass: bool = True,
    ) -> DryRunResult:
        """Execute a dry-run traversal of the plan's scheduling graph.

        Traverses all steps in the plan, evaluating preconditions, skip_if
        expressions, and resource availability. Records a StepDecision for
        each step. Does NOT execute any scripts or call any instruments.

        Args:
            plan: The YamlPlan to dry-run.
            assume_pass: If True (default), steps that pass all checks are
                assumed to PASS (since no real execution occurs). If False,
                steps are marked as "would_execute" with PASS decision but
                the reason indicates no execution was performed.

        Returns:
            DryRunResult with per-step decisions and aggregate statistics.
        """
        start_time = time.monotonic()
        decisions: list[StepDecision] = []
        deadlock_steps: list[str] = []

        # Phase 1: Register all steps and build condition graph
        step_conditions = self._build_step_conditions(plan)
        self._register_steps(plan, step_conditions)

        # Phase 2: Traverse steps in plan order
        completed_statuses: dict[str, StepStatus] = {}
        for item in plan.steps:
            if isinstance(item, YamlStep):
                decision = self._process_step(
                    item, completed_statuses, assume_pass,
                )
                decisions.append(decision)
                completed_statuses[item.id] = self._decision_to_status(decision)
            elif isinstance(item, YamlLoop):
                loop_decisions = self._process_loop(
                    item, completed_statuses, assume_pass,
                )
                decisions.extend(loop_decisions)
                # Record loop completion status
                loop_passed = all(d.decision == "PASS" for d in loop_decisions)
                completed_statuses[item.id] = (
                    StepStatus.PASSED if loop_passed else StepStatus.FAILED
                )

        # Phase 3: Check for deadlocked (blocked) steps
        blocked_steps = [d.step_id for d in decisions if d.decision == "BLOCKED"]
        if blocked_steps:
            deadlock_steps = blocked_steps
            logger.warning(
                "Dry-run detected %d blocked steps (potential deadlock): %s",
                len(blocked_steps),
                blocked_steps,
            )

        # Phase 4: Compute aggregate statistics
        duration = time.monotonic() - start_time
        passed = sum(1 for d in decisions if d.decision == "PASS")
        failed = sum(1 for d in decisions if d.decision == "FAIL")
        skipped = sum(1 for d in decisions if d.decision == "SKIP")
        blocked = sum(1 for d in decisions if d.decision == "BLOCKED")
        errors = sum(1 for d in decisions if d.decision == "ERROR")
        not_reached = sum(1 for d in decisions if d.decision == "NOT_REACHED")

        return DryRunResult(
            plan_name=plan.name,
            plan_version=plan.version,
            decisions=decisions,
            total_steps=len(decisions),
            passed=passed,
            failed=failed,
            skipped=skipped,
            blocked=blocked,
            errors=errors,
            not_reached=not_reached,
            duration_s=duration,
            deadlock_detected=len(blocked_steps) > 0,
            deadlock_steps=deadlock_steps,
        )

    # ------------------------------------------------------------------
    # Step registration and condition building
    # ------------------------------------------------------------------

    def _build_step_conditions(
        self, plan: YamlPlan,
    ) -> dict[str, Condition | None]:
        """Build Condition objects from step preconditions.

        A step's preconditions list contains step IDs that must have PASSED
        before this step can execute. This translates to a Condition with
        step=<first_predecessor> and status="PASSED".

        For multiple preconditions, the first one is used as the primary
        condition (the StepRegistry evaluates all registered conditions).

        Args:
            plan: The plan to extract conditions from.

        Returns:
            Dict mapping step_id to Condition (or None if no preconditions).
        """
        conditions: dict[str, Condition | None] = {}

        def process_items(items: list[YamlStep | YamlLoop]) -> None:
            for item in items:
                if isinstance(item, YamlStep):
                    if item.preconditions:
                        # Use the first precondition as the primary condition
                        # (registry evaluates all via get_ready_steps)
                        conditions[item.id] = Condition(
                            step=item.preconditions[0],
                            status="PASSED",
                        )
                    else:
                        conditions[item.id] = None
                elif isinstance(item, YamlLoop):
                    if item.preconditions if hasattr(item, "preconditions") else None:
                        # YamlLoop doesn't have preconditions in the dataclass,
                        # but skip_if is handled separately
                        pass
                    process_items(item.steps)

        process_items(plan.steps)
        return conditions

    def _register_steps(
        self,
        plan: YamlPlan,
        conditions: dict[str, Condition | None],
    ) -> None:
        """Register all steps from the plan into the StepRegistry.

        Clears any existing registrations first to ensure a clean state.

        Args:
            plan: The plan to register steps from.
            conditions: Pre-built condition dict from _build_step_conditions.
        """
        self._registry.clear()

        def register_items(items: list[YamlStep | YamlLoop]) -> None:
            for item in items:
                if isinstance(item, YamlStep):
                    condition = conditions.get(item.id)
                    if not self._registry.has_step(item.id):
                        self._registry.register(item.id, condition=condition)
                elif isinstance(item, YamlLoop):
                    if not self._registry.has_step(item.id):
                        self._registry.register(item.id)
                    register_items(item.steps)

        register_items(plan.steps)

    # ------------------------------------------------------------------
    # Step processing
    # ------------------------------------------------------------------

    def _process_step(
        self,
        step: YamlStep,
        completed_statuses: dict[str, StepStatus],
        assume_pass: bool,
    ) -> StepDecision:
        """Process a single step in the dry run.

        Evaluates preconditions, skip_if, and resource availability.
        Acquires and releases resources. Records the decision.

        Args:
            step: The YamlStep to process.
            completed_statuses: Dict of step_id -> StepStatus for completed steps.
            assume_pass: Whether to assume steps pass after checks.

        Returns:
            StepDecision recording the outcome.
        """
        # Check skip_if first
        skip_result = self._evaluate_skip_if(step)
        if skip_result is not None and skip_result:
            decision = StepDecision(
                step_id=step.id,
                decision="SKIP",
                reason=f"skip_if condition met: {step.skip_if}",
                condition_met=True,
                resources_acquired=[],
                skip_if_evaluated=step.skip_if,
                skip_if_result=True,
            )
            self._update_registry_status(step.id, StepStatus.SKIPPED)
            return decision

        # Evaluate preconditions
        condition_met = self._evaluate_preconditions(step, completed_statuses)
        if not condition_met:
            failed_pred = self._find_failed_precondition(step, completed_statuses)
            reason = (
                f"Precondition not met: '{failed_pred}' "
                f"(status: {self._status_name(completed_statuses.get(failed_pred))})"
            )
            decision = StepDecision(
                step_id=step.id,
                decision="BLOCKED",
                reason=reason,
                condition_met=False,
                resources_acquired=[],
                skip_if_evaluated=step.skip_if,
                skip_if_result=False,
            )
            self._update_registry_status(step.id, StepStatus.PENDING)
            return decision

        # Acquire resources
        resource_ids = list(step.resources.keys()) if step.resources else []
        acquired: list[str] = []
        acquisition_failed = False

        for res_id in resource_ids:
            if self._resource_manager.acquire(res_id, step.id, timeout=0):
                acquired.append(res_id)
            else:
                acquisition_failed = True
                logger.debug(
                    "Dry-run: resource '%s' unavailable for step '%s'",
                    res_id, step.id,
                )
                break

        if acquisition_failed:
            # Release any partially acquired resources
            for res_id in acquired:
                self._resource_manager.release(res_id, step.id)
            decision = StepDecision(
                step_id=step.id,
                decision="BLOCKED",
                reason=f"Resource unavailable: could not acquire all required resources {resource_ids}",
                condition_met=True,
                resources_acquired=[],
                skip_if_evaluated=step.skip_if,
                skip_if_result=skip_result,
            )
            self._update_registry_status(step.id, StepStatus.PENDING)
            return decision

        # All checks passed - record success
        # Release resources (dry run doesn't hold them)
        for res_id in acquired:
            self._resource_manager.release(res_id, step.id)

        reason = "All preconditions and resources satisfied" if assume_pass else (
            "All checks passed (no execution performed)"
        )
        decision = StepDecision(
            step_id=step.id,
            decision="PASS",
            reason=reason,
            condition_met=True,
            resources_acquired=acquired,
            skip_if_evaluated=step.skip_if,
            skip_if_result=skip_result,
        )
        self._update_registry_status(step.id, StepStatus.PASSED)
        return decision

    def _process_loop(
        self,
        loop: YamlLoop,
        completed_statuses: dict[str, StepStatus],
        assume_pass: bool,
    ) -> list[StepDecision]:
        """Process a YamlLoop by expanding and traversing its children.

        For FOR loops: traverse children for each iteration.
        For WHILE loops: evaluate condition, traverse if True (up to max).
        For FOREACH loops: traverse children once (representative iteration).

        Each child step gets a decision with a suffixed step_id:
        {loop_id}#{iteration}#{child_id}

        Args:
            loop: The YamlLoop to process.
            completed_statuses: Dict of completed step statuses.
            assume_pass: Whether to assume steps pass.

        Returns:
            List of StepDecision records for the loop's children.
        """
        decisions: list[StepDecision] = []

        # Evaluate loop-level skip_if
        if loop.skip_if is not None and loop.skip_if.strip():
            if self._evaluate_skip_expression(loop.skip_if):
                decisions.append(StepDecision(
                    step_id=loop.id,
                    decision="SKIP",
                    reason=f"Loop skip_if condition met: {loop.skip_if}",
                    condition_met=True,
                    resources_acquired=[],
                    skip_if_evaluated=loop.skip_if,
                    skip_if_result=True,
                ))
                self._update_registry_status(loop.id, StepStatus.SKIPPED)
                return decisions

        # Determine number of iterations to simulate
        if loop.loop_type.value == "FOR":
            iterations = loop.count if loop.count is not None else 1
            iterations = min(iterations, self.MAX_WHILE_ITERATIONS)
        elif loop.loop_type.value == "WHILE":
            # Evaluate the while condition - if True, simulate one iteration
            # (can't truly loop without runtime variable changes)
            if loop.condition is not None:
                if self._evaluate_skip_expression(loop.condition):
                    # Condition is a break condition - if True, loop doesn't execute
                    # Or it's a continue condition - ambiguous; simulate 1 iteration
                    iterations = 1
                else:
                    iterations = 1
            else:
                iterations = 1
        elif loop.loop_type.value == "FOREACH":
            # Can't resolve collection without runtime data; simulate 1 iteration
            iterations = 1
        else:
            iterations = 1

        # Traverse children for each iteration
        for i in range(iterations):
            for child in loop.steps:
                if isinstance(child, YamlStep):
                    # Create a synthetic step ID for this iteration
                    iter_step_id = f"{loop.id}#{i}#{child.id}"
                    iter_step = YamlStep(
                        id=iter_step_id,
                        script=child.script,
                        params=child.params,
                        preconditions=child.preconditions,
                        resources=child.resources,
                        timeout=child.timeout,
                        retry=child.retry,
                        on_fail=child.on_fail,
                        export_outputs=child.export_outputs,
                        skip_if=child.skip_if,
                        skip_reason=child.skip_reason,
                    )
                    # Register the iterated step
                    if not self._registry.has_step(iter_step_id):
                        condition = None
                        if child.preconditions:
                            condition = Condition(
                                step=child.preconditions[0],
                                status="PASSED",
                            )
                        self._registry.register(iter_step_id, condition=condition)

                    decision = self._process_step(
                        iter_step, completed_statuses, assume_pass,
                    )
                    decisions.append(decision)
                    completed_statuses[iter_step_id] = self._decision_to_status(decision)
                elif isinstance(child, YamlLoop):
                    # Nested loop - recurse with prefixed ID
                    nested_decisions = self._process_loop(
                        child, completed_statuses, assume_pass,
                    )
                    decisions.extend(nested_decisions)

        return decisions

    # ------------------------------------------------------------------
    # Condition and skip_if evaluation
    # ------------------------------------------------------------------

    def _evaluate_preconditions(
        self,
        step: YamlStep,
        completed_statuses: dict[str, StepStatus],
    ) -> bool:
        """Evaluate whether a step's preconditions are met.

        Checks that all predecessor steps have PASSED.

        Args:
            step: The step to check.
            completed_statuses: Dict of completed step statuses.

        Returns:
            True if all preconditions are met, False otherwise.
        """
        if not step.preconditions:
            return True

        for pred_id in step.preconditions:
            pred_status = completed_statuses.get(pred_id)
            if pred_status is None:
                # Predecessor hasn't been processed yet
                return False
            if pred_status != StepStatus.PASSED:
                return False

        return True

    def _find_failed_precondition(
        self,
        step: YamlStep,
        completed_statuses: dict[str, StepStatus],
    ) -> str:
        """Find the first failed or missing precondition.

        Args:
            step: The step whose preconditions to check.
            completed_statuses: Dict of completed step statuses.

        Returns:
            The step_id of the first failed/missing precondition.
        """
        for pred_id in step.preconditions:
            pred_status = completed_statuses.get(pred_id)
            if pred_status is None:
                return pred_id
            if pred_status != StepStatus.PASSED:
                return pred_id
        return step.preconditions[0] if step.preconditions else ""

    def _evaluate_skip_if(self, step: YamlStep) -> bool | None:
        """Evaluate a step's skip_if expression.

        Args:
            step: The step to evaluate.

        Returns:
            True if the step should be skipped, False if it should not,
            None if no skip_if is present.
        """
        if step.skip_if is None or not step.skip_if.strip():
            return None

        return self._evaluate_skip_expression(step.skip_if)

    def _evaluate_skip_expression(self, expression: str) -> bool:
        """Evaluate a skip_if or condition expression.

        Uses ConditionEvaluator with the current variable space.

        Args:
            expression: The expression string (may contain ${} references).

        Returns:
            True if the expression evaluates to True.
        """
        # Build step_results from registry state
        all_steps = self._registry.get_all_steps()
        step_results: dict[str, dict[str, object]] = {}
        for sid, st in all_steps.items():
            step_results[sid] = {"status": st, "outputs": {}}

        evaluator = ConditionEvaluator(
            step_results,
            resource_manager=self._resource_manager,
            variable_space=self._variable_space,
        )
        return evaluator.evaluate_skip_condition(expression)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _update_registry_status(self, step_id: str, status: StepStatus) -> None:
        """Update a step's status in the registry, handling unregistered steps.

        Args:
            step_id: The step identifier.
            status: The new status.
        """
        try:
            self._registry.update_status(step_id, status)
        except KeyError:
            self._registry.register(step_id)
            self._registry.update_status(step_id, status)

    @staticmethod
    def _decision_to_status(decision: StepDecision) -> StepStatus:
        """Convert a StepDecision to a StepStatus.

        Args:
            decision: The step decision.

        Returns:
            The corresponding StepStatus.
        """
        mapping: dict[StepDecisionType, StepStatus] = {
            "PASS": StepStatus.PASSED,
            "FAIL": StepStatus.FAILED,
            "SKIP": StepStatus.SKIPPED,
            "BLOCKED": StepStatus.PENDING,
            "ERROR": StepStatus.ERROR,
            "NOT_REACHED": StepStatus.PENDING,
        }
        return mapping[decision.decision]

    @staticmethod
    def _status_name(status: StepStatus | None) -> str:
        """Get a human-readable status name.

        Args:
            status: The StepStatus or None.

        Returns:
            The status name string, or "NOT_PROCESSED" if None.
        """
        if status is None:
            return "NOT_PROCESSED"
        return status.value
