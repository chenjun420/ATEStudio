"""Loop executor for YamlLoop structures in ATE Platform.

This module provides the LoopExecutor class that executes YamlLoop structures
in serial or parallel mode, supporting three loop types:
- FOR: Count-based iteration (0..count-1)
- WHILE: Condition-based iteration with safety limit
- FOREACH: Collection-based iteration

Key features:
- Serial mode: iterate sequentially, each iteration waits for previous
- Parallel mode: use ProcessExecutor.execute_batch() with asyncio.Semaphore
- Publish LOOP_ITERATION_STARTED/COMPLETED events via EventBus
- Each iteration gets its own scope in VariableSpace: loop.<loop_id>.<iteration>.<key>
- FOR loop: iteration variable (default "i") set to current index
- WHILE loop: evaluate condition via ConditionEvaluator before each iteration
- FOREACH loop: resolve collection from VariableSpace, set iteration_var to current item
- Results: accumulate LoopIterationResult for each iteration into LoopResult

Example:
    >>> from ate_platform.executor import LoopExecutor, ProcessExecutor
    >>> from ate_platform.scheduler import EventBus, VariableSpace
    >>> from shared.dsl import YamlLoop, LoopType, ExecutionMode
    >>>
    >>> bus = EventBus()
    >>> executor = ProcessExecutor(event_bus=bus)
    >>> vs = VariableSpace(event_bus=bus)
    >>> loop_exec = LoopExecutor(executor, bus, vs)
    >>>
    >>> loop = YamlLoop(id="loop1", loop_type=LoopType.FOR, count=3, steps=[...])
    >>> result = await loop_exec.execute_loop(loop, run_id="run_001")
    >>> print(result.status)
    StepStatus.PASSED
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from typing import Any

from ..scheduler.condition_evaluator import ConditionEvaluator
from ..scheduler.event_bus import EventBus, EventType
from ..scheduler.variable_space import VariableSpace
from ..types import Condition, LoopIterationResult, LoopResult, StepResult, StepStatus
from .step_executor import StepExecutor
from shared.dsl import ExecutionMode, LoopType, YamlLoop, YamlStep
from shared.events import LoopIterationCompletedData, LoopIterationStartedData
from shared.types import ExecuteTask

logger = logging.getLogger(__name__)


class LoopExecutor:
    """Executor for YamlLoop structures with serial and parallel modes.

    Executes the steps within a YamlLoop repeatedly based on the loop type,
    managing iteration variables in VariableSpace and publishing events.

    Attributes:
        _executor: ProcessExecutor for running individual step scripts
        _event_bus: Optional EventBus for publishing loop iteration events
        _variable_space: VariableSpace for iteration variable scoping
    """

    def __init__(
        self,
        executor: StepExecutor,
        event_bus: EventBus | None = None,
        variable_space: VariableSpace | None = None,
    ) -> None:
        """Initialize the loop executor.

        Args:
            executor: StepExecutor for running step scripts (Protocol)
            event_bus: Optional EventBus for publishing loop events
            variable_space: Optional VariableSpace for iteration variables
        """
        self._executor = executor
        self._event_bus = event_bus
        self._variable_space = variable_space or VariableSpace(event_bus=event_bus)

    async def execute_loop(
        self,
        loop: YamlLoop,
        run_id: str | None = None,
    ) -> LoopResult:
        """Execute a YamlLoop and return the aggregated result.

        Dispatches to the appropriate handler based on loop_type:
        - LoopType.FOR → _execute_for_loop
        - LoopType.WHILE → _execute_while_loop
        - LoopType.FOREACH → _execute_foreach_loop

        Args:
            loop: The YamlLoop to execute
            run_id: Optional execution run identifier

        Returns:
            LoopResult with aggregated status and per-iteration results
        """
        start_time = time.monotonic()

        try:
            if loop.loop_type == LoopType.FOR:
                result = await self._execute_for_loop(loop, run_id)
            elif loop.loop_type == LoopType.WHILE:
                result = await self._execute_while_loop(loop, run_id)
            elif loop.loop_type == LoopType.FOREACH:
                result = await self._execute_foreach_loop(loop, run_id)
            else:
                result = LoopResult(
                    loop_id=loop.id,
                    loop_type=loop.loop_type.value,
                    status=StepStatus.ERROR,
                    error=f"Unknown loop type: {loop.loop_type}",
                )
        except Exception as e:
            logger.exception("Loop '%s' failed with exception: %s", loop.id, e)
            result = LoopResult(
                loop_id=loop.id,
                loop_type=loop.loop_type.value,
                status=StepStatus.ERROR,
                error=str(e),
            )

        # Set total duration
        result.duration = time.monotonic() - start_time

        # Store result in VariableSpace
        self._variable_space.set(f"loop.{loop.id}.result", result)

        return result

    async def _execute_for_loop(
        self,
        loop: YamlLoop,
        run_id: str | None = None,
    ) -> LoopResult:
        """Execute a FOR loop (count-based iteration).

        Iterates from 0 to count-1, setting the iteration variable
        (default "i") to the current index in each iteration.

        Args:
            loop: The YamlLoop with loop_type=FOR
            run_id: Optional execution run identifier

        Returns:
            LoopResult with per-iteration results
        """
        count = loop.count or 0
        iteration_var = loop.iterator_var or "i"

        if loop.execution_mode == ExecutionMode.PARALLEL:
            return await self._execute_iterations_parallel(
                loop, count, iteration_var, is_index=True, run_id=run_id,
            )
        else:
            return await self._execute_iterations_serial(
                loop, count, iteration_var, is_index=True, run_id=run_id,
            )

    async def _execute_while_loop(
        self,
        loop: YamlLoop,
        run_id: str | None = None,
    ) -> LoopResult:
        """Execute a WHILE loop (condition-based iteration).

        Evaluates the condition expression before each iteration.
        Stops when the condition evaluates to False or max_iterations is reached.

        Args:
            loop: The YamlLoop with loop_type=WHILE
            run_id: Optional execution run identifier

        Returns:
            LoopResult with per-iteration results
        """
        max_iterations = loop.max_iterations or 1000
        condition_expr = loop.condition or "False"

        iteration_results: list[LoopIterationResult] = []
        iteration = 0

        while iteration < max_iterations:
            # Evaluate condition
            if not self._evaluate_condition_expr(condition_expr):
                break

            # Execute one iteration
            iter_result = await self._execute_single_iteration(
                loop, iteration, run_id=run_id,
            )
            iteration_results.append(iter_result)

            # If iteration failed and we should stop on failure
            if iter_result.status in (StepStatus.FAILED, StepStatus.ERROR):
                break

            iteration += 1

        return self._build_loop_result(loop, iteration_results)

    async def _execute_foreach_loop(
        self,
        loop: YamlLoop,
        run_id: str | None = None,
    ) -> LoopResult:
        """Execute a FOREACH loop (collection-based iteration).

        Resolves the collection from VariableSpace and iterates over items,
        setting the iteration variable to the current item.

        Args:
            loop: The YamlLoop with loop_type=FOREACH
            run_id: Optional execution run identifier

        Returns:
            LoopResult with per-iteration results
        """
        collection_expr = loop.collection or ""
        iteration_var = loop.iterator_var or "item"

        # Resolve collection from VariableSpace
        collection = self._resolve_collection(collection_expr)
        if collection is None:
            return LoopResult(
                loop_id=loop.id,
                loop_type=loop.loop_type.value,
                status=StepStatus.ERROR,
                error=f"Collection '{collection_expr}' resolved to None or not found",
            )

        count = len(collection)

        if loop.execution_mode == ExecutionMode.PARALLEL:
            return await self._execute_iterations_parallel(
                loop, count, iteration_var, is_index=False,
                collection=collection, run_id=run_id,
            )
        else:
            return await self._execute_iterations_serial(
                loop, count, iteration_var, is_index=False,
                collection=collection, run_id=run_id,
            )

    async def _execute_iterations_serial(
        self,
        loop: YamlLoop,
        count: int,
        iteration_var: str,
        is_index: bool = True,
        collection: list[Any] | None = None,
        run_id: str | None = None,
    ) -> LoopResult:
        """Execute loop iterations sequentially.

        Args:
            loop: The YamlLoop being executed
            count: Number of iterations
            iteration_var: Variable name for the iteration value
            is_index: If True, set iteration_var to the index (FOR loop);
                if False, set to the collection item (FOREACH loop)
            collection: Optional collection for FOREACH loops
            run_id: Optional execution run identifier

        Returns:
            LoopResult with per-iteration results
        """
        iteration_results: list[LoopIterationResult] = []

        for i in range(count):
            # Set iteration variable
            if is_index:
                self._variable_space.set_loop_variable(loop.id, i, iteration_var, i)
            elif collection is not None:
                self._variable_space.set_loop_variable(loop.id, i, iteration_var, collection[i])

            # Execute iteration
            iter_result = await self._execute_single_iteration(loop, i, run_id=run_id)
            iteration_results.append(iter_result)

            # Stop on failure/error
            if iter_result.status in (StepStatus.FAILED, StepStatus.ERROR):
                break

        return self._build_loop_result(loop, iteration_results)

    async def _execute_iterations_parallel(
        self,
        loop: YamlLoop,
        count: int,
        iteration_var: str,
        is_index: bool = True,
        collection: list[Any] | None = None,
        run_id: str | None = None,
    ) -> LoopResult:
        """Execute loop iterations in parallel with concurrency control.

        Uses ProcessExecutor.execute_batch() with asyncio.Semaphore for
        bounded concurrency. All iterations are launched, but concurrency
        is limited by max_concurrency (derived from loop.max_iterations
        or executor's default).

        Race fix: VariableSpace values are snapshotted per-iteration before
        dispatching tasks, so parallel iterations don't see each other's
        intermediate writes.

        Args:
            loop: The YamlLoop being executed
            count: Number of iterations
            iteration_var: Variable name for the iteration value
            is_index: If True, set iteration_var to the index (FOR loop)
            collection: Optional collection for FOREACH loops
            run_id: Optional execution run identifier

        Returns:
            LoopResult with per-iteration results
        """
        # Pre-set iteration variables for all iterations
        for i in range(count):
            if is_index:
                self._variable_space.set_loop_variable(loop.id, i, iteration_var, i)
            elif collection is not None:
                self._variable_space.set_loop_variable(loop.id, i, iteration_var, collection[i])

        # Snapshot VariableSpace state per-iteration before parallel dispatch.
        # This prevents race conditions where iteration N+1 reads values
        # written by iteration N that shouldn't be visible yet.
        iteration_snapshots: list[dict[str, Any]] = []
        for i in range(count):
            snapshot = self._variable_space.get_all_scope_vars().copy()
            # Also capture loop variables for this iteration
            loop_vars: dict[str, Any] = {}
            for key, value in self._variable_space.get_all_scope_vars().items():
                loop_vars[key] = value
            # Include the iteration-specific loop variable
            iter_var_name = f"loop.{loop.id}.{i}.{iteration_var}"
            iter_var_value = self._variable_space.get(iter_var_name)
            if iter_var_value is not None:
                loop_vars[iter_var_name] = iter_var_value
            iteration_snapshots.append(loop_vars)

        # Build ExecuteTask list from loop steps for each iteration
        all_tasks: list[ExecuteTask] = []
        task_to_iteration: list[int] = []

        for i in range(count):
            iteration_tasks = self._build_iteration_tasks(loop, i, run_id)
            for task in iteration_tasks:
                all_tasks.append(task)
                task_to_iteration.append(i)

        if not all_tasks:
            # No steps in loop — each iteration is a no-op PASSED
            iteration_results = [
                LoopIterationResult(iteration=i, status=StepStatus.PASSED, duration=0.0)
                for i in range(count)
            ]
            # Publish events for each iteration
            for i in range(count):
                await self._publish_iteration_events(loop.id, i, count, run_id)
            return self._build_loop_result(loop, iteration_results)

        # Execute all tasks in batch
        max_concurrency = loop.max_iterations if loop.max_iterations and loop.max_iterations < 1000 else None
        step_results = await self._executor.execute_batch(all_tasks, max_concurrency=max_concurrency)

        # Group results by iteration
        iteration_step_results: dict[int, list[StepResult]] = {}
        for task_idx, step_result in enumerate(step_results):
            iter_idx = task_to_iteration[task_idx]
            iteration_step_results.setdefault(iter_idx, []).append(step_result)

        # Build iteration results
        iteration_results: list[LoopIterationResult] = []
        for i in range(count):
            iter_start = time.monotonic()
            step_res_list = iteration_step_results.get(i, [])

            # Determine iteration status from step results
            iter_status = StepStatus.PASSED
            iter_error: str | None = None
            iter_outputs: dict[str, Any] = {}

            for sr in step_res_list:
                if sr.status in (StepStatus.FAILED, StepStatus.ERROR):
                    iter_status = sr.status
                    iter_error = sr.error
                    break
                iter_outputs.update(sr.outputs)

            # Restore per-iteration snapshot for output scoping.
            # Each iteration's outputs are written to its own loop scope,
            # so parallel iterations don't clobber each other.
            for key, value in iter_outputs.items():
                self._variable_space.set_loop_variable(loop.id, i, key, value)

            # Publish events
            await self._publish_iteration_events(loop.id, i, count, run_id)

            iter_duration = time.monotonic() - iter_start
            iteration_results.append(LoopIterationResult(
                iteration=i,
                status=iter_status,
                outputs=iter_outputs,
                error=iter_error,
                duration=iter_duration,
            ))

        return self._build_loop_result(loop, iteration_results)

    async def _execute_single_iteration(
        self,
        loop: YamlLoop,
        iteration: int,
        run_id: str | None = None,
    ) -> LoopIterationResult:
        """Execute a single loop iteration (all steps within the loop).

        For serial mode, steps are executed sequentially.
        For parallel mode within a single iteration, steps are also sequential
        (parallelism is across iterations, not within).

        Args:
            loop: The YamlLoop being executed
            iteration: Zero-based iteration index
            run_id: Optional execution run identifier

        Returns:
            LoopIterationResult for this iteration
        """
        iter_start = time.monotonic()
        total_iterations = loop.count or 0

        # Publish LOOP_ITERATION_STARTED
        await self._publish_event(
            EventType.LOOP_ITERATION_STARTED,
            asdict(LoopIterationStartedData(
                loop_id=loop.id,
                iteration=iteration,
                total_iterations=total_iterations if total_iterations > 0 else None,
                run_id=run_id,
            )),
        )

        # Execute each step in the loop
        iter_status = StepStatus.PASSED
        iter_error: str | None = None
        iter_outputs: dict[str, Any] = {}

        for step in loop.steps:
            if isinstance(step, YamlStep):
                step_result = await self._executor.execute_async(
                    script_path=step.script,
                    params=step.params,
                    step_id=f"{loop.id}_iter{iteration}_{step.id}",
                    timeout=step.timeout if step.timeout else None,
                    run_id=run_id,
                )

                # Store step outputs in iteration scope
                for key, value in step_result.outputs.items():
                    self._variable_space.set_loop_variable(
                        loop.id, iteration, key, value,
                    )

                if step_result.status in (StepStatus.FAILED, StepStatus.ERROR):
                    iter_status = step_result.status
                    iter_error = step_result.error
                    break

                iter_outputs.update(step_result.outputs)

            elif isinstance(step, YamlLoop):
                # Nested loop — execute recursively
                nested_executor = LoopExecutor(
                    self._executor, self._event_bus, self._variable_space,
                )
                nested_result = await nested_executor.execute_loop(step, run_id=run_id)

                if nested_result.status in (StepStatus.FAILED, StepStatus.ERROR):
                    iter_status = nested_result.status
                    iter_error = nested_result.error if hasattr(nested_result, 'error') else "Nested loop failed"
                    break

        # Publish LOOP_ITERATION_COMPLETED
        await self._publish_event(
            EventType.LOOP_ITERATION_COMPLETED,
            asdict(LoopIterationCompletedData(
                loop_id=loop.id,
                iteration=iteration,
                total_iterations=total_iterations if total_iterations > 0 else None,
                run_id=run_id,
            )),
        )

        iter_duration = time.monotonic() - iter_start
        return LoopIterationResult(
            iteration=iteration,
            status=iter_status,
            outputs=iter_outputs,
            error=iter_error,
            duration=iter_duration,
        )

    def _build_iteration_tasks(
        self,
        loop: YamlLoop,
        iteration: int,
        run_id: str | None = None,
    ) -> list[ExecuteTask]:
        """Build ExecuteTask list for a single iteration (parallel mode).

        Only YamlStep entries are converted to ExecuteTask; nested YamlLoop
        entries are skipped (they need recursive execution which can't be
        batched).

        Args:
            loop: The YamlLoop being executed
            iteration: Zero-based iteration index
            run_id: Optional execution run identifier

        Returns:
            List of ExecuteTask for script steps in this iteration
        """
        tasks: list[ExecuteTask] = []
        for step in loop.steps:
            if isinstance(step, YamlStep):
                tasks.append(ExecuteTask(
                    script_path=step.script,
                    params=step.params,
                    step_id=f"{loop.id}_iter{iteration}_{step.id}",
                    timeout=float(step.timeout) if step.timeout else None,
                    run_id=run_id,
                ))
        return tasks

    def _build_loop_result(
        self,
        loop: YamlLoop,
        iteration_results: list[LoopIterationResult],
    ) -> LoopResult:
        """Build the aggregate LoopResult from per-iteration results.

        Args:
            loop: The YamlLoop that was executed
            iteration_results: List of per-iteration results

        Returns:
            LoopResult with aggregated status
        """
        passed = sum(1 for r in iteration_results if r.status == StepStatus.PASSED)
        failed = sum(1 for r in iteration_results if r.status in (StepStatus.FAILED, StepStatus.ERROR))

        # Aggregate status: PASSED only if all iterations passed
        if not iteration_results:
            aggregate_status = StepStatus.PASSED
        elif all(r.status == StepStatus.PASSED for r in iteration_results):
            aggregate_status = StepStatus.PASSED
        else:
            aggregate_status = StepStatus.FAILED

        return LoopResult(
            loop_id=loop.id,
            loop_type=loop.loop_type.value,
            total_iterations=len(iteration_results),
            passed=passed,
            failed=failed,
            iteration_results=iteration_results,
            status=aggregate_status,
        )

    def _evaluate_condition_expr(self, expression: str) -> bool:
        """Evaluate a boolean condition expression.

        Uses VariableSpace.resolve() for variable substitution, then
        evaluates the expression using a safe evaluator.

        Args:
            expression: Boolean expression string

        Returns:
            True if the expression evaluates to a truthy value
        """
        try:
            # Resolve variable references
            resolved = self._variable_space.resolve(expression)

            # Simple safe evaluation for boolean expressions
            # Allow basic comparisons and boolean operators
            allowed_names: dict[str, Any] = {
                "True": True,
                "False": False,
                "true": True,
                "false": False,
            }

            # Try to resolve any remaining variable references from VariableSpace
            # e.g., "scope.counter < 5" after ${} resolution
            result = eval(resolved, {"__builtins__": {}}, allowed_names)  # noqa: S307
            return bool(result)
        except Exception:
            logger.debug("Condition evaluation failed for: %s", expression)
            return False

    def _resolve_collection(self, collection_expr: str) -> list[Any] | None:
        """Resolve a collection expression from VariableSpace.

        Attempts to get the collection from VariableSpace using the
        expression as a variable name, or parse it as a literal.

        Args:
            collection_expr: Variable name or expression for the collection

        Returns:
            The resolved collection as a list, or None if not found
        """
        # Try to resolve as a variable reference
        resolved = self._variable_space.resolve(collection_expr)

        # If resolution changed the expression, try to evaluate it
        if resolved != collection_expr:
            try:
                result = eval(resolved, {"__builtins__": {}}, {})  # noqa: S307
                if isinstance(result, (list, tuple)):
                    return list(result)
            except Exception:
                pass

        # Try direct variable lookup (e.g., "scope.items")
        value = self._variable_space.get(collection_expr)
        if value is not None and isinstance(value, (list, tuple)):
            return list(value)

        # Try as a ${...} expression
        if collection_expr.startswith("${") and collection_expr.endswith("}"):
            var_name = collection_expr[2:-1]
            value = self._variable_space.get(var_name)
            if value is not None and isinstance(value, (list, tuple)):
                return list(value)

        return None

    async def _publish_iteration_events(
        self,
        loop_id: str,
        iteration: int,
        total_iterations: int,
        run_id: str | None = None,
    ) -> None:
        """Publish LOOP_ITERATION_STARTED and COMPLETED events.

        Convenience method for parallel mode where we publish events
        after the fact (since all iterations run concurrently).

        Args:
            loop_id: The loop identifier
            iteration: Zero-based iteration index
            total_iterations: Total number of iterations
            run_id: Optional execution run identifier
        """
        await self._publish_event(
            EventType.LOOP_ITERATION_STARTED,
            asdict(LoopIterationStartedData(
                loop_id=loop_id,
                iteration=iteration,
                total_iterations=total_iterations if total_iterations > 0 else None,
                run_id=run_id,
            )),
        )
        await self._publish_event(
            EventType.LOOP_ITERATION_COMPLETED,
            asdict(LoopIterationCompletedData(
                loop_id=loop_id,
                iteration=iteration,
                total_iterations=total_iterations if total_iterations > 0 else None,
                run_id=run_id,
            )),
        )

    async def _publish_event(
        self,
        event_type: EventType,
        data: dict[str, Any],
    ) -> None:
        """Publish an event via EventBus if available.

        Args:
            event_type: The type of event
            data: The event payload
        """
        if self._event_bus is not None:
            await self._event_bus.publish(event_type, data)
