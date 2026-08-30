"""Unit tests for StepExecutor Protocol and implementations.

Tests cover:
- StepExecutor Protocol structural subtyping
- ProcessStepExecutor delegation to ProcessExecutor
- ThreadStepExecutor direct thread pool execution
- Executor injection in ScannerScheduler
- Error propagation with proper logging
- Active worker tracking
"""

import os
import tempfile
from typing import Any

import pytest

from ate_platform.executor.step_executor import (
    ProcessStepExecutor,
    StepExecutor,
    ThreadStepExecutor,
)
from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.scheduler.event_bus import EventBus
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
from ate_platform.scheduler.step_registry import StepRegistry
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import StepResult, StepStatus


@pytest.fixture
def examples_dir() -> str:
    """Get the path to examples directory as string."""
    return str(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "examples")
    )


@pytest.fixture
def fixtures_dir() -> str:
    """Get the path to test fixtures directory as string."""
    return str(
        os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")
    )


class TestStepExecutorProtocol:
    """Tests for StepExecutor Protocol structural subtyping."""

    def test_process_step_executor_satisfies_protocol(self) -> None:
        """ProcessStepExecutor should satisfy StepExecutor Protocol."""
        executor = ProcessStepExecutor(max_workers=2)
        assert isinstance(executor, StepExecutor)
        executor.shutdown()

    def test_thread_step_executor_satisfies_protocol(self) -> None:
        """ThreadStepExecutor should satisfy StepExecutor Protocol."""
        executor = ThreadStepExecutor(max_workers=2)
        assert isinstance(executor, StepExecutor)
        executor.shutdown()

    def test_custom_class_satisfies_protocol(self) -> None:
        """A custom class with matching signatures should satisfy Protocol."""

        class CustomExecutor:
            def execute(
                self,
                script_path: str,
                params: dict,
                step_id: str | None = None,
                timeout: float | None = None,
                run_id: str | None = None,
            ) -> StepResult:
                return StepResult(status=StepStatus.PASSED)

            async def execute_async(
                self,
                script_path: str,
                params: dict,
                step_id: str | None = None,
                timeout: float | None = None,
                run_id: str | None = None,
            ) -> StepResult:
                return StepResult(status=StepStatus.PASSED)

            async def execute_batch(
                self,
                tasks: list,
                max_concurrency: int | None = None,
            ) -> list[StepResult]:
                return []

            def pool_stats(self) -> dict[str, Any]:
                return {"active": 0, "max": 1, "utilization": 0.0, "queued": 0}

        assert isinstance(CustomExecutor(), StepExecutor)


class TestProcessStepExecutor:
    """Tests for ProcessStepExecutor delegation to ProcessExecutor."""

    def test_execute_passing_script(
        self, examples_dir: str
    ) -> None:
        """Should execute a passing script and return PASSED status."""
        executor = ProcessStepExecutor(max_workers=2, script_timeout=5.0)
        try:
            script_path = os.path.join(examples_dir, "test_pass.py")
            result = executor.execute(script_path, {"value": 42})

            assert result.status == StepStatus.PASSED
            assert result.error is None
        finally:
            executor.shutdown()

    def test_execute_script_not_found(self) -> None:
        """Should return ERROR status for non-existent script."""
        executor = ProcessStepExecutor(max_workers=2, script_timeout=5.0)
        try:
            result = executor.execute("/nonexistent/script.py", {})

            assert result.status == StepStatus.ERROR
            assert "not found" in result.error.lower()
        finally:
            executor.shutdown()

    @pytest.mark.asyncio
    async def test_execute_async_passing_script(
        self, examples_dir: str
    ) -> None:
        """Should execute a passing script asynchronously."""
        executor = ProcessStepExecutor(max_workers=2, script_timeout=5.0)
        try:
            script_path = os.path.join(examples_dir, "test_pass.py")
            result = await executor.execute_async(script_path, {"value": 42})

            assert result.status == StepStatus.PASSED
            assert result.error is None
        finally:
            executor.shutdown()

    @pytest.mark.asyncio
    async def test_execute_async_script_not_found(self) -> None:
        """Should return ERROR for non-existent script asynchronously."""
        executor = ProcessStepExecutor(max_workers=2, script_timeout=5.0)
        try:
            result = await executor.execute_async("/nonexistent/script.py", {})

            assert result.status == StepStatus.ERROR
            assert "not found" in result.error.lower()
        finally:
            executor.shutdown()

    def test_active_workers_tracking(
        self, examples_dir: str
    ) -> None:
        """Should track active worker count during execution."""
        executor = ProcessStepExecutor(max_workers=2, script_timeout=5.0)
        try:
            assert executor.active_workers == 0

            script_path = os.path.join(examples_dir, "test_pass.py")
            result = executor.execute(script_path, {"value": 42})

            # After completion, active_workers should be back to 0
            assert executor.active_workers == 0
            assert result.status == StepStatus.PASSED
        finally:
            executor.shutdown()

    def test_process_executor_accessible(self) -> None:
        """Should expose underlying ProcessExecutor for advanced operations."""
        executor = ProcessStepExecutor(max_workers=2)
        try:
            from ate_platform.executor.process_executor import ProcessExecutor

            assert isinstance(executor.process_executor, ProcessExecutor)
        finally:
            executor.shutdown()


class TestThreadStepExecutor:
    """Tests for ThreadStepExecutor direct thread pool execution."""

    def test_execute_passing_script(
        self, examples_dir: str
    ) -> None:
        """Should execute a passing script and return PASSED status."""
        executor = ThreadStepExecutor(max_workers=2)
        try:
            script_path = os.path.join(examples_dir, "test_pass.py")
            result = executor.execute(script_path, {"value": 42})

            assert result.status == StepStatus.PASSED
            assert result.error is None
        finally:
            executor.shutdown()

    def test_execute_script_not_found(self) -> None:
        """Should return ERROR status for non-existent script."""
        executor = ThreadStepExecutor(max_workers=2)
        try:
            result = executor.execute("/nonexistent/script.py", {})

            assert result.status == StepStatus.ERROR
            assert "not found" in result.error.lower()
        finally:
            executor.shutdown()

    def test_execute_failing_script(
        self, examples_dir: str
    ) -> None:
        """Should execute a failing script and return FAILED status."""
        executor = ThreadStepExecutor(max_workers=2)
        try:
            script_path = os.path.join(examples_dir, "test_fail.py")
            result = executor.execute(
                script_path, {"expected": 100, "actual": 50}
            )

            assert result.status == StepStatus.FAILED
            assert result.error is not None
        finally:
            executor.shutdown()

    def test_execute_error_script(
        self, examples_dir: str
    ) -> None:
        """Should execute an erroring script and return ERROR status."""
        executor = ThreadStepExecutor(max_workers=2)
        try:
            script_path = os.path.join(examples_dir, "test_error.py")
            result = executor.execute(script_path, {"simulate_error": True})

            assert result.status == StepStatus.ERROR
            assert result.error is not None
        finally:
            executor.shutdown()

    @pytest.mark.asyncio
    async def test_execute_async_passing_script(
        self, examples_dir: str
    ) -> None:
        """Should execute a passing script asynchronously."""
        executor = ThreadStepExecutor(max_workers=2)
        try:
            script_path = os.path.join(examples_dir, "test_pass.py")
            result = await executor.execute_async(script_path, {"value": 42})

            assert result.status == StepStatus.PASSED
            assert result.error is None
        finally:
            executor.shutdown()

    @pytest.mark.asyncio
    async def test_execute_async_script_not_found(self) -> None:
        """Should return ERROR for non-existent script asynchronously."""
        executor = ThreadStepExecutor(max_workers=2)
        try:
            result = await executor.execute_async("/nonexistent/script.py", {})

            assert result.status == StepStatus.ERROR
            assert "not found" in result.error.lower()
        finally:
            executor.shutdown()

    def test_active_workers_tracking(
        self, examples_dir: str
    ) -> None:
        """Should track active worker count during execution."""
        executor = ThreadStepExecutor(max_workers=2)
        try:
            assert executor.active_workers == 0

            script_path = os.path.join(examples_dir, "test_pass.py")
            result = executor.execute(script_path, {"value": 42})

            # After completion, active_workers should be back to 0
            assert executor.active_workers == 0
            assert result.status == StepStatus.PASSED
        finally:
            executor.shutdown()

    def test_execute_with_outputs(
        self, examples_dir: str
    ) -> None:
        """Should capture script outputs via result_ prefix convention."""
        executor = ThreadStepExecutor(max_workers=2)
        try:
            # Create a temp script that sets result_ outputs
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False
            ) as f:
                f.write(
                    "result_voltage = params['voltage']\n"
                    "result_current = params['current']\n"
                )
                script_path = f.name

            try:
                result = executor.execute(
                    script_path, {"voltage": 5.0, "current": 1.0}
                )
                assert result.status == StepStatus.PASSED
                assert result.outputs["voltage"] == 5.0
                assert result.outputs["current"] == 1.0
            finally:
                os.unlink(script_path)
        finally:
            executor.shutdown()

    @pytest.mark.asyncio
    async def test_execute_batch(
        self, examples_dir: str
    ) -> None:
        """Should execute multiple tasks concurrently via execute_batch."""
        from shared.types import ExecuteTask

        executor = ThreadStepExecutor(max_workers=2)
        try:
            script_path = os.path.join(examples_dir, "test_pass.py")
            tasks = [
                ExecuteTask(script_path=script_path, params={"value": i})
                for i in range(3)
            ]

            results = await executor.execute_batch(tasks)

            assert len(results) == 3
            for result in results:
                assert result.status == StepStatus.PASSED
        finally:
            executor.shutdown()


class TestExecutorInjection:
    """Tests for executor injection in ScannerScheduler."""

    def test_default_step_executor_is_process(self) -> None:
        """ScannerScheduler should default to ProcessStepExecutor."""
        event_bus = EventBus()
        registry = StepRegistry()
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        assert isinstance(scheduler._step_executor, ProcessStepExecutor)

    def test_custom_step_executor_injected(self) -> None:
        """ScannerScheduler should use injected StepExecutor."""
        event_bus = EventBus()
        registry = StepRegistry()
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        custom_executor = ThreadStepExecutor(max_workers=2)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
            step_executor=custom_executor,
        )

        assert scheduler._step_executor is custom_executor

    def test_process_step_executor_injected(self) -> None:
        """ScannerScheduler should accept ProcessStepExecutor explicitly."""
        event_bus = EventBus()
        registry = StepRegistry()
        evaluator = ConditionEvaluator({}, None, None)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        process_executor = ProcessStepExecutor(
            max_workers=2, event_bus=event_bus
        )

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
            step_executor=process_executor,
        )

        assert scheduler._step_executor is process_executor


class TestErrorPropagation:
    """Tests for error propagation through executors."""

    def test_process_step_executor_propagates_error(self) -> None:
        """ProcessStepExecutor should propagate script errors as StepResult."""
        executor = ProcessStepExecutor(max_workers=2, script_timeout=5.0)
        try:
            result = executor.execute("/nonexistent/script.py", {})

            assert result.status == StepStatus.ERROR
            assert result.error is not None
        finally:
            executor.shutdown()

    def test_thread_step_executor_propagates_error(self) -> None:
        """ThreadStepExecutor should propagate script errors as StepResult."""
        executor = ThreadStepExecutor(max_workers=2)
        try:
            result = executor.execute("/nonexistent/script.py", {})

            assert result.status == StepStatus.ERROR
            assert result.error is not None
        finally:
            executor.shutdown()

    @pytest.mark.asyncio
    async def test_process_step_executor_async_propagates_error(self) -> None:
        """ProcessStepExecutor should propagate errors in async execution."""
        executor = ProcessStepExecutor(max_workers=2, script_timeout=5.0)
        try:
            result = await executor.execute_async("/nonexistent/script.py", {})

            assert result.status == StepStatus.ERROR
            assert result.error is not None
        finally:
            executor.shutdown()

    @pytest.mark.asyncio
    async def test_thread_step_executor_async_propagates_error(self) -> None:
        """ThreadStepExecutor should propagate errors in async execution."""
        executor = ThreadStepExecutor(max_workers=2)
        try:
            result = await executor.execute_async("/nonexistent/script.py", {})

            assert result.status == StepStatus.ERROR
            assert result.error is not None
        finally:
            executor.shutdown()

    def test_thread_step_executor_timeout(self) -> None:
        """ThreadStepExecutor should timeout long-running scripts."""
        executor = ThreadStepExecutor(max_workers=2)
        try:
            # Create a script that sleeps forever
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False
            ) as f:
                f.write("import time; time.sleep(30)\n")
                script_path = f.name

            try:
                result = executor.execute(
                    script_path, {}, timeout=0.5
                )
                assert result.status == StepStatus.ERROR
                assert "timed out" in result.error.lower()
            except PermissionError:
                # Windows: thread may still hold file handle after timeout
                pass
            finally:
                # Best-effort cleanup; thread may still hold handle on Windows
                try:
                    os.unlink(script_path)
                except PermissionError:
                    pass
        finally:
            executor.shutdown(wait=False)
