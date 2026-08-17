"""Step Registry for ATE Platform.

This module provides step status management:
- StepRegistry: Thread-safe registry for tracking step statuses
- Condition-based readiness checking
- Event publishing on status changes

Execution enhancements (F5, 设计文档 §6.4):
- StepExecutionConfig: per-step retry/repeat/skip configuration
- Retry/repeat counters: track how many retries/repeats a step consumed

Thread Safety:
    All status operations are protected by threading.Lock.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from ..types import Condition, StepStatus
from .condition_evaluator import ConditionEvaluator
from .event_bus import EventBus, EventType

if TYPE_CHECKING:
    from .resource_manager import ResourceManager
    from .variable_space import VariableSpace


@dataclass(frozen=True)
class StepExecutionConfig:
    """Per-step execution policy (retry / repeat / skip).

    Frozen so configs are immutable once registered — counters live in the
    registry, policy lives in the config.

    Attributes:
        max_retries: 最大重试次数（ERROR 后），0 表示不重试。
        retry_delay_ms: 重试间隔（毫秒）。
        repeat_on_measurement_fail: 测量 FAILED 后是否重复执行。
        repeat_limit: 重复执行上限，0 表示无限（配合 force_repeat 语义）。
        force_repeat: 忽略 repeat_limit 强制重复（用于人工介入后重测）。
        skip_if: 跳过表达式（条件成立则该步骤跳过），None 不跳过。
    """

    max_retries: int = 0
    retry_delay_ms: int = 0
    repeat_on_measurement_fail: bool = False
    repeat_limit: int = 0
    force_repeat: bool = False
    skip_if: str | None = None


class StepRegistry:
    """Thread-safe registry for tracking step execution status.

    Manages step statuses and their preconditions:
    - Register steps with conditions
    - Update and query step statuses
    - Determine which steps are ready to execute

    Thread Safety:
        All operations that modify or read state are protected by a lock.

    Example:
        >>> registry = StepRegistry(event_bus, condition_evaluator)
        >>> registry.register("step1", Condition(step="step0", status="PASSED"))
        >>> registry.update_status("step1", StepStatus.RUNNING)
        >>> ready = registry.get_ready_steps()
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        condition_evaluator: ConditionEvaluator | None = None,
    ) -> None:
        """Initialize the step registry.

        Args:
            event_bus: Optional EventBus for publishing status change events
            condition_evaluator: Optional ConditionEvaluator for checking conditions
        """
        self._steps: dict[str, StepStatus] = {}
        self._conditions: dict[str, Condition] = {}
        self._configs: dict[str, StepExecutionConfig] = {}
        self._retry_counts: dict[str, int] = {}
        self._repeat_counts: dict[str, int] = {}
        self._event_bus: EventBus | None = event_bus
        self._condition_evaluator: ConditionEvaluator | None = condition_evaluator
        self._lock: threading.Lock = threading.Lock()

    def register(
        self,
        step_id: str,
        condition: Condition | None = None,
        config: StepExecutionConfig | None = None,
    ) -> None:
        """Register a step with an optional precondition.

        Args:
            step_id: Unique identifier for the step
            condition: Optional precondition that must be met before execution
            config: Optional StepExecutionConfig for retry/repeat/skip policy

        Raises:
            ValueError: If step_id is empty or already registered
        """
        if not step_id or not step_id.strip():
            raise ValueError("step_id cannot be empty")

        with self._lock:
            if step_id in self._steps:
                raise ValueError(f"Step '{step_id}' is already registered")

            self._steps[step_id] = StepStatus.PENDING
            if condition is not None:
                self._conditions[step_id] = condition
            if config is not None:
                self._configs[step_id] = config
            self._retry_counts[step_id] = 0
            self._repeat_counts[step_id] = 0

    def update_status(self, step_id: str, status: StepStatus) -> None:
        """Update the status of a registered step.

        Publishes STEP_STATUS_CHANGED event if event_bus is configured
        and the status actually changed.

        Args:
            step_id: The step identifier
            status: The new status

        Raises:
            KeyError: If step is not registered
        """
        with self._lock:
            if step_id not in self._steps:
                raise KeyError(f"Step '{step_id}' is not registered")

            old_status = self._steps[step_id]
            self._steps[step_id] = status

        # Publish event outside the lock to avoid potential deadlock
        if self._event_bus is not None and old_status != status:
            from shared.events import StepStatusChangedData

            event_data = asdict(StepStatusChangedData(
                step_id=step_id,
                old_status=old_status.value,
                new_status=status.value,
            ))
            self._event_bus.publish_sync(EventType.STEP_STATUS_CHANGED, event_data)

    def get_status(self, step_id: str) -> StepStatus:
        """Get the current status of a step.

        Args:
            step_id: The step identifier

        Returns:
            The current status of the step

        Raises:
            KeyError: If step is not registered
        """
        with self._lock:
            if step_id not in self._steps:
                raise KeyError(f"Step '{step_id}' is not registered")

            return self._steps[step_id]

    def get_ready_steps(
        self,
        variable_space: "VariableSpace | None" = None,
        resource_manager: "ResourceManager | None" = None,
    ) -> list[str]:
        """Get list of steps that are ready to execute.

        A step is ready if:
        - Its status is PENDING
        - It has no condition, OR its condition evaluates to True

        Args:
            variable_space: Optional VariableSpace for resolving variable
                references in expression conditions.
            resource_manager: Optional ResourceManager for checking
                resource availability conditions.

        Returns:
            List of step IDs that are ready to execute
        """
        with self._lock:
            ready_steps: list[str] = []

            for step_id, status in self._steps.items():
                # Only PENDING steps can be ready
                if status != StepStatus.PENDING:
                    continue

                # Check if step has a condition
                condition = self._conditions.get(step_id)

                if condition is None:
                    # No condition means ready
                    ready_steps.append(step_id)
                else:
                    # Evaluate condition - build step_results dict from current statuses
                    step_results: dict[str, dict[str, object]] = {}
                    for sid, st in self._steps.items():
                        step_results[sid] = {"status": st, "outputs": {}}

                    # Create a temporary evaluator with current state
                    evaluator = ConditionEvaluator(
                        step_results,  # type: ignore[arg-type]
                        resource_manager=resource_manager,
                        variable_space=variable_space,
                    )

                    if evaluator.evaluate(condition):
                        ready_steps.append(step_id)

            return ready_steps

    def unregister(self, step_id: str) -> bool:
        """Unregister a step.

        Args:
            step_id: The step identifier

        Returns:
            True if step was unregistered, False if not found
        """
        with self._lock:
            if step_id not in self._steps:
                return False

            del self._steps[step_id]
            _ = self._conditions.pop(step_id, None)
            _ = self._configs.pop(step_id, None)
            _ = self._retry_counts.pop(step_id, None)
            _ = self._repeat_counts.pop(step_id, None)
            return True

    def has_step(self, step_id: str) -> bool:
        """Check if a step is registered.

        Args:
            step_id: The step identifier

        Returns:
            True if step is registered, False otherwise
        """
        with self._lock:
            return step_id in self._steps

    def get_all_steps(self) -> dict[str, StepStatus]:
        """Get all registered steps and their statuses.

        Returns:
            Copy of the steps dictionary
        """
        with self._lock:
            return dict(self._steps)

    def get_condition(self, step_id: str) -> Condition | None:
        """Get the condition for a step.

        Args:
            step_id: The step identifier

        Returns:
            The condition if set, None otherwise

        Raises:
            KeyError: If step is not registered
        """
        with self._lock:
            if step_id not in self._steps:
                raise KeyError(f"Step '{step_id}' is not registered")

            return self._conditions.get(step_id)

    def get_config(self, step_id: str) -> StepExecutionConfig:
        """Get the execution config for a step.

        Steps registered without a config receive the default
        (all-features-disabled) config, preserving backward compatibility.

        Args:
            step_id: The step identifier

        Returns:
            The StepExecutionConfig for the step

        Raises:
            KeyError: If step is not registered
        """
        with self._lock:
            if step_id not in self._steps:
                raise KeyError(f"Step '{step_id}' is not registered")
            return self._configs.get(step_id, StepExecutionConfig())

    def get_retry_count(self, step_id: str) -> int:
        """Get the current retry count for a step.

        Args:
            step_id: The step identifier

        Returns:
            Number of retries consumed so far

        Raises:
            KeyError: If step is not registered
        """
        with self._lock:
            if step_id not in self._steps:
                raise KeyError(f"Step '{step_id}' is not registered")
            return self._retry_counts.get(step_id, 0)

    def increment_retry_count(self, step_id: str) -> int:
        """Increment the retry count for a step.

        Args:
            step_id: The step identifier

        Returns:
            The new retry count
        """
        with self._lock:
            if step_id not in self._steps:
                raise KeyError(f"Step '{step_id}' is not registered")
            self._retry_counts[step_id] = self._retry_counts.get(step_id, 0) + 1
            return self._retry_counts[step_id]

    def reset_retry_count(self, step_id: str) -> None:
        """Reset the retry count for a step to zero.

        Args:
            step_id: The step identifier
        """
        with self._lock:
            if step_id not in self._steps:
                raise KeyError(f"Step '{step_id}' is not registered")
            self._retry_counts[step_id] = 0

    def get_repeat_count(self, step_id: str) -> int:
        """Get the current repeat count for a step.

        Args:
            step_id: The step identifier

        Returns:
            Number of repeats consumed so far

        Raises:
            KeyError: If step is not registered
        """
        with self._lock:
            if step_id not in self._steps:
                raise KeyError(f"Step '{step_id}' is not registered")
            return self._repeat_counts.get(step_id, 0)

    def increment_repeat_count(self, step_id: str) -> int:
        """Increment the repeat count for a step.

        Args:
            step_id: The step identifier

        Returns:
            The new repeat count
        """
        with self._lock:
            if step_id not in self._steps:
                raise KeyError(f"Step '{step_id}' is not registered")
            self._repeat_counts[step_id] = self._repeat_counts.get(step_id, 0) + 1
            return self._repeat_counts[step_id]

    def reset_repeat_count(self, step_id: str) -> None:
        """Reset the repeat count for a step to zero.

        Args:
            step_id: The step identifier
        """
        with self._lock:
            if step_id not in self._steps:
                raise KeyError(f"Step '{step_id}' is not registered")
            self._repeat_counts[step_id] = 0

    def clear(self) -> None:
        """Clear all registered steps, configs and counters."""
        with self._lock:
            self._steps.clear()
            self._conditions.clear()
            self._configs.clear()
            self._retry_counts.clear()
            self._repeat_counts.clear()
