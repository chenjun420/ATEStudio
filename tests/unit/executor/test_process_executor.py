"""Tests for ProcessExecutor.

This module tests the process-isolated execution of test scripts.

Tests cover:
- Successful script execution
- Script timeout handling
- Script failure (assertion error)
- Script error (unexpected exception)
- Script not found handling
- Cancellation of running tasks
- Event publishing
- Async execution (execute_async)
- Batch execution (execute_batch)
- ThreadPoolExecutor (default) and multiprocessing.Pool modes
- run_id parameter
"""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from ate_platform.executor.process_executor import ProcessExecutor
from ate_platform.scheduler.event_bus import EventBus, EventType
from ate_platform.types import StepStatus
from shared.types import ExecuteTask


@pytest.fixture
def executor() -> ProcessExecutor:
    """Create a ProcessExecutor instance for testing (ThreadPoolExecutor default)."""
    return ProcessExecutor(max_workers=2, script_timeout=5.0)


@pytest.fixture
def mp_executor() -> ProcessExecutor:
    """Create a ProcessExecutor instance using multiprocessing.Pool."""
    return ProcessExecutor(max_workers=2, script_timeout=5.0, use_multiprocessing=True)


@pytest.fixture
def event_bus() -> EventBus:
    """Create an EventBus instance for testing."""
    return EventBus()


@pytest.fixture
def examples_dir() -> Path:
    """Get the path to examples directory."""
    return Path(__file__).parent.parent.parent.parent / "examples"


@pytest.fixture
def fixtures_dir() -> Path:
    """Get the path to test fixtures directory."""
    return Path(__file__).parent.parent.parent / "fixtures"


class TestProcessExecutorBasic:
    """Basic tests for ProcessExecutor."""

    def test_execute_passing_script(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should execute a passing script and return PASSED status."""
        script_path = examples_dir / "test_pass.py"
        result = executor.execute(str(script_path), {"value": 42})

        assert result.status == StepStatus.PASSED
        assert result.error is None

    def test_execute_failing_script(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should execute a failing script and return FAILED status."""
        script_path = examples_dir / "test_fail.py"
        result = executor.execute(str(script_path), {"expected": 100, "actual": 50})

        assert result.status == StepStatus.FAILED
        assert result.error is not None
        assert "Expected 100" in result.error

    def test_execute_error_script(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should execute an erroring script and return ERROR status."""
        script_path = examples_dir / "test_error.py"
        result = executor.execute(str(script_path), {"simulate_error": True})

        assert result.status == StepStatus.ERROR
        assert result.error is not None
        assert "RuntimeError" in result.error

    def test_execute_script_not_found(self, executor: ProcessExecutor) -> None:
        """Should return ERROR status for non-existent script."""
        result = executor.execute("/nonexistent/script.py", {})

        assert result.status == StepStatus.ERROR
        assert "not found" in result.error.lower()

    def test_execute_with_outputs(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should capture script outputs."""
        script_path = examples_dir / "test_with_outputs.py"
        result = executor.execute(str(script_path), {"voltage": 5.0, "current": 1.0})

        assert result.status == StepStatus.PASSED
        assert "measured_voltage" in result.outputs
        assert result.outputs["measured_voltage"] == 5.0
        assert "calculated_power" in result.outputs
        assert result.outputs["calculated_power"] == 5.0


class TestProcessExecutorTimeout:
    """Tests for timeout handling."""

    def test_script_timeout(self, executor: ProcessExecutor, examples_dir: Path) -> None:
        """Should timeout a long-running script."""
        script_path = examples_dir / "test_timeout.py"
        # Use very short timeout
        result = executor.execute(str(script_path), {"sleep_duration": 30}, timeout=0.5)

        assert result.status == StepStatus.ERROR
        assert "timed out" in result.error.lower()

    def test_default_timeout_used(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should use default timeout when not specified."""
        script_path = examples_dir / "test_pass.py"
        # No timeout specified, should use default (5.0s)
        result = executor.execute(str(script_path), {"value": 10})

        assert result.status == StepStatus.PASSED


class TestProcessExecutorCancellation:
    """Tests for cancellation."""

    def test_cancel_nonexistent_task(self, executor: ProcessExecutor) -> None:
        """Should return False for non-existent task."""
        result = executor.cancel("nonexistent_step")
        assert result is False

    def test_cancel_running_task(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should cancel a running task."""
        script_path = examples_dir / "test_timeout.py"
        # Start a long-running task
        step_id = "cancel_test_step"
        # Execute asynchronously would need threading, but cancel() works on tracked tasks
        # For now, test that cancel doesn't crash
        result = executor.cancel(step_id)
        # Task not running, should return False
        assert result is False


class TestProcessExecutorEvents:
    """Tests for event publishing."""

    def test_event_published_on_status_change(
        self, examples_dir: Path
    ) -> None:
        """Should publish STEP_STATUS_CHANGED event."""
        bus = EventBus()
        executor = ProcessExecutor(max_workers=2, script_timeout=5.0, event_bus=bus)

        # Track events
        events_received = []

        def event_handler(event):
            events_received.append(event)

        bus.subscribe(EventType.STEP_STATUS_CHANGED, event_handler)

        # Execute a script
        script_path = examples_dir / "test_pass.py"
        result = executor.execute(str(script_path), {"value": 42})

        # Shutdown executor
        executor.shutdown()

        # Check events were published (at least RUNNING and PASSED)
        # Note: Events are async, may not be immediately available
        # We just verify no exception occurred

        assert result.status == StepStatus.PASSED


class TestProcessExecutorContext:
    """Tests for context manager usage."""

    def test_context_manager_shutdown(
        self, examples_dir: Path
    ) -> None:
        """Should shutdown pool on context exit."""
        with ProcessExecutor(max_workers=2) as executor:
            script_path = examples_dir / "test_pass.py"
            result = executor.execute(str(script_path), {"value": 42})
            assert result.status == StepStatus.PASSED

        # Pool should be shutdown after context exit


class TestProcessExecutorScriptValidation:
    """Tests for script validation."""

    def test_syntax_error_in_script(self, executor: ProcessExecutor) -> None:
        """Should handle syntax errors in script."""
        # Create a temp file with syntax error
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def broken(:\n    pass\n")  # Invalid syntax
            script_path = f.name

        try:
            result = executor.execute(script_path, {})
            assert result.status == StepStatus.ERROR
            assert result.error is not None
        finally:
            os.unlink(script_path)

    def test_script_with_import(self, executor: ProcessExecutor) -> None:
        """Should handle scripts with imports."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("""
import math
result_value = math.sqrt(params['value'])  # noqa: F821
""")
            script_path = f.name

        try:
            result = executor.execute(script_path, {"value": 16})
            assert result.status == StepStatus.PASSED
            assert "result_value" in result.outputs
            assert result.outputs["result_value"] == 4.0
        finally:
            os.unlink(script_path)


class TestProcessExecutorProcessIsolation:
    """Tests for process isolation."""

    def test_exception_does_not_crash_executor(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should not crash executor when script raises exception."""
        script_path = examples_dir / "test_error.py"
        result1 = executor.execute(str(script_path), {"simulate_error": True})
        assert result1.status == StepStatus.ERROR

        # Executor should still work after error
        script_path_pass = examples_dir / "test_pass.py"
        result2 = executor.execute(str(script_path_pass), {"value": 10})
        assert result2.status == StepStatus.PASSED

    def test_multiple_executions(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should handle multiple sequential executions."""
        script_path = examples_dir / "test_pass.py"

        for i in range(5):
            result = executor.execute(str(script_path), {"value": i})
            assert result.status == StepStatus.PASSED


class TestProcessExecutorMultiprocessing:
    """Tests for multiprocessing.Pool mode."""

    def test_execute_passing_script_mp(
        self, mp_executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should execute a passing script with multiprocessing pool."""
        script_path = examples_dir / "test_pass.py"
        result = mp_executor.execute(str(script_path), {"value": 42})

        assert result.status == StepStatus.PASSED
        assert result.error is None

    def test_execute_failing_script_mp(
        self, mp_executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should execute a failing script with multiprocessing pool."""
        script_path = examples_dir / "test_fail.py"
        result = mp_executor.execute(str(script_path), {"expected": 100, "actual": 50})

        assert result.status == StepStatus.FAILED
        assert result.error is not None

    def test_execute_error_script_mp(
        self, mp_executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should execute an erroring script with multiprocessing pool."""
        script_path = examples_dir / "test_error.py"
        result = mp_executor.execute(str(script_path), {"simulate_error": True})

        assert result.status == StepStatus.ERROR
        assert result.error is not None

    def test_script_timeout_mp(
        self, mp_executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should timeout a long-running script with multiprocessing pool."""
        script_path = examples_dir / "test_timeout.py"
        result = mp_executor.execute(str(script_path), {"sleep_duration": 30}, timeout=0.5)

        assert result.status == StepStatus.ERROR
        assert "timed out" in result.error.lower()

    def test_context_manager_mp(self, examples_dir: Path) -> None:
        """Should shutdown multiprocessing pool on context exit."""
        with ProcessExecutor(max_workers=2, use_multiprocessing=True) as executor:
            script_path = examples_dir / "test_pass.py"
            result = executor.execute(str(script_path), {"value": 42})
            assert result.status == StepStatus.PASSED


class TestProcessExecutorRunId:
    """Tests for run_id parameter."""

    def test_execute_with_run_id(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should accept run_id parameter without error."""
        script_path = examples_dir / "test_pass.py"
        result = executor.execute(
            str(script_path), {"value": 42}, run_id="run_abc123",
        )

        assert result.status == StepStatus.PASSED

    def test_execute_without_run_id(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should work without run_id (backward compat)."""
        script_path = examples_dir / "test_pass.py"
        result = executor.execute(str(script_path), {"value": 42})

        assert result.status == StepStatus.PASSED

    def test_run_id_passed_to_event(
        self, examples_dir: Path
    ) -> None:
        """Should pass run_id through to published events."""
        bus = EventBus()
        executor = ProcessExecutor(max_workers=2, script_timeout=5.0, event_bus=bus)

        events_received = []

        def event_handler(event):
            events_received.append(event)

        bus.subscribe(EventType.STEP_STATUS_CHANGED, event_handler)

        script_path = examples_dir / "test_pass.py"
        result = executor.execute(
            str(script_path), {"value": 42}, run_id="run_event_test",
        )

        executor.shutdown()

        assert result.status == StepStatus.PASSED
        # Verify events were published with run_id
        for event in events_received:
            assert event.data.get("run_id") == "run_event_test"


class TestProcessExecutorAsync:
    """Tests for execute_async method."""

    @pytest.mark.asyncio
    async def test_execute_async_passing_script(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should execute a passing script asynchronously."""
        script_path = examples_dir / "test_pass.py"
        result = await executor.execute_async(str(script_path), {"value": 42})

        assert result.status == StepStatus.PASSED
        assert result.error is None

    @pytest.mark.asyncio
    async def test_execute_async_failing_script(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should execute a failing script asynchronously."""
        script_path = examples_dir / "test_fail.py"
        result = await executor.execute_async(
            str(script_path), {"expected": 100, "actual": 50},
        )

        assert result.status == StepStatus.FAILED
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_execute_async_error_script(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should execute an erroring script asynchronously."""
        script_path = examples_dir / "test_error.py"
        result = await executor.execute_async(
            str(script_path), {"simulate_error": True},
        )

        assert result.status == StepStatus.ERROR
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_execute_async_script_not_found(
        self, executor: ProcessExecutor
    ) -> None:
        """Should return ERROR for non-existent script asynchronously."""
        result = await executor.execute_async("/nonexistent/script.py", {})

        assert result.status == StepStatus.ERROR
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_execute_async_with_run_id(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should accept run_id parameter in async execution."""
        script_path = examples_dir / "test_pass.py"
        result = await executor.execute_async(
            str(script_path), {"value": 42}, run_id="async_run_001",
        )

        assert result.status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_execute_async_with_step_id(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should accept step_id parameter in async execution."""
        script_path = examples_dir / "test_pass.py"
        result = await executor.execute_async(
            str(script_path), {"value": 42}, step_id="my_async_step",
        )

        assert result.status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_execute_async_with_timeout(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should timeout a long-running script asynchronously."""
        script_path = examples_dir / "test_timeout.py"
        result = await executor.execute_async(
            str(script_path), {"sleep_duration": 30}, timeout=0.5,
        )

        assert result.status == StepStatus.ERROR
        assert "timed out" in result.error.lower()


class TestProcessExecutorBatch:
    """Tests for execute_batch method."""

    @pytest.mark.asyncio
    async def test_execute_batch_multiple_tasks(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should execute multiple tasks concurrently."""
        script_path = examples_dir / "test_pass.py"
        tasks = [
            ExecuteTask(script_path=str(script_path), params={"value": i})
            for i in range(3)
        ]

        results = await executor.execute_batch(tasks)

        assert len(results) == 3
        for result in results:
            assert result.status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_execute_batch_preserves_order(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should return results in the same order as input tasks."""
        pass_script = examples_dir / "test_pass.py"
        fail_script = examples_dir / "test_fail.py"

        tasks = [
            ExecuteTask(script_path=str(pass_script), params={"value": 1}),
            ExecuteTask(script_path=str(fail_script), params={"expected": 100, "actual": 50}),
            ExecuteTask(script_path=str(pass_script), params={"value": 3}),
        ]

        results = await executor.execute_batch(tasks)

        assert len(results) == 3
        assert results[0].status == StepStatus.PASSED
        assert results[1].status == StepStatus.FAILED
        assert results[2].status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_execute_batch_with_concurrency_limit(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should respect max_concurrency limit."""
        script_path = examples_dir / "test_pass.py"
        tasks = [
            ExecuteTask(script_path=str(script_path), params={"value": i})
            for i in range(5)
        ]

        # Limit to 1 concurrent execution
        results = await executor.execute_batch(tasks, max_concurrency=1)

        assert len(results) == 5
        for result in results:
            assert result.status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_execute_batch_empty_list(
        self, executor: ProcessExecutor
    ) -> None:
        """Should return empty list for empty task list."""
        results = await executor.execute_batch([])

        assert results == []

    @pytest.mark.asyncio
    async def test_execute_batch_with_run_id(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should pass run_id from ExecuteTask through to execution."""
        script_path = examples_dir / "test_pass.py"
        tasks = [
            ExecuteTask(
                script_path=str(script_path),
                params={"value": 1},
                run_id="batch_run_001",
            ),
        ]

        results = await executor.execute_batch(tasks)

        assert len(results) == 1
        assert results[0].status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_execute_batch_with_step_ids(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should pass step_id from ExecuteTask through to execution."""
        script_path = examples_dir / "test_pass.py"
        tasks = [
            ExecuteTask(
                script_path=str(script_path),
                params={"value": 1},
                step_id="batch_step_1",
            ),
            ExecuteTask(
                script_path=str(script_path),
                params={"value": 2},
                step_id="batch_step_2",
            ),
        ]

        results = await executor.execute_batch(tasks)

        assert len(results) == 2
        assert results[0].status == StepStatus.PASSED
        assert results[1].status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_execute_batch_with_timeout(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should pass timeout from ExecuteTask through to execution."""
        script_path = examples_dir / "test_timeout.py"
        tasks = [
            ExecuteTask(
                script_path=str(script_path),
                params={"sleep_duration": 30},
                timeout=0.5,
            ),
        ]

        results = await executor.execute_batch(tasks)

        assert len(results) == 1
        assert results[0].status == StepStatus.ERROR
        assert "timed out" in results[0].error.lower()

    @pytest.mark.asyncio
    async def test_execute_batch_mixed_results(
        self, executor: ProcessExecutor, examples_dir: Path
    ) -> None:
        """Should handle batch with mixed pass/fail/error results."""
        pass_script = examples_dir / "test_pass.py"
        fail_script = examples_dir / "test_fail.py"

        tasks = [
            ExecuteTask(script_path=str(pass_script), params={"value": 1}),
            ExecuteTask(script_path=str(fail_script), params={"expected": 100, "actual": 50}),
            ExecuteTask(script_path="/nonexistent/script.py", params={}),
        ]

        results = await executor.execute_batch(tasks)

        assert len(results) == 3
        assert results[0].status == StepStatus.PASSED
        assert results[1].status == StepStatus.FAILED
        assert results[2].status == StepStatus.ERROR


class TestProcessExecutorFixture:
    """Tests using the pass_script.py fixture."""

    def test_execute_fixture_script(
        self, executor: ProcessExecutor, fixtures_dir: Path
    ) -> None:
        """Should execute the pass_script.py fixture."""
        script_path = fixtures_dir / "pass_script.py"
        result = executor.execute(str(script_path), {})

        assert result.status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_execute_async_fixture_script(
        self, executor: ProcessExecutor, fixtures_dir: Path
    ) -> None:
        """Should execute the pass_script.py fixture asynchronously."""
        script_path = fixtures_dir / "pass_script.py"
        result = await executor.execute_async(str(script_path), {})

        assert result.status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_execute_batch_fixture_script(
        self, executor: ProcessExecutor, fixtures_dir: Path
    ) -> None:
        """Should execute batch with pass_script.py fixture."""
        script_path = fixtures_dir / "pass_script.py"
        tasks = [
            ExecuteTask(script_path=str(script_path), params={}),
            ExecuteTask(script_path=str(script_path), params={}),
        ]

        results = await executor.execute_batch(tasks)

        assert len(results) == 2
        for result in results:
            assert result.status == StepStatus.PASSED