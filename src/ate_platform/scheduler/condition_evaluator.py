"""Condition Evaluator for ATE Platform.

This module provides condition evaluation for test step execution,
supporting:
- Step status checks
- Expression evaluation via simpleeval
- Resource availability checks
- Logical combinations (all/any)
- Async waiting with timeout

Built-in functions for expressions:
- time_since(step_id): Time in seconds since step completion
- step_outputs(step_id, key): Get output value from a step
"""

import asyncio
import time
from typing import Any

from simpleeval import SimpleEval

from ..exceptions import ConditionTimeoutError
from ..types import Condition, StepStatus
from .resource_manager import ResourceManager
from .variable_space import VariableSpace


class ConditionEvaluator:
    """Evaluates conditions for test step execution.

    Supports multiple condition types:
    - Step status checks: condition.step and condition.status
    - Expression evaluation: condition.expression via simpleeval
    - Resource availability: condition.resource_available
    - Logical combinations: all/any fields for multiple conditions

    Thread Safety:
        ConditionEvaluator itself is stateless and thread-safe.
        Dependencies (ResourceManager, VariableSpace) have their own thread safety.

    Example:
        >>> evaluator = ConditionEvaluator(step_results, resource_manager, variable_space)
        >>> condition = Condition(step="step1", status="PASSED")
        >>> evaluator.evaluate(condition)
        True
    """

    def __init__(
        self,
        step_results: dict[str, Any],
        resource_manager: ResourceManager | None = None,
        variable_space: VariableSpace | None = None,
        step_completion_times: dict[str, float] | None = None,
    ) -> None:
        """Initialize the condition evaluator.

        Args:
            step_results: Mapping of step_id to StepResult (or dict with status/outputs)
            resource_manager: Optional ResourceManager for resource availability checks
            variable_space: Optional VariableSpace for variable resolution in expressions
            step_completion_times: Optional mapping of step_id to completion timestamp
        """
        self._step_results = step_results
        self._resource_manager = resource_manager
        self._variable_space = variable_space
        self._step_completion_times = step_completion_times or {}
        self._evaluator = self._create_evaluator()

    def _create_evaluator(self) -> SimpleEval:
        """Create a simpleeval evaluator with built-in functions."""
        evaluator = SimpleEval()

        # Add built-in functions
        evaluator.functions["time_since"] = self._time_since
        evaluator.functions["step_outputs"] = self._step_outputs

        return evaluator

    def _time_since(self, step_id: str) -> float | None:
        """Get time in seconds since a step completed.

        Args:
            step_id: The step identifier

        Returns:
            Time in seconds since completion, or None if step hasn't completed
        """
        completion_time = self._step_completion_times.get(step_id)
        if completion_time is None:
            return None
        return time.time() - completion_time

    def _step_outputs(self, step_id: str, key: str) -> Any:
        """Get an output value from a completed step.

        Args:
            step_id: The step identifier
            key: The output key to retrieve

        Returns:
            The output value, or None if not found
        """
        result = self._step_results.get(step_id)
        if result is None:
            return None

        # Handle both StepResult objects and dict
        if hasattr(result, "outputs"):
            outputs = result.outputs
        else:
            outputs = result.get("outputs", {})

        return outputs.get(key)

    def _get_step_status(self, step_id: str) -> StepStatus | None:
        """Get the status of a step.

        Args:
            step_id: The step identifier

        Returns:
            The StepStatus, or None if step not found
        """
        result = self._step_results.get(step_id)
        if result is None:
            return None

        # Handle both StepResult objects and dict
        if hasattr(result, "status"):
            status = result.status
        else:
            status_val = result.get("status")
            if status_val is None:
                return None
            # Handle both StepStatus enum and string
            if isinstance(status_val, StepStatus):
                status = status_val
            else:
                # Convert string to StepStatus
                try:
                    status = StepStatus[status_val]
                except (KeyError, TypeError):
                    return None

        return status

    def evaluate(self, condition: Condition) -> bool:
        """Evaluate a condition and return the result.

        Evaluates all specified condition fields and combines them:
        - step + status: Check if step has expected status
        - expression: Evaluate boolean expression
        - resource_available: Check if all resources are available

        All specified conditions must be True for the overall result to be True.
        If no conditions are specified, returns True.

        Args:
            condition: The condition to evaluate

        Returns:
            True if all specified conditions are met, False otherwise

        Example:
            >>> evaluator.evaluate(Condition(step="step1", status="PASSED"))
            True
            >>> evaluator.evaluate(Condition(expression="voltage > 3.0"))
            True
        """
        results: list[bool] = []

        # Evaluate step status check
        if condition.step is not None and condition.status is not None:
            step_status = self._get_step_status(condition.step)
            if step_status is None:
                # Step not found - condition fails
                results.append(False)
            else:
                # Compare status (condition.status is a string like "PASSED")
                expected_status = StepStatus[condition.status]
                results.append(step_status == expected_status)

        # Evaluate expression
        if condition.expression is not None:
            try:
                expr = condition.expression

                # Resolve variables if variable_space is available
                if self._variable_space is not None:
                    expr = self._variable_space.resolve(expr)

                result = self._evaluator.eval(expr)
                results.append(bool(result))
            except Exception:
                # Expression evaluation failed - condition fails
                results.append(False)

        # Evaluate resource availability
        if condition.resource_available is not None:
            if self._resource_manager is None:
                # No resource manager - cannot check, assume unavailable
                results.append(False)
            else:
                all_available = all(
                    self._resource_manager.is_available(res)
                    for res in condition.resource_available
                )
                results.append(all_available)

        # If no conditions were specified, return True
        if not results:
            return True

        # All conditions must be True
        return all(results)

    def evaluate_all(self, conditions: list[Condition]) -> bool:
        """Evaluate a list of conditions (all must pass).

        Args:
            conditions: List of conditions to evaluate

        Returns:
            True if all conditions pass, False otherwise
        """
        return all(self.evaluate(cond) for cond in conditions)

    def evaluate_any(self, conditions: list[Condition]) -> bool:
        """Evaluate a list of conditions (any can pass).

        Args:
            conditions: List of conditions to evaluate

        Returns:
            True if any condition passes, False otherwise
        """
        return any(self.evaluate(cond) for cond in conditions)

    def evaluate_skip_condition(self, expression: str) -> bool:
        """Evaluate a skip_if expression against the variable space.

        Resolves ${} references via VariableSpace, then evaluates the
        resulting expression with simpleeval. Returns True if the step
        should be skipped, False if it should execute normally.

        The expression is expected to be a boolean expression like
        '${env.SKIP_TESTS} == "true"' or '${scope.debug_mode}'.

        Args:
            expression: The skip_if expression string from YAML

        Returns:
            True if the step should be skipped, False otherwise
        """
        if not expression or not expression.strip():
            return False

        try:
            expr = expression

            # Resolve ${} variables via VariableSpace
            if self._variable_space is not None:
                expr = self._variable_space.resolve(expr)

            # Evaluate the resolved expression
            result = self._evaluator.eval(expr)
            return bool(result)
        except Exception:
            # Expression evaluation failed — don't skip (fail-safe)
            return False

    async def wait_for_condition(
        self,
        condition: Condition,
        timeout: float = 30.0,
        poll_interval: float = 0.1,
    ) -> bool:
        """Wait for a condition to become True with timeout.

        Polls the condition at regular intervals until it becomes True
        or the timeout expires.

        Args:
            condition: The condition to wait for
            timeout: Maximum time to wait in seconds
            poll_interval: Time between polls in seconds

        Returns:
            True if condition became True within timeout

        Raises:
            ConditionTimeoutError: If timeout expires before condition is met

        Example:
            >>> await evaluator.wait_for_condition(
            ...     Condition(step="step1", status="PASSED"),
            ...     timeout=10.0
            ... )
            True
        """
        start_time = time.time()

        while True:
            if self.evaluate(condition):
                return True

            elapsed = time.time() - start_time
            if elapsed >= timeout:
                raise ConditionTimeoutError(
                    f"Condition not met within {timeout} seconds"
                )

            await asyncio.sleep(poll_interval)

    async def wait_for_all(
        self,
        conditions: list[Condition],
        timeout: float = 30.0,
        poll_interval: float = 0.1,
    ) -> bool:
        """Wait for all conditions to become True with timeout.

        Args:
            conditions: List of conditions to wait for
            timeout: Maximum time to wait in seconds
            poll_interval: Time between polls in seconds

        Returns:
            True if all conditions became True within timeout

        Raises:
            ConditionTimeoutError: If timeout expires
        """
        start_time = time.time()

        while True:
            if self.evaluate_all(conditions):
                return True

            elapsed = time.time() - start_time
            if elapsed >= timeout:
                raise ConditionTimeoutError(
                    f"Conditions not all met within {timeout} seconds"
                )

            await asyncio.sleep(poll_interval)

    async def wait_for_any(
        self,
        conditions: list[Condition],
        timeout: float = 30.0,
        poll_interval: float = 0.1,
    ) -> bool:
        """Wait for any condition to become True with timeout.

        Args:
            conditions: List of conditions to wait for
            timeout: Maximum time to wait in seconds
            poll_interval: Time between polls in seconds

        Returns:
            True if any condition became True within timeout

        Raises:
            ConditionTimeoutError: If timeout expires
        """
        start_time = time.time()

        while True:
            if self.evaluate_any(conditions):
                return True

            elapsed = time.time() - start_time
            if elapsed >= timeout:
                raise ConditionTimeoutError(
                    f"No conditions met within {timeout} seconds"
                )

            await asyncio.sleep(poll_interval)

    def update_step_completion_time(self, step_id: str, completion_time: float | None = None) -> None:
        """Update the completion time for a step.

        Args:
            step_id: The step identifier
            completion_time: The completion timestamp (defaults to current time)
        """
        self._step_completion_times[step_id] = completion_time or time.time()

    def set_names(self, names: dict[str, Any]) -> None:
        """Set additional names/variables for expression evaluation.

        Args:
            names: Dictionary of variable names and values
        """
        self._evaluator.names.update(names)
