"""Step executor protocol and implementations for ATE Platform.

This module defines the StepExecutor Protocol that abstracts step execution,
allowing different execution strategies (process-based, thread-based) to be
swapped in via dependency injection.

Key components:
- StepExecutor: Protocol defining execute() and execute_async() signatures
- ProcessStepExecutor: Wraps ProcessExecutor with ThreadPoolExecutor for production
- ThreadStepExecutor: Wraps concurrent.futures.ThreadPoolExecutor for testing/CPU-light scripts

This Protocol is the seam for Task 15 (pool exhaustion guard), which will add
pool_stats() to the interface.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any, Protocol, runtime_checkable

from ..types import StepResult

logger = logging.getLogger(__name__)


@runtime_checkable
class StepExecutor(Protocol):
    """Protocol for step execution strategies.

    Implementations must provide both synchronous and asynchronous execution
    of test scripts. The signatures mirror ProcessExecutor's public API so
    that LoopExecutor can call either implementation transparently.

    Task 15 will extend this Protocol with pool_stats() for pool exhaustion
    monitoring.
    """

    def execute(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str | None = None,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> StepResult:
        """Execute a test script synchronously.

        Args:
            script_path: Path to the Python script to execute
            params: Parameters to pass to the script
            step_id: Optional step identifier
            timeout: Optional timeout override in seconds
            run_id: Optional execution run identifier

        Returns:
            StepResult containing execution outcome
        """
        ...

    async def execute_async(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str | None = None,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> StepResult:
        """Execute a test script asynchronously.

        Args:
            script_path: Path to the Python script to execute
            params: Parameters to pass to the script
            step_id: Optional step identifier
            timeout: Optional timeout override in seconds
            run_id: Optional execution run identifier

        Returns:
            StepResult containing execution outcome
        """
        ...

    async def execute_batch(
        self,
        tasks: list[Any],
        max_concurrency: int | None = None,
    ) -> list[StepResult]:
        """Execute multiple tasks concurrently with bounded concurrency.

        Args:
            tasks: List of ExecuteTask instances describing scripts to run
            max_concurrency: Maximum number of concurrent executions

        Returns:
            List of StepResult in the same order as the input tasks
        """
        ...

    def pool_stats(self) -> dict[str, Any]:
        """Return current worker pool statistics.

        Returns a dictionary with keys:
            active: Number of currently executing tasks
            max: Maximum worker pool size
            utilization: Ratio active/max (0.0 to 1.0+)
            queued: Estimated number of queued tasks (0 if not trackable)

        Returns:
            Dict with active, max, utilization, queued keys
        """
        ...


class ProcessStepExecutor:
    """StepExecutor that wraps ProcessExecutor for production use.

    Delegates to an internal ProcessExecutor instance. The ProcessExecutor
    uses ThreadPoolExecutor by default (I/O-bound scripts) or optionally
    multiprocessing.Pool for CPU-bound scripts requiring true isolation.

    This is the default executor used by ScannerScheduler when no
    step_executor is explicitly provided.

    Attributes:
        _process_executor: The wrapped ProcessExecutor instance
        _active_workers: Count of currently executing tasks
    """

    def __init__(
        self,
        max_workers: int = 4,
        script_timeout: float = 60.0,
        event_bus: Any | None = None,
        use_multiprocessing: bool = False,
    ) -> None:
        """Initialize the process-based step executor.

        Args:
            max_workers: Maximum number of worker threads/processes
            script_timeout: Default timeout for script execution (seconds)
            event_bus: Optional event bus for status notifications
            use_multiprocessing: If True, use multiprocessing.Pool instead
                of ThreadPoolExecutor
        """
        from .process_executor import ProcessExecutor

        self._process_executor = ProcessExecutor(
            max_workers=max_workers,
            script_timeout=script_timeout,
            event_bus=event_bus,
            use_multiprocessing=use_multiprocessing,
        )
        self._active_workers: int = 0

    def execute(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str | None = None,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> StepResult:
        """Execute a test script synchronously via ProcessExecutor.

        Args:
            script_path: Path to the Python script to execute
            params: Parameters to pass to the script
            step_id: Optional step identifier
            timeout: Optional timeout override in seconds
            run_id: Optional execution run identifier

        Returns:
            StepResult containing execution outcome
        """
        self._active_workers += 1
        try:
            return self._process_executor.execute(
                script_path=script_path,
                params=params,
                step_id=step_id,
                timeout=timeout,
                run_id=run_id,
            )
        finally:
            self._active_workers -= 1

    async def execute_async(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str | None = None,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> StepResult:
        """Execute a test script asynchronously via ProcessExecutor.

        Args:
            script_path: Path to the Python script to execute
            params: Parameters to pass to the script
            step_id: Optional step identifier
            timeout: Optional timeout override in seconds
            run_id: Optional execution run identifier

        Returns:
            StepResult containing execution outcome
        """
        self._active_workers += 1
        try:
            return await self._process_executor.execute_async(
                script_path=script_path,
                params=params,
                step_id=step_id,
                timeout=timeout,
                run_id=run_id,
            )
        finally:
            self._active_workers -= 1

    async def execute_batch(
        self,
        tasks: list[Any],
        max_concurrency: int | None = None,
    ) -> list[StepResult]:
        """Execute multiple tasks concurrently via ProcessExecutor.

        Args:
            tasks: List of ExecuteTask instances describing scripts to run
            max_concurrency: Maximum number of concurrent executions

        Returns:
            List of StepResult in the same order as the input tasks
        """
        return await self._process_executor.execute_batch(
            tasks=tasks,
            max_concurrency=max_concurrency,
        )

    @property
    def active_workers(self) -> int:
        """Current number of active worker tasks."""
        return self._active_workers

    @property
    def process_executor(self) -> Any:
        """Access the underlying ProcessExecutor for advanced operations."""
        return self._process_executor

    def pool_stats(self) -> dict[str, Any]:
        """Return current worker pool statistics.

        Delegates to ProcessExecutor's get_pool_utilization() and
        provides the StepExecutor-standard pool_stats format.

        Returns:
            Dict with active, max, utilization, queued keys
        """
        max_workers = self._process_executor._max_workers
        utilization = self._process_executor.get_pool_utilization()
        return {
            "active": self._active_workers,
            "max": max_workers,
            "utilization": utilization,
            "queued": 0,
        }

    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the underlying ProcessExecutor pool.

        Args:
            wait: If True, wait for pending tasks to complete
        """
        self._process_executor.shutdown(wait=wait)


class ThreadStepExecutor:
    """StepExecutor that wraps concurrent.futures.ThreadPoolExecutor directly.

    Designed for testing and CPU-light scripts where the full ProcessExecutor
    machinery (event publishing, multiprocessing support) is unnecessary.
    Executes callables directly in a thread pool without script file validation.

    Attributes:
        _pool: The ThreadPoolExecutor instance
        _max_workers: Maximum number of worker threads
        _active_workers: Count of currently executing tasks
    """

    def __init__(self, max_workers: int = 4) -> None:
        """Initialize the thread-based step executor.

        Args:
            max_workers: Maximum number of worker threads
        """
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self._max_workers = max_workers
        self._active_workers: int = 0

    def execute(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str | None = None,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> StepResult:
        """Execute a test script synchronously in a thread pool.

        Runs the script in a worker thread and blocks until completion.

        Args:
            script_path: Path to the Python script to execute
            params: Parameters to pass to the script
            step_id: Optional step identifier
            timeout: Optional timeout override in seconds
            run_id: Optional execution run identifier

        Returns:
            StepResult containing execution outcome
        """
        from ..types import StepStatus

        self._active_workers += 1
        try:
            future = self._pool.submit(
                self._run_script, script_path, params, step_id,
            )
            effective_timeout = timeout or 60.0
            try:
                return future.result(timeout=effective_timeout)
            except concurrent.futures.TimeoutError:
                error_msg = (
                    f"Script '{script_path}' timed out after {effective_timeout}s"
                )
                logger.warning(error_msg)
                return StepResult(status=StepStatus.ERROR, error=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error executing '{script_path}': {e}"
            logger.exception(error_msg)
            return StepResult(status=StepStatus.ERROR, error=error_msg)
        finally:
            self._active_workers -= 1

    async def execute_async(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str | None = None,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> StepResult:
        """Execute a test script asynchronously in a thread pool.

        Submits the script to the pool and returns an awaitable.

        Args:
            script_path: Path to the Python script to execute
            params: Parameters to pass to the script
            step_id: Optional step identifier
            timeout: Optional timeout override in seconds
            run_id: Optional execution run identifier

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

        Args:
            tasks: List of ExecuteTask instances describing scripts to run
            max_concurrency: Maximum number of concurrent executions

        Returns:
            List of StepResult in the same order as the input tasks
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

    @property
    def active_workers(self) -> int:
        """Current number of active worker tasks."""
        return self._active_workers

    def pool_stats(self) -> dict[str, Any]:
        """Return current worker pool statistics.

        Returns:
            Dict with active, max, utilization, queued keys
        """
        utilization = self._active_workers / self._max_workers if self._max_workers > 0 else 0.0
        return {
            "active": self._active_workers,
            "max": self._max_workers,
            "utilization": utilization,
            "queued": 0,
        }

    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the thread pool.

        Args:
            wait: If True, wait for pending tasks to complete
        """
        self._pool.shutdown(wait=wait)

    @staticmethod
    def _run_script(
        script_path: str,
        params: dict[str, Any],
        step_id: str | None = None,
    ) -> StepResult:
        """Run a script in a thread and return StepResult.

        Executes the script file in a shared thread context (no process
        isolation). Uses the same script execution protocol as
        ProcessExecutor's _run_script_in_thread.

        Args:
            script_path: Path to the Python script to execute
            params: Parameters to pass to the script
            step_id: Optional step identifier

        Returns:
            StepResult with execution outcome
        """
        import os

        from ..types import StepStatus

        if not os.path.isfile(script_path):
            error_msg = f"Script not found: {script_path}"
            logger.error(error_msg)
            return StepResult(status=StepStatus.ERROR, error=error_msg)

        # Build execution namespace
        exec_namespace: dict[str, Any] = {"params": params}
        if step_id is not None:
            exec_namespace["step_id"] = step_id

        try:
            with open(script_path, encoding="utf-8") as f:
                code = compile(f.read(), script_path, "exec")
                exec(code, exec_namespace)  # noqa: S102

            # Extract outputs (same protocol as ProcessExecutor)
            outputs: dict[str, Any] = {}
            for key, value in exec_namespace.items():
                if key.startswith("result_") and not key.startswith("__"):
                    outputs[key[7:]] = value

            return StepResult(status=StepStatus.PASSED, outputs=outputs)

        except AssertionError as e:
            error_msg = str(e) if str(e) else f"Assertion failed in {script_path}"
            return StepResult(status=StepStatus.FAILED, error=error_msg)

        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            return StepResult(status=StepStatus.ERROR, error=error_msg)
