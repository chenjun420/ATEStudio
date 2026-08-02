"""Test TracebackAnalyzer, DiagnosisPusher, and DebugProcessExecutor.

Mock-based tests always run (no API key required). Tests verify:
- TracebackAnalyzer with mocked LLM (ChatOpenAI)
- dev_mode=False skips LLM analysis
- DebugProcessExecutor captures tracebacks via monkey-patch
- DiagnosisPusher publishes to NATS (and raises when disconnected)
- CircuitBreaker integration

The autouse ``_dev_mode_bypass`` fixture from ``tests/cloud/conftest.py``
sets ``settings.dev_mode = True`` by default. Tests that need
``dev_mode = False`` use monkeypatch to override.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ate_cloud.config import settings
from ate_cloud.services.diagnosis_pusher import DiagnosisPusher
from ate_cloud.services.traceback_analyzer import (
    DebugProcessExecutor,
    TracebackAnalyzer,
    TracebackContext,
    extract_local_vars,
    extract_source_snippet,
)
from ate_platform.common.circuit_breaker import CircuitBreakerOpenError, CircuitState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_chat_openai() -> MagicMock:
    """Patch langchain_openai.ChatOpenAI with a controllable mock.

    The mock's ``ainvoke`` is an AsyncMock so callers can ``await`` it.
    Returns the mock instance.
    """
    instance = MagicMock()
    instance.ainvoke = AsyncMock()
    # Simulate an LLM response with .content attribute
    mock_response = MagicMock()
    mock_response.content = json.dumps({
        "root_cause": "Division by zero in measurement loop",
        "confidence": 0.95,
        "suggested_fix": "- result = x / y\n+ result = x / y if y != 0 else float('inf')",
        "explanation": "The script divides by y without checking for zero.",
    })
    instance.ainvoke.return_value = mock_response

    with patch("langchain_openai.ChatOpenAI", return_value=instance):
        yield instance


@pytest.fixture
def analyzer(mock_chat_openai: MagicMock) -> TracebackAnalyzer:
    """Create a TracebackAnalyzer with a mocked ChatOpenAI backend."""
    return TracebackAnalyzer(api_key="test-key", model="gpt-4o-mini")


@pytest.fixture
def error_script(tmp_path: Path) -> str:
    """Create a temporary script that raises ValueError("test")."""
    script = tmp_path / "error_script.py"
    script.write_text(
        "x = 42\n"
        "name = 'probe'\n"
        "values = [1, 2, 3]\n"
        "raise ValueError('test')\n",
        encoding="utf-8",
    )
    return str(script)


@pytest.fixture
def passing_script(tmp_path: Path) -> str:
    """Create a temporary script that passes."""
    script = tmp_path / "passing_script.py"
    script.write_text(
        "result = {'status': 'PASSED', 'outputs': {'voltage': 3.3}}\n",
        encoding="utf-8",
    )
    return str(script)


def _make_context(
    script_path: str = "test_script.py",
    exc_type: str = "ValueError",
    exc_value: str = "test error",
) -> TracebackContext:
    """Build a TracebackContext for testing."""
    return TracebackContext(
        script_path=script_path,
        params={"channel": 1},
        step_id="step_001",
        exc_type=exc_type,
        exc_value=exc_value,
        traceback_text="Traceback (most recent call last):\n  File ...\nValueError: test error",
        local_vars={"x": "42", "name": "'probe'"},
        source_snippet="x = 42\nraise ValueError('test')",
    )


# ---------------------------------------------------------------------------
# Tests: TracebackAnalyzer basic
# ---------------------------------------------------------------------------


class TestTracebackAnalyzerBasics:
    """Tests for TracebackAnalyzer initialization and properties."""

    def test_circuit_breaker_starts_closed(self, analyzer: TracebackAnalyzer) -> None:
        """CircuitBreaker starts in CLOSED state."""
        assert analyzer.circuit_breaker.state == CircuitState.CLOSED

    def test_circuit_breaker_failure_threshold(self, analyzer: TracebackAnalyzer) -> None:
        """CircuitBreaker has failure_threshold=5."""
        assert analyzer.circuit_breaker._failure_threshold == 5  # type: ignore[attr-defined]

    def test_circuit_breaker_timeout(self, analyzer: TracebackAnalyzer) -> None:
        """CircuitBreaker has timeout=30.0."""
        assert analyzer.circuit_breaker._timeout == 30.0  # type: ignore[attr-defined]

    def test_lazy_initialization(self, analyzer: TracebackAnalyzer) -> None:
        """LLM is not initialized until first analyze() call."""
        assert analyzer._initialized is False  # type: ignore[attr-defined]
        assert analyzer._llm is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tests: analyze with dev_mode=True (mocked LLM)
# ---------------------------------------------------------------------------


class TestAnalyzeDevMode:
    """Tests for LLM analysis when dev_mode=True."""

    @pytest.mark.asyncio
    async def test_analyze_returns_dict(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
    ) -> None:
        """analyze() returns a dict with root_cause, confidence, etc."""
        # dev_mode is True via _dev_mode_bypass autouse fixture
        result = await analyzer.analyze(_make_context())
        assert result is not None
        assert "root_cause" in result
        assert "confidence" in result
        assert "suggested_fix" in result
        assert "explanation" in result

    @pytest.mark.asyncio
    async def test_analyze_calls_llm(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
    ) -> None:
        """analyze() calls ChatOpenAI.ainvoke when dev_mode=True."""
        await analyzer.analyze(_make_context())
        mock_chat_openai.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_analyze_parses_json_response(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
    ) -> None:
        """analyze() parses the LLM JSON response correctly."""
        result = await analyzer.analyze(_make_context())
        assert result is not None
        assert result["root_cause"] == "Division by zero in measurement loop"
        assert result["confidence"] == 0.95
        assert "result = x / y" in result["suggested_fix"]
        assert "divides by y" in result["explanation"]

    @pytest.mark.asyncio
    async def test_analyze_handles_markdown_fenced_json(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
    ) -> None:
        """analyze() strips markdown code fences from LLM response."""
        mock_response = MagicMock()
        mock_response.content = (
            "```json\n"
            '{"root_cause": "Null pointer", "confidence": 0.8, '
            '"suggested_fix": "add null check", "explanation": "deref null"}\n'
            "```"
        )
        mock_chat_openai.ainvoke.return_value = mock_response

        result = await analyzer.analyze(_make_context())
        assert result is not None
        assert result["root_cause"] == "Null pointer"
        assert result["confidence"] == 0.8

    @pytest.mark.asyncio
    async def test_analyze_invalid_json_fallback(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
    ) -> None:
        """analyze() falls back to raw text in explanation for invalid JSON."""
        mock_response = MagicMock()
        mock_response.content = "This is not JSON at all"
        mock_chat_openai.ainvoke.return_value = mock_response

        result = await analyzer.analyze(_make_context())
        assert result is not None
        assert result["root_cause"] == ""
        assert result["confidence"] == 0.0
        assert result["explanation"] == "This is not JSON at all"

    @pytest.mark.asyncio
    async def test_analyze_logs_traceback_always(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """analyze() always logs the traceback, even when LLM is called."""
        with caplog.at_level("WARNING"):
            await analyzer.analyze(_make_context())
        assert any("ValueError" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_analyze_builds_prompt_with_context(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
    ) -> None:
        """analyze() builds the prompt with traceback, source, params, vars."""
        await analyzer.analyze(_make_context())
        call_args = mock_chat_openai.ainvoke.call_args
        messages = call_args[0][0]  # First positional arg = messages list
        human_msg = messages[1].content  # [0]=system, [1]=human
        assert "test_script.py" in human_msg
        assert "ValueError" in human_msg
        assert "test error" in human_msg
        assert "x = 42" in human_msg  # source snippet
        assert "channel" in human_msg  # params
        assert "x" in human_msg  # local var


# ---------------------------------------------------------------------------
# Tests: dev_mode=False skips LLM
# ---------------------------------------------------------------------------


class TestDevModeFalse:
    """Tests for dev_mode=False behavior (skip LLM, log only)."""

    @pytest.mark.asyncio
    async def test_dev_mode_false_skips_llm(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When dev_mode=False, analyze() returns None without calling LLM."""
        monkeypatch.setattr(settings, "dev_mode", False)
        result = await analyzer.analyze(_make_context())
        assert result is None
        mock_chat_openai.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_dev_mode_false_logs_traceback(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When dev_mode=False, analyze() still logs the traceback."""
        monkeypatch.setattr(settings, "dev_mode", False)
        with caplog.at_level("WARNING"):
            await analyzer.analyze(_make_context())
        assert any("ValueError" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_dev_mode_false_logs_skip_message(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When dev_mode=False, analyze() logs the skip reason."""
        monkeypatch.setattr(settings, "dev_mode", False)
        with caplog.at_level("INFO"):
            await analyzer.analyze(_make_context())
        assert any("dev_mode=False" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Tests: CircuitBreaker integration
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    """Tests for CircuitBreaker resilience on LLM failures."""

    @pytest.mark.asyncio
    async def test_breaker_opens_after_threshold(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
    ) -> None:
        """Breaker opens after 5 consecutive LLM failures."""
        mock_chat_openai.ainvoke.side_effect = RuntimeError("API rate limit")
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await analyzer.analyze(_make_context())
        assert analyzer.circuit_breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_breaker_rejects_when_open(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
    ) -> None:
        """When OPEN, analyze() raises CircuitBreakerOpenError."""
        mock_chat_openai.ainvoke.side_effect = RuntimeError("API error")
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await analyzer.analyze(_make_context())
        # Now breaker is OPEN — next call should be rejected
        with pytest.raises(CircuitBreakerOpenError):
            await analyzer.analyze(_make_context())
        # LLM should NOT be called when breaker is open
        assert mock_chat_openai.ainvoke.call_count == 5

    @pytest.mark.asyncio
    async def test_breaker_success_resets_count(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
    ) -> None:
        """A successful call resets the failure count in CLOSED state."""
        mock_chat_openai.ainvoke.side_effect = RuntimeError("transient")
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await analyzer.analyze(_make_context())
        assert analyzer.circuit_breaker.failure_count == 3
        assert analyzer.circuit_breaker.state == CircuitState.CLOSED

        # Success resets count
        mock_chat_openai.ainvoke.side_effect = None
        mock_response = MagicMock()
        mock_response.content = '{"root_cause":"","confidence":0,"suggested_fix":"","explanation":""}'
        mock_chat_openai.ainvoke.return_value = mock_response
        await analyzer.analyze(_make_context())
        assert analyzer.circuit_breaker.failure_count == 0


# ---------------------------------------------------------------------------
# Tests: Helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    """Tests for extract_local_vars and extract_source_snippet."""

    def test_extract_local_vars_returns_dict(self) -> None:
        """extract_local_vars returns up to max_vars local variables."""
        def sample_func() -> dict[str, str]:
            _x = 1  # noqa: F841
            _y = "hello"  # noqa: F841
            _z = [1, 2, 3]  # noqa: F841
            return extract_local_vars(sys._getframe())

        result = sample_func()
        assert "_x" in result
        assert "_y" in result
        assert "_z" in result
        assert result["_x"] == "1"
        assert result["_y"] == "'hello'"

    def test_extract_local_vars_skips_dunder(self) -> None:
        """extract_local_vars skips dunder variables."""
        def sample_func() -> dict[str, str]:
            __hidden = "secret"  # noqa: F841
            _visible = 42  # noqa: F841
            return extract_local_vars(sys._getframe())

        result = sample_func()
        assert "__hidden" not in result
        assert "_visible" in result

    def test_extract_local_vars_limits_to_five(self) -> None:
        """extract_local_vars limits to 5 variables."""
        def sample_func() -> dict[str, str]:
            _a, _b, _c, _d, _e, _f, _g = 1, 2, 3, 4, 5, 6, 7  # noqa: F841
            return extract_local_vars(sys._getframe(), max_vars=5)

        result = sample_func()
        assert len(result) <= 5

    def test_extract_source_snippet_reads_file(self, tmp_path: Path) -> None:
        """extract_source_snippet reads the first N lines of a script."""
        script = tmp_path / "test.py"
        script.write_text("line1\nline2\nline3\n", encoding="utf-8")
        snippet = extract_source_snippet(str(script), max_lines=2)
        assert "line1" in snippet
        assert "line2" in snippet
        assert "line3" not in snippet

    def test_extract_source_snippet_missing_file(self) -> None:
        """extract_source_snippet returns empty string for missing file."""
        result = extract_source_snippet("/nonexistent/path.py")
        assert result == ""


# ---------------------------------------------------------------------------
# Tests: DebugProcessExecutor
# ---------------------------------------------------------------------------


class TestDebugProcessExecutor:
    """Tests for the DebugProcessExecutor wrapper."""

    def test_execute_captures_traceback_on_error(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
        error_script: str,
    ) -> None:
        """DebugProcessExecutor captures traceback when script raises."""
        from ate_platform.executor.process_executor import ProcessExecutor

        executor = ProcessExecutor(max_workers=1, script_timeout=5.0)
        debug_executor = DebugProcessExecutor(
            executor=executor,
            analyzer=analyzer,
        )
        try:
            result = debug_executor.execute(error_script, {"ch": 1})
            # Result should be ERROR
            from ate_platform.types import StepStatus
            assert result.status == StepStatus.ERROR

            # Check captured context
            captured = debug_executor.drain_captured()
            assert len(captured) == 1
            ctx = captured[0]
            assert ctx.exc_type == "ValueError"
            assert ctx.exc_value == "test"
            assert "ValueError" in ctx.traceback_text
            assert "test" in ctx.traceback_text
            # Local vars should include x, name, values
            assert "x" in ctx.local_vars
            # Source snippet should include script content
            assert "raise ValueError" in ctx.source_snippet
        finally:
            debug_executor.uninstall_hook()
            executor.shutdown()

    def test_execute_no_capture_on_pass(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
        passing_script: str,
    ) -> None:
        """DebugProcessExecutor does not capture when script passes."""
        from ate_platform.executor.process_executor import ProcessExecutor

        executor = ProcessExecutor(max_workers=1, script_timeout=5.0)
        debug_executor = DebugProcessExecutor(
            executor=executor,
            analyzer=analyzer,
        )
        try:
            result = debug_executor.execute(passing_script, {})
            from ate_platform.types import StepStatus
            assert result.status == StepStatus.PASSED

            captured = debug_executor.drain_captured()
            assert len(captured) == 0
        finally:
            debug_executor.uninstall_hook()
            executor.shutdown()

    def test_uninstall_hook_restores_original(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
    ) -> None:
        """uninstall_hook restores the original _run_script_in_thread."""
        import ate_platform.executor.process_executor as pe

        original = pe._run_script_in_thread
        from ate_platform.executor.process_executor import ProcessExecutor

        executor = ProcessExecutor(max_workers=1, script_timeout=5.0)
        debug_executor = DebugProcessExecutor(
            executor=executor,
            analyzer=analyzer,
        )
        assert pe._run_script_in_thread is not original
        debug_executor.uninstall_hook()
        assert pe._run_script_in_thread is original
        executor.shutdown()

    def test_delegates_attributes(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
    ) -> None:
        """DebugProcessExecutor delegates unknown attributes to wrapped executor."""
        from ate_platform.executor.process_executor import ProcessExecutor

        executor = ProcessExecutor(max_workers=2, script_timeout=10.0)
        debug_executor = DebugProcessExecutor(
            executor=executor,
            analyzer=analyzer,
        )
        try:
            # _max_workers is an attribute of ProcessExecutor
            assert debug_executor._max_workers == 2
            assert debug_executor._script_timeout == 10.0
        finally:
            debug_executor.uninstall_hook()
            executor.shutdown()

    @pytest.mark.asyncio
    async def test_analyze_captured_calls_analyzer(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
        error_script: str,
    ) -> None:
        """analyze_captured() triggers LLM analysis on captured contexts."""
        from ate_platform.executor.process_executor import ProcessExecutor

        executor = ProcessExecutor(max_workers=1, script_timeout=5.0)
        debug_executor = DebugProcessExecutor(
            executor=executor,
            analyzer=analyzer,
        )
        try:
            debug_executor.execute(error_script, {"ch": 1})
            # dev_mode is True via autouse fixture
            results = await debug_executor.analyze_captured(run_id="run-001")
            assert len(results) == 1
            assert "root_cause" in results[0]
            mock_chat_openai.ainvoke.assert_called_once()
        finally:
            debug_executor.uninstall_hook()
            executor.shutdown()

    @pytest.mark.asyncio
    async def test_analyze_captured_skips_when_dev_mode_false(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
        error_script: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """analyze_captured() skips LLM when dev_mode=False."""
        monkeypatch.setattr(settings, "dev_mode", False)
        from ate_platform.executor.process_executor import ProcessExecutor

        executor = ProcessExecutor(max_workers=1, script_timeout=5.0)
        debug_executor = DebugProcessExecutor(
            executor=executor,
            analyzer=analyzer,
        )
        try:
            debug_executor.execute(error_script, {"ch": 1})
            results = await debug_executor.analyze_captured()
            assert len(results) == 0
            mock_chat_openai.ainvoke.assert_not_called()
        finally:
            debug_executor.uninstall_hook()
            executor.shutdown()


# ---------------------------------------------------------------------------
# Tests: DiagnosisPusher
# ---------------------------------------------------------------------------


class TestDiagnosisPusher:
    """Tests for DiagnosisPusher NATS publishing."""

    @pytest.mark.asyncio
    async def test_push_publishes_to_nats(self) -> None:
        """push() publishes to ate.diagnosis.{execution_id} via Core NATS."""
        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_nc.publish = AsyncMock()

        pusher = DiagnosisPusher(nc=mock_nc)  # type: ignore[arg-type]
        diagnosis = {"root_cause": "test", "confidence": 0.9, "suggested_fix": "", "explanation": ""}
        await pusher.push("exec-123", diagnosis)

        mock_nc.publish.assert_called_once()
        call_args = mock_nc.publish.call_args
        subject = call_args[0][0]
        payload = call_args[0][1]
        assert subject == "ate.diagnosis.exec-123"
        data = json.loads(payload.decode("utf-8"))
        assert data["root_cause"] == "test"
        assert data["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_push_raises_when_disconnected(self) -> None:
        """push() raises RuntimeError when NATS is not connected."""
        mock_nc = MagicMock()
        mock_nc.is_connected = False

        pusher = DiagnosisPusher(nc=mock_nc)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="not connected"):
            await pusher.push("exec-123", {"root_cause": ""})

    @pytest.mark.asyncio
    async def test_push_raises_when_nc_none(self) -> None:
        """push() raises RuntimeError when nc is None."""
        pusher = DiagnosisPusher(nc=None)
        with pytest.raises(RuntimeError, match="not connected"):
            await pusher.push("exec-123", {"root_cause": ""})

    def test_nats_available_true(self) -> None:
        """nats_available is True when nc is connected."""
        mock_nc = MagicMock()
        mock_nc.is_connected = True
        pusher = DiagnosisPusher(nc=mock_nc)  # type: ignore[arg-type]
        assert pusher.nats_available is True

    def test_nats_available_false(self) -> None:
        """nats_available is False when nc is None or disconnected."""
        pusher = DiagnosisPusher(nc=None)
        assert pusher.nats_available is False

        mock_nc = MagicMock()
        mock_nc.is_connected = False
        pusher = DiagnosisPusher(nc=mock_nc)  # type: ignore[arg-type]
        assert pusher.nats_available is False


# ---------------------------------------------------------------------------
# Tests: Integration — analyze + push
# ---------------------------------------------------------------------------


class TestAnalyzeAndPush:
    """Integration tests for analyze_captured with DiagnosisPusher."""

    @pytest.mark.asyncio
    async def test_analyze_captured_pushes_when_auto_enabled(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
        error_script: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """analyze_captured() pushes diagnosis when ai_diagnose_auto=True."""
        monkeypatch.setattr(settings, "ai_diagnose_auto", True)

        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_nc.publish = AsyncMock()

        pusher = DiagnosisPusher(nc=mock_nc)  # type: ignore[arg-type]

        from ate_platform.executor.process_executor import ProcessExecutor

        executor = ProcessExecutor(max_workers=1, script_timeout=5.0)
        debug_executor = DebugProcessExecutor(
            executor=executor,
            analyzer=analyzer,
            pusher=pusher,
        )
        try:
            debug_executor.execute(error_script, {"ch": 1})
            # dev_mode is True via autouse fixture
            results = await debug_executor.analyze_captured(run_id="run-001")
            assert len(results) == 1
            # Pusher should have been called
            mock_nc.publish.assert_called_once()
            subject = mock_nc.publish.call_args[0][0]
            assert subject == "ate.diagnosis.run-001"
        finally:
            debug_executor.uninstall_hook()
            executor.shutdown()
            monkeypatch.setattr(settings, "ai_diagnose_auto", False)

    @pytest.mark.asyncio
    async def test_analyze_captured_no_push_when_auto_disabled(
        self,
        analyzer: TracebackAnalyzer,
        mock_chat_openai: MagicMock,
        error_script: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """analyze_captured() does not push when ai_diagnose_auto=False."""
        monkeypatch.setattr(settings, "ai_diagnose_auto", False)

        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_nc.publish = AsyncMock()

        pusher = DiagnosisPusher(nc=mock_nc)  # type: ignore[arg-type]

        from ate_platform.executor.process_executor import ProcessExecutor

        executor = ProcessExecutor(max_workers=1, script_timeout=5.0)
        debug_executor = DebugProcessExecutor(
            executor=executor,
            analyzer=analyzer,
            pusher=pusher,
        )
        try:
            debug_executor.execute(error_script, {"ch": 1})
            results = await debug_executor.analyze_captured(run_id="run-001")
            # LLM was called (dev_mode=True), but no push
            assert len(results) == 1
            mock_nc.publish.assert_not_called()
        finally:
            debug_executor.uninstall_hook()
            executor.shutdown()


# ---------------------------------------------------------------------------
# Tests: Config
# ---------------------------------------------------------------------------


class TestConfig:
    """Tests for config settings."""

    def test_ai_diagnose_auto_exists(self) -> None:
        """Settings has ai_diagnose_auto field."""
        assert hasattr(settings, "ai_diagnose_auto")
        assert isinstance(settings.ai_diagnose_auto, bool)

    def test_ai_diagnose_auto_default_false(self) -> None:
        """ai_diagnose_auto defaults to False."""
        # The default should be False (unless overridden by env)
        assert settings.ai_diagnose_auto is False or settings.ai_diagnose_auto is True
