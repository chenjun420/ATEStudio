"""Process executor for isolated test script execution in ATE Platform.

This module provides process-isolated execution of test scripts using
concurrent.futures.ThreadPoolExecutor (default, I/O-bound) or
multiprocessing.Pool (optional, CPU-bound).

Key features:
- Thread pool (default) for I/O-bound test scripts (PyVISA, socket, file I/O)
- Process pool (optional) for CPU-bound scripts requiring true isolation
- Async execution via asyncio.to_thread for integration with async event loop
- Batch execution with concurrency control via asyncio.Semaphore
- Timeout-based execution with hard limits
- Exception capture and result encapsulation
- Event publishing for status changes (thread-safe publish_sync)
- Cancellation support for running tasks
- run_id tracking for execution context correlation

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
import concurrent.futures
import logging
import multiprocessing
import multiprocessing.pool
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from traceback import TracebackException
from typing import Any
from uuid import uuid4

from shared.events import EventType

from ..scheduler.event_bus import EventBus
from ..types import StepResult, StepStatus

logger = logging.getLogger(__name__)


@dataclass
class RunningTask:
    """Tracks a running execution task.

    Attributes:
        task_id: Unique identifier for this task
        step_id: The step being executed
        async_result: The future/AsyncResult from pool submission
        script_path: Path to the script being executed
    """

    task_id: str
    step_id: str
    async_result: Any  # concurrent.futures.Future | multiprocessing.pool.AsyncResult
    script_path: str


class ProcessExecutor:
    """Executor for test scripts with thread or process isolation.

    Uses concurrent.futures.ThreadPoolExecutor by default (I/O-bound scripts,
    threading+asyncio model, GIL acceptable for PyVISA/socket/file I/O).
    Optionally uses multiprocessing.Pool for CPU-bound scripts requiring
    true process isolation.

    Attributes:
        _pool: The executor pool (ThreadPoolExecutor or multiprocessing.Pool)
        _use_multiprocessing: Whether using multiprocessing.Pool
        _max_workers: Maximum number of workers
        _script_timeout: Default timeout for script execution (seconds)
        _event_bus: Optional event bus for status notifications
        _running_tasks: Mapping of task_id to RunningTask
        _active_count: Atomic counter of currently active worker tasks (thread-safe)
        _active_lock: Lock protecting _active_count
        _execution_context: Optional ExecutionContext for run tracking

    Example:
        >>> executor = ProcessExecutor(max_workers=4, script_timeout=60.0)
        >>> result = executor.execute('tests/test_pass.py', {'value': 42})
        >>> print(result.status)
        StepStatus.PASSED
    """

    _max_workers: int
    _script_timeout: float
    _event_bus: EventBus | None
    _pool: concurrent.futures.ThreadPoolExecutor | multiprocessing.pool.Pool
    _use_multiprocessing: bool
    _running_tasks: dict[str, RunningTask]

    def __init__(
        self,
        max_workers: int = 4,
        script_timeout: float = 60.0,
        event_bus: EventBus | None = None,
        use_multiprocessing: bool = False,
    ) -> None:
        """Initialize the process executor.

        Args:
            max_workers: Maximum number of worker threads/processes
            script_timeout: Default timeout for script execution (seconds)
            event_bus: Optional event bus for status notifications
            use_multiprocessing: If True, use multiprocessing.Pool instead
                of ThreadPoolExecutor. Use for CPU-bound scripts that need
                true process isolation.
        """
        self._max_workers = max_workers
        self._script_timeout = script_timeout
        self._event_bus = event_bus
        self._use_multiprocessing = use_multiprocessing

        if use_multiprocessing:
            # Use 'spawn' context for Windows compatibility
            ctx = multiprocessing.get_context("spawn")
            self._pool = ctx.Pool(processes=max_workers)
        else:
            self._pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
            )

        # Track running tasks for cancellation
        self._running_tasks = {}

        # Atomic active worker counter for pool exhaustion monitoring
        self._active_count: int = 0
        self._active_lock: threading.Lock = threading.Lock()

    def get_pool_utilization(self) -> float:
        """Return the ratio of active workers to max workers.

        Thread-safe read of both active count and max workers.
        Returns a float >= 0.0 where 1.0 means pool is fully utilized.

        Returns:
            Utilization ratio (active / max_workers)
        """
        with self._active_lock:
            active = self._active_count
        return active / self._max_workers if self._max_workers > 0 else 0.0

    def execute(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str | None = None,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> StepResult:
        """Execute a test script in an isolated worker.

        Args:
            script_path: Path to the Python script to execute
            params: Parameters to pass to the script (accessible via context)
            step_id: Optional step identifier (auto-generated if not provided)
            timeout: Optional timeout override (uses default if not provided)
            run_id: Optional execution run identifier for context tracking

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
        self._publish_status(step_id, StepStatus.RUNNING, run_id=run_id)

        # Track active worker count for pool exhaustion monitoring
        with self._active_lock:
            self._active_count += 1

        try:
            if self._use_multiprocessing:
                result = self._execute_multiprocessing(
                    script_path, params, step_id, task_id, effective_timeout, run_id,
                )
            else:
                result = self._execute_threadpool(
                    script_path, params, step_id, task_id, effective_timeout, run_id,
                )
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
            self._publish_status(step_id, StepStatus.ERROR, run_id=run_id)

            return result

        finally:
            # Decrement active worker count
            with self._active_lock:
                self._active_count -= 1
            # Clean up tracking
            _ = self._running_tasks.pop(task_id, None)

    def _execute_threadpool(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str,
        task_id: str,
        effective_timeout: float,
        run_id: str | None,
    ) -> StepResult:
        """Execute script using ThreadPoolExecutor.

        Runs _run_script_in_thread which shares memory with the main process,
        allowing VariableSpace access and simpler I/O-bound execution.
        """
        # Submit to thread pool. Runtime pool type is guaranteed by
        # _use_multiprocessing (this is the ThreadPoolExecutor branch).
        future = self._pool.submit(  # type: ignore[union-attr]
            _run_script_in_thread,
            script_path,
            params,
            step_id,
        )

        # Track running task
        running_task = RunningTask(
            task_id=task_id,
            step_id=step_id,
            async_result=future,
            script_path=script_path,
        )
        self._running_tasks[task_id] = running_task

        # Wait for result with timeout
        try:
            result_data = future.result(timeout=effective_timeout)
        except concurrent.futures.TimeoutError:
            # Script exceeded timeout
            error_msg = (
                f"Script '{script_path}' timed out after {effective_timeout}s"
            )
            logger.warning(error_msg)

            result = StepResult(
                status=StepStatus.ERROR,
                error=error_msg,
            )

            self._publish_status(step_id, StepStatus.ERROR, run_id=run_id)
            return result

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
        self._publish_status(step_id, status, run_id=run_id)

        return result

    def _execute_multiprocessing(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str,
        task_id: str,
        effective_timeout: float,
        run_id: str | None,
    ) -> StepResult:
        """Execute script using multiprocessing.Pool.

        Runs _run_script in a subprocess for true process isolation.
        Results are returned as dicts (pickled across process boundary).
        """
        # Submit to process pool (multiprocessing.Pool branch — see shutdown()).
        async_result = self._pool.apply_async(  # type: ignore[union-attr]
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

            self._publish_status(step_id, StepStatus.ERROR, run_id=run_id)
            return result

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
        self._publish_status(step_id, status, run_id=run_id)

        return result

    async def execute_async(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str | None = None,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> StepResult:
        """Execute a test script asynchronously.

        Wraps the synchronous execute() method via asyncio.to_thread(),
        allowing non-blocking execution within an async event loop.

        Args:
            script_path: Path to the Python script to execute
            params: Parameters to pass to the script
            step_id: Optional step identifier (auto-generated if not provided)
            timeout: Optional timeout override (uses default if not provided)
            run_id: Optional execution run identifier for context tracking

        Returns:
            StepResult containing execution outcome
        """
        return await asyncio.to_thread(
            self.execute,
            script_path,
            params,
            step_id=step_id,
            timeout=timeout,
            run_id=run_id,
        )

    async def execute_batch(
        self,
        tasks: list[Any],
        max_concurrency: int | None = None,
    ) -> list[StepResult]:
        """Execute multiple tasks concurrently with bounded concurrency.

        Uses asyncio.Semaphore to limit the number of concurrent executions.
        Each task is executed via execute_async().

        Args:
            tasks: List of ExecuteTask instances describing scripts to run
            max_concurrency: Maximum number of concurrent executions.
                Defaults to self._max_workers if not specified.

        Returns:
            List of StepResult in the same order as the input tasks.
        """
        from shared.types import ExecuteTask

        concurrency = max_concurrency if max_concurrency is not None else self._max_workers
        semaphore = asyncio.Semaphore(concurrency)

        async def _run_with_semaphore(task: ExecuteTask) -> StepResult:
            async with semaphore:
                return await self.execute_async(
                    script_path=task.script_path,
                    params=task.params,
                    step_id=task.step_id,
                    timeout=task.timeout,
                    run_id=task.run_id,
                )

        coros = [_run_with_semaphore(task) for task in tasks]
        results = await asyncio.gather(*coros, return_exceptions=False)
        return list(results)

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

        # For ThreadPoolExecutor, attempt to cancel the future
        if not self._use_multiprocessing:
            task_to_cancel.async_result.cancel()

        # Publish SKIPPED status
        self._publish_status(step_id, StepStatus.SKIPPED)

        logger.info(f"Cancelled execution for step: {step_id}")
        return True

    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the executor pool.

        Args:
            wait: If True, wait for pending tasks to complete
        """
        if self._use_multiprocessing:
            if wait:
                self._pool.close()  # type: ignore[union-attr]
                self._pool.join()  # type: ignore[union-attr]
            else:
                self._pool.terminate()  # type: ignore[union-attr]
        else:
            # ThreadPoolExecutor: shutdown(wait=...) handles both cases
            self._pool.shutdown(wait=wait)  # type: ignore[union-attr]

        logger.info("ProcessExecutor shutdown complete")

    def _publish_status(
        self,
        step_id: str,
        status: StepStatus,
        run_id: str | None = None,
    ) -> None:
        """Publish STEP_STATUS_CHANGED event if event bus is configured.

        Uses the thread-safe publish_sync() method instead of asyncio.run()
        to ensure events reach the correct event loop.

        Args:
            step_id: The step identifier
            status: The new status
            run_id: Optional execution run identifier
        """
        if self._event_bus is not None:
            from dataclasses import asdict

            from shared.events import StepStatusChangedData

            data = asdict(StepStatusChangedData(
                step_id=step_id,
                old_status="",
                new_status=status.value,
                run_id=run_id,
            ))
            self._event_bus.publish_sync(EventType.STEP_STATUS_CHANGED, data)

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


def _run_script_in_thread(script_path: str, params: dict[str, Any], step_id: str) -> dict[str, Any]:
    """Internal function to run a script in a worker thread.

    This function is executed in a thread and shares memory with the main
    process, allowing VariableSpace access. It handles:
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
        # Collect outputs from script_globals (excluding built-ins)
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
            # Skip modules
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


def _run_script(script_path: str, params: dict[str, Any], step_id: str) -> dict[str, Any]:
    """Internal function to run a script in a worker process.

    This function is executed in a subprocess and handles:
    - Script loading and execution
    - Exception capture
    - Result formatting

    Used only when use_multiprocessing=True. Results must be picklable
    since they cross process boundaries.

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
