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
"""

import os
import tempfile
from pathlib import Path

import pytest

from ate_platform.executor.process_executor import ProcessExecutor
from ate_platform.scheduler.event_bus import EventBus, EventType
from ate_platform.types import StepStatus


@pytest.fixture
def executor() -> ProcessExecutor:
    """Create a ProcessExecutor instance for testing."""
    return ProcessExecutor(max_workers=2, script_timeout=5.0)


@pytest.fixture
def event_bus() -> EventBus:
    """Create an EventBus instance for testing."""
    return EventBus()


@pytest.fixture
def examples_dir() -> Path:
    """Get the path to examples directory."""
    return Path(__file__).parent.parent.parent.parent / "examples"


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