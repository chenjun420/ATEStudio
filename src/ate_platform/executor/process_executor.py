"""Process executor for isolated test script execution in ATE Platform.

This module provides process-isolated execution of test scripts using
multiprocessing.Pool for true process isolation (not threads).

Key features:
- Process pool for parallel script execution
- Timeout-based execution with hard limits
- Exception capture and result encapsulation
- Event publishing for status changes
- Cancellation support for running tasks

Example:
    >>> from ate_platform.executor import ProcessExecutor
    >>> from ate_platform.scheduler import EventBus, EventType
    >>>
    >>> bus = EventBus()
    >>> executor = ProcessExecutor(event_bus=bus)
    >>>
    >>> result = executor.execute('test_script.py', {'voltage': 3.3})
    >>> print(result.status)
    StepStatus.PASSED
"""

import asyncio
import logging
import multiprocessing
import multiprocessing.pool
import os
from dataclasses import dataclass
from pathlib import Path
from traceback import TracebackException
from typing import Any
from uuid import uuid4

from ..scheduler.event_bus import EventBus, EventType
from ..types import StepResult, StepStatus

logger = logging.getLogger(__name__)


@dataclass
class RunningTask:
    """Tracks a running execution task.

    Attributes:
        task_id: Unique identifier for this task
        step_id: The step being executed
        async_result: The AsyncResult from pool.apply_async
        script_path: Path to the script being executed
    """

    task_id: str
    step_id: str
    async_result: multiprocessing.pool.AsyncResult
    script_path: str


class ProcessExecutor:
    """Process-isolated executor for test scripts.

    Uses multiprocessing.Pool for true process isolation, ensuring
    that script failures don't affect the main process.

    Attributes:
        _pool: The multiprocessing pool
        _max_workers: Maximum number of worker processes
        _script_timeout: Default timeout for script execution (seconds)
        _event_bus: Optional event bus for status notifications
        _running_tasks: Mapping of task_id to RunningTask

    Example:
        >>> executor = ProcessExecutor(max_workers=4, script_timeout=60.0)
        >>> result = executor.execute('tests/test_pass.py', {'value': 42})
        >>> print(result.status)
        StepStatus.PASSED
    """

    _max_workers: int
    _script_timeout: float
    _event_bus: EventBus | None
    _pool: multiprocessing.pool.Pool
    _running_tasks: dict[str, RunningTask]

    def __init__(
        self,
        max_workers: int = 4,
        script_timeout: float = 60.0,
        event_bus: EventBus | None = None,
    ) -> None:
        """Initialize the process executor.

        Args:
            max_workers: Maximum number of worker processes
            script_timeout: Default timeout for script execution (seconds)
            event_bus: Optional event bus for status notifications
        """
        self._max_workers = max_workers
        self._script_timeout = script_timeout
        self._event_bus = event_bus

        # Use 'spawn' context for Windows compatibility
        ctx = multiprocessing.get_context("spawn")
        self._pool = ctx.Pool(processes=max_workers)

        # Track running tasks for cancellation
        self._running_tasks = {}

    def execute(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str | None = None,
        timeout: float | None = None,
    ) -> StepResult:
        """Execute a test script in an isolated process.

        Args:
            script_path: Path to the Python script to execute
            params: Parameters to pass to the script (accessible via context)
            step_id: Optional step identifier (auto-generated if not provided)
            timeout: Optional timeout override (uses default if not provided)

        Returns:
            StepResult containing execution outcome

        Example:
            >>> result = executor.execute('measure_voltage.py', {'channel': 1})
            >>> if result.status == StepStatus.PASSED:
            ...     print(f"Measured: {result.outputs}")
        """
        # Validate script exists
        if not os.path.isfile(script_path):
            error_msg = f"Script not found: {script_path}"
            logger.error(error_msg)
            return StepResult(
                status=StepStatus.ERROR,
                error=error_msg,
            )

        # Generate step ID if not provided
        if step_id is None:
            step_id = f"step_{uuid4().hex[:8]}"

        # Use default timeout if not specified
        effective_timeout = timeout if timeout is not None else self._script_timeout

        # Generate task ID for tracking
        task_id = f"task_{uuid4().hex[:8]}"

        # Publish RUNNING event
        self._publish_status(step_id, StepStatus.RUNNING)

        try:
            # Submit to pool
            async_result = self._pool.apply_async(
                _run_script,
                args=(script_path, params, step_id),
            )

            # Track running task
            running_task = RunningTask(
                task_id=task_id,
                step_id=step_id,
                async_result=async_result,
                script_path=script_path,
            )
            self._running_tasks[task_id] = running_task

            # Wait for result with timeout
            try:
                result_data = async_result.get(timeout=effective_timeout)

                # Extract result
                status_str: str = result_data.get("status", "ERROR")
                status = StepStatus(status_str)
                outputs: dict[str, Any] = result_data.get("outputs", {})
                error: str | None = result_data.get("error")

                result = StepResult(
                    status=status,
                    outputs=outputs,
                    error=error,
                )

                # Publish final status
                self._publish_status(step_id, status)

                return result

            except multiprocessing.TimeoutError:
                # Script exceeded timeout
                error_msg = (
                    f"Script '{script_path}' timed out after {effective_timeout}s"
                )
                logger.warning(error_msg)

                result = StepResult(
                    status=StepStatus.ERROR,
                    error=error_msg,
                )

                # Publish ERROR status
                self._publish_status(step_id, StepStatus.ERROR)

                return result

        except Exception as e:
            # Catch-all for unexpected errors
            error_msg = f"Unexpected error executing '{script_path}': {e}"
            logger.exception(error_msg)

            result = StepResult(
                status=StepStatus.ERROR,
                error=error_msg,
            )

            # Publish ERROR status
            self._publish_status(step_id, StepStatus.ERROR)

            return result

        finally:
            # Clean up tracking
            _ = self._running_tasks.pop(task_id, None)

    def cancel(self, step_id: str) -> bool:
        """Cancel a running execution.

        Note: Due to multiprocessing limitations, cancellation is advisory.
        The process will complete or timeout, but we stop tracking it.

        Args:
            step_id: The step ID to cancel

        Returns:
            True if a task was found and marked for cancellation, False otherwise
        """
        # Find task by step_id
        task_to_cancel = None
        for task in self._running_tasks.values():
            if task.step_id == step_id:
                task_to_cancel = task
                break

        if task_to_cancel is None:
            logger.warning(f"No running task found for step_id: {step_id}")
            return False

        # Remove from tracking
        _ = self._running_tasks.pop(task_to_cancel.task_id, None)

        # Publish SKIPPED status
        self._publish_status(step_id, StepStatus.SKIPPED)

        logger.info(f"Cancelled execution for step: {step_id}")
        return True

    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the process pool.

        Args:
            wait: If True, wait for pending tasks to complete
        """
        if wait:
            self._pool.close()
            self._pool.join()
        else:
            self._pool.terminate()

        logger.info("ProcessExecutor shutdown complete")

    def _publish_status(self, step_id: str, status: StepStatus) -> None:
        """Publish STEP_STATUS_CHANGED event if event bus is configured.

        Args:
            step_id: The step identifier
            status: The new status
        """
        if self._event_bus is not None:
            try:
                _ = asyncio.get_running_loop()
                # We're in an async context, schedule the publish
                _ = asyncio.create_task(
                    self._event_bus.publish(
                        EventType.STEP_STATUS_CHANGED,
                        {"step_id": step_id, "status": status.value},
                    )
                )
            except RuntimeError:
                # No running loop, create one for synchronous publish
                asyncio.run(
                    self._event_bus.publish(
                        EventType.STEP_STATUS_CHANGED,
                        {"step_id": step_id, "status": status.value},
                    )
                )

    def __enter__(self) -> "ProcessExecutor":
        """Context manager entry."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackException | None,
    ) -> None:
        """Context manager exit - shutdown pool."""
        self.shutdown(wait=True)


def _run_script(script_path: str, params: dict[str, Any], step_id: str) -> dict[str, Any]:
    """Internal function to run a script in a worker process.

    This function is executed in a subprocess and handles:
    - Script loading and execution
    - Exception capture
    - Result formatting

    Args:
        script_path: Path to the script to execute
        params: Parameters passed from execute()
        step_id: Step identifier for context

    Returns:
        Dictionary with 'status', 'outputs', and 'error' keys
    """
    try:
        # Create a namespace for the script
        script_globals: dict[str, Any] = {
            "__name__": "__main__",
            "__file__": script_path,
            "params": params,
            "step_id": step_id,
        }

        # Read and compile the script
        script_file = Path(script_path)
        script_content = script_file.read_text(encoding="utf-8")

        # Compile the script
        code = compile(script_content, script_path, "exec")

        # Execute the script
        exec(code, script_globals)

        # Check for explicit result in script
        if "result" in script_globals:
            # Script explicitly set a result
            result = script_globals["result"]
            if isinstance(result, dict):
                return {
                    "status": result.get("status", "PASSED"),
                    "outputs": result.get("outputs", {}),
                    "error": result.get("error"),
                }

        # Default: PASSED if no exception
        # Collect outputs from script_globals (excluding built-ins and non-picklable)
        outputs: dict[str, Any] = {}
        skip_keys = {
            "__name__",
            "__file__",
            "params",
            "step_id",
            "result",
        }
        for key, value in script_globals.items():
            # Skip private attributes and known special keys
            if key.startswith("_") or key in skip_keys:
                continue
            # Skip modules (not picklable across processes)
            if isinstance(value, type(os)):
                continue
            # Skip functions and classes defined in the script
            if callable(value) and not isinstance(value, (int, float, str, bool, list, dict, tuple, set)):
                continue
            outputs[key] = value

        return {
            "status": "PASSED",
            "outputs": outputs,
            "error": None,
        }

    except AssertionError as e:
        # Test assertion failure
        return {
            "status": "FAILED",
            "outputs": {},
            "error": str(e) if str(e) else "Assertion failed",
        }

    except Exception as e:
        # Any other exception is an error
        error_msg = f"{type(e).__name__}: {e}"
        return {
            "status": "ERROR",
            "outputs": {},
            "error": error_msg,
        }
