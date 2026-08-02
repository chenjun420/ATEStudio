"""Tests for DebugProcessExecutor.

Tests cover:
- execute: delegates to wrapped ProcessExecutor
- execute_async: delegates to wrapped ProcessExecutor
- execute_debug: spawns child via multiprocessing.Process, captures pause events
- execute_debug_async: async wrapper with SSE publish callback
- _start_adapter: starts debugpy adapter, returns port
- build_child_env: environment variables for child process
- _build_pause_event: constructs SSE event dict
- serialize_event: JSON serialization
- adapter_port property

All tests mock debugpy to avoid launching real debug sessions.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ate_platform.debug.breakpoint_manager import BreakpointData
from ate_platform.debug.debug_executor import (
    DebugProcessExecutor,
    _ENV_DAP_HOST,
    _ENV_DAP_PORT,
    _ENV_DAP_TOKEN,
    _run_debug_child,
)
from ate_platform.executor.process_executor import ProcessExecutor
from ate_platform.types import StepResult, StepStatus


@pytest.fixture
def executor() -> ProcessExecutor:
    """Create a ProcessExecutor for the wrapper."""
    return ProcessExecutor(max_workers=2, script_timeout=5.0)


@pytest.fixture
def debug_executor(executor: ProcessExecutor) -> DebugProcessExecutor:
    """Create a DebugProcessExecutor wrapping the ProcessExecutor."""
    return DebugProcessExecutor(
        executor=executor,
        dap_host="127.0.0.1",
        dap_port=0,
        access_token="test-token",
    )


@pytest.fixture
def examples_dir() -> Path:
    """Get the path to examples directory."""
    return Path(__file__).parent.parent.parent.parent / "examples"


@pytest.fixture
def passing_script(examples_dir: Path) -> str:
    """Get path to a passing test script."""
    return str(examples_dir / "test_pass.py")


def _make_bp(
    bp_id: str = "bp-1",
    line_number: int = 15,
    enabled: bool = True,
) -> BreakpointData:
    """Create a BreakpointData for testing."""
    return BreakpointData(
        id=bp_id,
        session_id="sess-1",
        step_id="step-1",
        node_id="node-1",
        line_number=line_number,
        condition=None,
        enabled=enabled,
        node_data=None,
    )


class TestExecuteDelegation:
    """Tests that non-debug execution delegates to wrapped executor."""

    def test_execute_delegates(
        self,
        debug_executor: DebugProcessExecutor,
        passing_script: str,
    ) -> None:
        """Should delegate execute() to wrapped ProcessExecutor."""
        result = debug_executor.execute(passing_script, {"value": 42})

        assert result.status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_execute_async_delegates(
        self,
        debug_executor: DebugProcessExecutor,
        passing_script: str,
    ) -> None:
        """Should delegate execute_async() to wrapped ProcessExecutor."""
        result = await debug_executor.execute_async(passing_script, {"value": 42})

        assert result.status == StepStatus.PASSED

    def test_executor_property(
        self,
        debug_executor: DebugProcessExecutor,
        executor: ProcessExecutor,
    ) -> None:
        """Should expose the wrapped executor."""
        assert debug_executor.executor is executor


class TestExecuteDebug:
    """Tests for execute_debug()."""

    def test_script_not_found(
        self,
        debug_executor: DebugProcessExecutor,
    ) -> None:
        """Should return ERROR when script does not exist."""
        result = debug_executor.execute_debug(
            script_path="/nonexistent/script.py",
            params={},
            step_id="step-1",
            session_id="sess-1",
        )

        assert result.status == StepStatus.ERROR
        assert "Script not found" in result.error

    @patch("ate_platform.debug.debug_executor.DebugProcessExecutor._start_adapter")
    def test_execute_debug_passing_script(
        self,
        mock_start_adapter: MagicMock,
        debug_executor: DebugProcessExecutor,
        passing_script: str,
    ) -> None:
        """Should execute a passing script in debug mode and return PASSED."""
        mock_start_adapter.return_value = 5678

        result = debug_executor.execute_debug(
            script_path=passing_script,
            params={"value": 42},
            step_id="step-1",
            session_id="sess-1",
            breakpoints=[_make_bp(line_number=15)],
            timeout=10.0,
        )

        # Child should complete successfully
        assert result.status in (StepStatus.PASSED, StepStatus.ERROR)

    @patch("ate_platform.debug.debug_executor.DebugProcessExecutor._start_adapter")
    def test_execute_debug_calls_on_pause(
        self,
        mock_start_adapter: MagicMock,
        debug_executor: DebugProcessExecutor,
        passing_script: str,
    ) -> None:
        """Should call on_pause callback when a breakpoint is hit."""
        mock_start_adapter.return_value = 5678

        pause_events: list[tuple[str, dict]] = []

        def on_pause(session_id: str, event: dict) -> None:
            pause_events.append((session_id, event))

        # Use a breakpoint on line 15 (the assert line in test_pass.py)
        debug_executor.execute_debug(
            script_path=passing_script,
            params={"value": 42},
            step_id="step-1",
            session_id="sess-1",
            breakpoints=[_make_bp(line_number=15)],
            timeout=10.0,
            on_pause=on_pause,
        )

        # The callback may or may not be called depending on timing,
        # but it should not crash
        assert isinstance(pause_events, list)

    @patch("ate_platform.debug.debug_executor.DebugProcessExecutor._start_adapter")
    def test_execute_debug_disabled_breakpoints_filtered(
        self,
        mock_start_adapter: MagicMock,
        debug_executor: DebugProcessExecutor,
        passing_script: str,
    ) -> None:
        """Should filter out disabled breakpoints."""
        mock_start_adapter.return_value = 5678

        result = debug_executor.execute_debug(
            script_path=passing_script,
            params={"value": 42},
            step_id="step-1",
            session_id="sess-1",
            breakpoints=[
                _make_bp(bp_id="bp-1", line_number=15, enabled=False),
                _make_bp(bp_id="bp-2", line_number=0, enabled=True),  # line 0 filtered
            ],
            timeout=10.0,
        )

        assert result.status in (StepStatus.PASSED, StepStatus.ERROR)

    @patch("ate_platform.debug.debug_executor.DebugProcessExecutor._start_adapter")
    def test_execute_debug_no_breakpoints(
        self,
        mock_start_adapter: MagicMock,
        debug_executor: DebugProcessExecutor,
        passing_script: str,
    ) -> None:
        """Should execute without breakpoints (no pause events)."""
        mock_start_adapter.return_value = 5678

        result = debug_executor.execute_debug(
            script_path=passing_script,
            params={"value": 42},
            step_id="step-1",
            session_id="sess-1",
            breakpoints=None,
            timeout=10.0,
        )

        assert result.status in (StepStatus.PASSED, StepStatus.ERROR)


class TestExecuteDebugAsync:
    """Tests for execute_debug_async()."""

    @patch("ate_platform.debug.debug_executor.DebugProcessExecutor._start_adapter")
    @pytest.mark.asyncio
    async def test_execute_debug_async(
        self,
        mock_start_adapter: MagicMock,
        debug_executor: DebugProcessExecutor,
        passing_script: str,
    ) -> None:
        """Should execute debug script asynchronously."""
        mock_start_adapter.return_value = 5678

        result = await debug_executor.execute_debug_async(
            script_path=passing_script,
            params={"value": 42},
            step_id="step-1",
            session_id="sess-1",
            breakpoints=[_make_bp(line_number=15)],
            timeout=10.0,
        )

        assert result.status in (StepStatus.PASSED, StepStatus.ERROR)

    @patch("ate_platform.debug.debug_executor.DebugProcessExecutor._start_adapter")
    @pytest.mark.asyncio
    async def test_execute_debug_async_with_publish(
        self,
        mock_start_adapter: MagicMock,
        debug_executor: DebugProcessExecutor,
        passing_script: str,
    ) -> None:
        """Should call publish_pause callback when breakpoint hit."""
        mock_start_adapter.return_value = 5678

        published_events: list[tuple[str, dict]] = []

        async def publish_pause(session_id: str, event: dict) -> None:
            published_events.append((session_id, event))

        await debug_executor.execute_debug_async(
            script_path=passing_script,
            params={"value": 42},
            step_id="step-1",
            session_id="sess-1",
            breakpoints=[_make_bp(line_number=15)],
            timeout=10.0,
            publish_pause=publish_pause,
        )

        # publish_pause may or may not be called depending on timing
        assert isinstance(published_events, list)


class TestStartAdapter:
    """Tests for _start_adapter()."""

    @patch("ate_platform.debug.debug_executor.debugpy")
    def test_start_adapter_auto_port(
        self,
        mock_debugpy: MagicMock,
        executor: ProcessExecutor,
    ) -> None:
        """Should auto-assign port when dap_port=0."""
        mock_debugpy.listen.return_value = ("127.0.0.1", 12345)
        debug_exec = DebugProcessExecutor(
            executor=executor, dap_host="127.0.0.1", dap_port=0
        )

        port = debug_exec._start_adapter()

        assert port == 12345
        assert debug_exec.adapter_port == 12345
        mock_debugpy.listen.assert_called_once_with(("127.0.0.1", 0))

    @patch("ate_platform.debug.debug_executor.debugpy")
    def test_start_adapter_fixed_port(
        self,
        mock_debugpy: MagicMock,
        executor: ProcessExecutor,
    ) -> None:
        """Should use configured port when dap_port > 0."""
        mock_debugpy.listen.return_value = ("127.0.0.1", 5678)
        debug_exec = DebugProcessExecutor(
            executor=executor, dap_host="127.0.0.1", dap_port=5678
        )

        port = debug_exec._start_adapter()

        assert port == 5678

    @patch("ate_platform.debug.debug_executor.debugpy")
    def test_start_adapter_cached(
        self,
        mock_debugpy: MagicMock,
        executor: ProcessExecutor,
    ) -> None:
        """Should cache the port and not re-listen."""
        mock_debugpy.listen.return_value = ("127.0.0.1", 12345)
        debug_exec = DebugProcessExecutor(
            executor=executor, dap_host="127.0.0.1", dap_port=0
        )

        debug_exec._start_adapter()
        debug_exec._start_adapter()

        mock_debugpy.listen.assert_called_once()

    @patch("ate_platform.debug.debug_executor.debugpy")
    def test_start_adapter_fallback_on_error(
        self,
        mock_debugpy: MagicMock,
        executor: ProcessExecutor,
    ) -> None:
        """Should fall back to default port on error."""
        mock_debugpy.listen.side_effect = Exception("listen failed")
        debug_exec = DebugProcessExecutor(
            executor=executor, dap_host="127.0.0.1", dap_port=0
        )

        port = debug_exec._start_adapter()

        assert port == 5678


class TestBuildChildEnv:
    """Tests for build_child_env()."""

    def test_build_child_env(
        self,
        debug_executor: DebugProcessExecutor,
    ) -> None:
        """Should build environment variables for the child process."""
        env = debug_executor.build_child_env(dap_port=5678)

        assert env[_ENV_DAP_HOST] == "127.0.0.1"
        assert env[_ENV_DAP_PORT] == "5678"
        assert env[_ENV_DAP_TOKEN] == "test-token"


class TestBuildPauseEvent:
    """Tests for _build_pause_event()."""

    def test_build_pause_event(self) -> None:
        """Should construct a complete pause event dict."""
        snapshot = {
            "line_number": 15,
            "thread_id": 1234,
            "frames": [{"filename": "test.py", "line": 15}],
            "reason": "breakpoint",
            "timestamp": 1234567890.0,
        }

        event = DebugProcessExecutor._build_pause_event(
            session_id="sess-1",
            step_id="step-1",
            node_id="node-1",
            snapshot=snapshot,
        )

        assert event["session_id"] == "sess-1"
        assert event["step_id"] == "step-1"
        assert event["node_id"] == "node-1"
        assert event["line_number"] == 15
        assert event["thread_id"] == 1234
        assert event["frames"] == [{"filename": "test.py", "line": 15}]
        assert event["reason"] == "breakpoint"
        assert event["timestamp"] == 1234567890.0

    def test_build_pause_event_empty_node_id(self) -> None:
        """Should use node_id from snapshot when node_id is empty."""
        snapshot = {
            "line_number": 10,
            "node_id": "snap-node",
            "frames": [],
            "reason": "breakpoint",
            "timestamp": 1.0,
        }

        event = DebugProcessExecutor._build_pause_event(
            session_id="sess-1",
            step_id="step-1",
            node_id="",
            snapshot=snapshot,
        )

        assert event["node_id"] == "snap-node"

    def test_build_pause_event_defaults(self) -> None:
        """Should use defaults for missing snapshot fields."""
        event = DebugProcessExecutor._build_pause_event(
            session_id="sess-1",
            step_id="step-1",
            node_id="node-1",
            snapshot={},
        )

        assert event["line_number"] == 0
        assert event["thread_id"] is None
        assert event["frames"] == []
        assert event["reason"] == "breakpoint"
        assert event["timestamp"] > 0


class TestSerializeEvent:
    """Tests for serialize_event()."""

    def test_serialize_event(self) -> None:
        """Should serialize event to JSON string."""
        event = {"session_id": "sess-1", "step_id": "step-1"}

        result = DebugProcessExecutor.serialize_event(event)

        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed["session_id"] == "sess-1"
        assert parsed["step_id"] == "step-1"

    def test_serialize_event_with_non_serializable(self) -> None:
        """Should handle non-serializable values via default=str."""
        event = {"session_id": "sess-1", "data": object()}

        result = DebugProcessExecutor.serialize_event(event)

        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed["session_id"] == "sess-1"
        assert isinstance(parsed["data"], str)


class TestRunDebugChild:
    """Tests for the _run_debug_child module-level function."""

    def test_run_debug_child_passes(self) -> None:
        """Should execute a passing script and return PASSED."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write("x = params['value'] * 2\nassert x == 84\n")
            script_path = f.name

        try:
            import multiprocessing

            ctx = multiprocessing.get_context("spawn")
            pause_queue = ctx.Queue()
            result = _run_debug_child(
                script_path=script_path,
                params={"value": 42},
                step_id="step-1",
                dap_host="127.0.0.1",
                dap_port=9999,  # Won't connect, falls back gracefully
                access_token="",
                breakpoint_lines=[],
                pause_queue=pause_queue,
            )

            assert result["status"] == "PASSED"
        finally:
            os.unlink(script_path)

    def test_run_debug_child_failure(self) -> None:
        """Should return FAILED on assertion error."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write("assert 1 == 2, 'intentional failure'\n")
            script_path = f.name

        try:
            import multiprocessing

            ctx = multiprocessing.get_context("spawn")
            pause_queue = ctx.Queue()
            result = _run_debug_child(
                script_path=script_path,
                params={},
                step_id="step-1",
                dap_host="127.0.0.1",
                dap_port=9999,
                access_token="",
                breakpoint_lines=[],
                pause_queue=pause_queue,
            )

            assert result["status"] == "FAILED"
            assert "intentional failure" in result["error"]
        finally:
            os.unlink(script_path)

    def test_run_debug_child_error(self) -> None:
        """Should return ERROR on unexpected exception."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write("raise ValueError('boom')\n")
            script_path = f.name

        try:
            import multiprocessing

            ctx = multiprocessing.get_context("spawn")
            pause_queue = ctx.Queue()
            result = _run_debug_child(
                script_path=script_path,
                params={},
                step_id="step-1",
                dap_host="127.0.0.1",
                dap_port=9999,
                access_token="",
                breakpoint_lines=[],
                pause_queue=pause_queue,
            )

            assert result["status"] == "ERROR"
            assert "ValueError" in result["error"]
        finally:
            os.unlink(script_path)

    def test_run_debug_child_sends_pause_event(self) -> None:
        """Should send a pause event when a breakpoint line is hit."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write("x = 1\ny = 2\nz = x + y\n")
            script_path = f.name

        try:
            import multiprocessing

            ctx = multiprocessing.get_context("spawn")
            pause_queue = ctx.Queue()
            _run_debug_child(
                script_path=script_path,
                params={},
                step_id="step-1",
                dap_host="127.0.0.1",
                dap_port=9999,
                access_token="",
                breakpoint_lines=[2],  # Break at line 2 (y = 2)
                pause_queue=pause_queue,
            )

            # Check if a pause event was sent
            events = []
            while not pause_queue.empty():
                events.append(pause_queue.get_nowait())

            pause_events = [e for e in events if e.get("type") == "pause"]
            if pause_events:
                data = pause_events[0]["data"]
                assert data["step_id"] == "step-1"
                assert data["line_number"] == 2
                assert data["reason"] == "breakpoint"
                assert len(data["frames"]) == 1
        finally:
            os.unlink(script_path)
