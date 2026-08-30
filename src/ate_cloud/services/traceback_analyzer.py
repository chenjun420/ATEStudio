"""Traceback Analyzer — AI-assisted traceback analysis via LangChain ChatOpenAI.

Captures tracebacks from ProcessExecutor's exception path using a wrapper
pattern (``DebugProcessExecutor`` monkey-patches ``_run_script_in_thread``
and the module-level ``exec`` to intercept exceptions), extracts context
(traceback text + source snippet + input params + first 5 local variables),
and calls the LLM for root cause analysis.

Uses CircuitBreaker (failure_threshold=5, timeout=30s) for LLM call
protection — no silent degradation (AGENTS.md §7). When ``settings.dev_mode``
is False, LLM analysis is skipped; only the traceback is logged.

When ``settings.ai_diagnose_auto`` is True and a DiagnosisPusher is
configured, results are pushed to ``ate.diagnosis.{execution_id}`` via
Core NATS for real-time operator UI updates.
"""

from __future__ import annotations

import builtins
import json
import logging
import sys
import threading
import traceback as tb_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ate_cloud.config import settings
from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)

#: System prompt for the LLM — instructs it to output strict JSON.
#: Curly braces are doubled ({{ }}) to escape LangChain's template format.
_SYSTEM_PROMPT = (
    "你是ATE Studio故障诊断专家。分析以下测试脚本异常，"
    "给出根因、置信度(0.0-1.0)、修复建议(code diff)。"
    "只输出JSON格式：{{root_cause, confidence, suggested_fix, explanation}}"
)

#: Maximum number of local variables to capture (payload size limit).
_MAX_LOCAL_VARS = 5

#: Maximum number of source lines to include in the prompt.
_MAX_SOURCE_LINES = 20

#: Maximum length of a variable's repr before truncation.
_MAX_VAR_REPR_LEN = 200

#: Thread-local storage for passing script context to the exec interceptor.
_thread_local: threading.local = threading.local()


@dataclass
class TracebackContext:
    """Context captured from a script execution exception.

    Attributes:
        script_path: Path to the script that raised the exception.
        params: Input parameters passed to the script.
        step_id: Step identifier for the execution.
        exc_type: Exception class name (e.g. ``"ValueError"``).
        exc_value: Exception message (``str(exc)``).
        traceback_text: Full traceback text from ``traceback.format_exc()``.
        local_vars: Up to 5 local variable name→repr mappings from the
            innermost frame.
        source_snippet: First 20 lines of the script file.
        run_id: Optional execution run identifier.
    """

    script_path: str
    params: dict[str, Any]
    step_id: str
    exc_type: str
    exc_value: str
    traceback_text: str
    local_vars: dict[str, str] = field(default_factory=dict)
    source_snippet: str = ""
    run_id: str | None = None


class TracebackAnalyzer:
    """Analyzes script execution tracebacks using LangChain ChatOpenAI.

    Wraps the LLM call in a CircuitBreaker (failure_threshold=5, timeout=30s).
    Per AGENTS.md §7: if the LLM is configured but unreachable, the
    CircuitBreaker opens and ``CircuitBreakerOpenError`` propagates — no
    silent degradation to a stub response.

    When ``settings.dev_mode`` is False, LLM analysis is skipped entirely;
    only the traceback is logged.

    Args:
        api_key: OpenAI API key (defaults to ``settings.openai_api_key``).
        model: Chat model name (default ``"gpt-4o-mini"``).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self._api_key = api_key or settings.openai_api_key
        self._model = model or settings.openai_model
        self._breaker = CircuitBreaker(
            failure_threshold=5,
            timeout=30.0,
            name="llm-traceback-analyzer",
        )
        self._llm: Any = None
        self._prompt: Any = None
        self._initialized = False

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Underlying CircuitBreaker instance (for inspection/reset)."""
        return self._breaker

    def _ensure_initialized(self) -> None:
        """Lazily initialize LangChain components (deferred import).

        Defers the ``langchain_openai`` / ``langchain_core`` imports until
        the first analysis call, so modules importing this service don't
        pay the LangChain startup cost if dev_mode is False.
        """
        if self._initialized:
            return
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        kwargs: dict[str, Any] = {
            "model": self._model,
            "api_key": SecretStr(self._api_key),
            "temperature": 0,
        }
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        self._llm = ChatOpenAI(**kwargs)
        self._prompt = ChatPromptTemplate.from_messages([
            ("system", _SYSTEM_PROMPT),
            ("human", "{traceback_info}"),
        ])
        self._initialized = True

    async def analyze(self, context: TracebackContext) -> dict[str, Any] | None:
        """Analyze a traceback and return LLM diagnosis, or None if skipped.

        Always logs the traceback. When ``settings.dev_mode`` is True,
        calls the LLM via CircuitBreaker and returns a dict with
        ``root_cause``, ``confidence``, ``suggested_fix``, ``explanation``.

        Returns:
            Diagnosis dict, or ``None`` when dev_mode is False (LLM skipped).

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN after repeated
                LLM failures.
            Exception: Any LLM API error not suppressed by the breaker.
        """
        logger.warning(
            "Script '%s' (step=%s) failed: %s: %s\n%s",
            context.script_path,
            context.step_id,
            context.exc_type,
            context.exc_value,
            context.traceback_text,
        )

        if not settings.dev_mode:
            logger.info("dev_mode=False, skipping LLM traceback analysis")
            return None

        self._ensure_initialized()
        info_text = self._build_info_text(context)

        async def _do_llm_call() -> str:
            messages = self._prompt.format_messages(traceback_info=info_text)
            response = await self._llm.ainvoke(messages)
            return str(response.content)

        raw = await self._breaker.call(_do_llm_call)
        # CircuitBreaker.call infers T as Coroutine for async fn; runtime is str
        return self._parse_response(raw)

    def _build_info_text(self, context: TracebackContext) -> str:
        """Build the human-readable traceback info for the LLM prompt."""
        lines: list[str] = [
            f"Script: {context.script_path}",
            f"Step ID: {context.step_id}",
            f"Exception: {context.exc_type}: {context.exc_value}",
            "",
            "Traceback:",
            context.traceback_text,
        ]
        if context.source_snippet:
            lines.extend(["", "Source snippet:", context.source_snippet])
        if context.params:
            lines.extend(["", f"Input params: {context.params}"])
        if context.local_vars:
            lines.extend(["", "Local variables (first 5):"])
            for name, value in context.local_vars.items():
                lines.append(f"  {name} = {value}")
        return "\n".join(lines)

    def _parse_response(self, raw: str) -> dict[str, Any]:
        """Parse the LLM JSON response into a diagnosis dict.

        Strips markdown code fences if present. Falls back to putting the
        raw text in ``explanation`` if JSON parsing fails.
        """
        text = raw.strip()
        # Strip markdown code fences (```json ... ```)
        if text.startswith("```"):
            fence_lines = text.split("\n")
            fence_lines = [
                line for line in fence_lines[1:]
                if not line.strip().startswith("```")
            ]
            text = "\n".join(fence_lines).strip()

        try:
            data = json.loads(text)
            return {
                "root_cause": str(data.get("root_cause", "")),
                "confidence": float(data.get("confidence", 0.0)),
                "suggested_fix": str(data.get("suggested_fix", "")),
                "explanation": str(data.get("explanation", "")),
            }
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("Failed to parse LLM response as JSON: %s", e)
            return {
                "root_cause": "",
                "confidence": 0.0,
                "suggested_fix": "",
                "explanation": raw,
            }


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def extract_local_vars(frame: Any, max_vars: int = _MAX_LOCAL_VARS) -> dict[str, str]:
    """Extract up to ``max_vars`` local variables from a frame.

    Returns string representations (``repr``) of variable values, truncated
    to ``_MAX_VAR_REPR_LEN`` characters. Skips dunder variables.
    """
    result: dict[str, str] = {}
    try:
        local_vars = frame.f_locals
    except Exception:
        return result

    for name, value in local_vars.items():
        if name.startswith("__"):
            continue
        if len(result) >= max_vars:
            break
        try:
            repr_str = repr(value)
            if len(repr_str) > _MAX_VAR_REPR_LEN:
                repr_str = repr_str[:_MAX_VAR_REPR_LEN] + "..."
            result[name] = repr_str
        except Exception:
            result[name] = "<unreprable>"
    return result


def extract_source_snippet(script_path: str, max_lines: int = _MAX_SOURCE_LINES) -> str:
    """Read the first ``max_lines`` of a script file as a source snippet."""
    try:
        path = Path(script_path)
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[:max_lines])
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# DebugProcessExecutor — wrapper that captures tracebacks
# ---------------------------------------------------------------------------


class DebugProcessExecutor:
    """Wrapper around ProcessExecutor that captures tracebacks for AI analysis.

    Uses a two-layer monkey-patch on the ``process_executor`` module:

    1. Patches ``_run_script_in_thread`` to set thread-local context
       (script_path, params, step_id) before calling the original.
    2. Patches the module-level ``exec`` to intercept exceptions: captures
       the full traceback + local variables, stores a ``TracebackContext``
       in a thread-safe list, then re-raises so the original function's
       except block handles it normally.

    This approach does **not** modify ``process_executor.py`` source — all
    patching is done at runtime on the module's attributes. The happy path
    is fully delegated to the original function.

    On error, captured contexts can be drained via :meth:`drain_captured`
    and analyzed via :meth:`analyze_captured` (async, triggers LLM +
    optional push).

    Args:
        executor: The wrapped ``ProcessExecutor`` instance.
        analyzer: ``TracebackAnalyzer`` for LLM diagnosis.
        pusher: Optional ``DiagnosisPusher`` for NATS auto-push.
    """

    def __init__(
        self,
        executor: Any,
        analyzer: TracebackAnalyzer,
        pusher: Any = None,
    ) -> None:
        self._executor = executor
        self._analyzer = analyzer
        self._pusher = pusher
        self._captured: list[TracebackContext] = []
        self._lock = threading.Lock()
        # Saved originals for uninstall
        self._original_runner: Any = None
        self._exec_was_in_globals: bool = False
        self._installed = False
        self._install_hook()

    def _install_hook(self) -> None:
        """Monkey-patch _run_script_in_thread and exec on the module."""
        import ate_platform.executor.process_executor as pe

        # Save originals
        self._original_runner = pe._run_script_in_thread
        self._exec_was_in_globals = "exec" in pe.__dict__

        # Layer 1: patch exec to capture tracebacks on exception
        original_exec = builtins.exec

        def capturing_exec(code: Any, globs: Any = None, locs: Any = None) -> None:
            try:
                original_exec(code, globs, locs)
            except Exception:
                self._capture_exception()
                raise

        pe.exec = capturing_exec  # type: ignore[attr-defined]

        # Layer 2: patch _run_script_in_thread to set thread-local context
        original_runner = self._original_runner

        def patched_runner(
            script_path: str,
            params: dict[str, Any],
            step_id: str,
        ) -> dict[str, Any]:
            _thread_local.ate_context = (script_path, params, step_id)
            try:
                return original_runner(script_path, params, step_id)  # type: ignore[no-any-return]
            finally:
                _thread_local.ate_context = None

        pe._run_script_in_thread = patched_runner
        self._installed = True
        logger.debug("DebugProcessExecutor hook installed")

    def uninstall_hook(self) -> None:
        """Restore the original _run_script_in_thread and exec."""
        if not self._installed:
            return
        import ate_platform.executor.process_executor as pe

        pe._run_script_in_thread = self._original_runner
        if not self._exec_was_in_globals and hasattr(pe, "exec"):
            delattr(pe, "exec")
        self._installed = False
        logger.debug("DebugProcessExecutor hook uninstalled")

    def _capture_exception(self) -> None:
        """Capture the current exception's traceback into _captured."""
        exc_info = sys.exc_info()
        if exc_info[0] is None:
            return

        tb = exc_info[2]
        traceback_text = tb_module.format_exc()

        # Walk to innermost frame for local variables
        local_vars: dict[str, str] = {}
        if tb is not None:
            while tb.tb_next is not None:
                tb = tb.tb_next
            local_vars = extract_local_vars(tb.tb_frame)

        # Get script context from thread-local
        context_info = getattr(_thread_local, "ate_context", None)
        if context_info is None:
            return  # Not from our patched runner

        script_path, params, step_id = context_info

        context = TracebackContext(
            script_path=script_path,
            params=params,
            step_id=step_id,
            exc_type=exc_info[0].__name__ if exc_info[0] else "",
            exc_value=str(exc_info[1]) if exc_info[1] else "",
            traceback_text=traceback_text,
            local_vars=local_vars,
            source_snippet=extract_source_snippet(script_path),
        )

        with self._lock:
            self._captured.append(context)

    def drain_captured(self) -> list[TracebackContext]:
        """Drain and return all captured traceback contexts.

        Clears the internal list. Thread-safe.
        """
        with self._lock:
            result = self._captured[:]
            self._captured.clear()
        return result

    async def analyze_captured(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Analyze all captured contexts and optionally push results.

        For each captured context, calls :meth:`TracebackAnalyzer.analyze`.
        If ``settings.ai_diagnose_auto`` is True and a pusher is configured,
        pushes the diagnosis to NATS.

        Returns:
            List of diagnosis dicts (or empty dicts for skipped analyses).
        """
        contexts = self.drain_captured()
        results: list[dict[str, Any]] = []
        for context in contexts:
            context.run_id = run_id
            try:
                diagnosis = await self._analyzer.analyze(context)
                if diagnosis is not None:
                    results.append(diagnosis)
                    if (
                        settings.ai_diagnose_auto
                        and self._pusher is not None
                        and run_id is not None
                    ):
                        await self._pusher.push(run_id, diagnosis)
            except CircuitBreakerOpenError:
                logger.warning(
                    "CircuitBreaker open, diagnosis skipped for %s",
                    context.script_path,
                )
            except Exception:
                logger.exception(
                    "Analysis failed for %s",
                    context.script_path,
                )
        return results

    # ── Delegation to wrapped executor ──────────────────────────────

    def execute(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str | None = None,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> Any:
        """Delegate to wrapped executor.execute()."""
        return self._executor.execute(
            script_path, params, step_id=step_id, timeout=timeout, run_id=run_id,
        )

    async def execute_async(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str | None = None,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> Any:
        """Delegate to wrapped executor.execute_async(), then analyze captured."""
        result = await self._executor.execute_async(
            script_path, params, step_id=step_id, timeout=timeout, run_id=run_id,
        )
        # Trigger analysis for any captured contexts (non-blocking)
        contexts = self.drain_captured()
        for context in contexts:
            context.run_id = run_id
            asyncio_create_task(self._analyze_and_push(context, run_id))
        return result

    async def _analyze_and_push(
        self,
        context: TracebackContext,
        run_id: str | None,
    ) -> None:
        """Analyze a single context and push if configured (background task)."""
        try:
            diagnosis = await self._analyzer.analyze(context)
            if (
                diagnosis is not None
                and settings.ai_diagnose_auto
                and self._pusher is not None
                and run_id is not None
            ):
                await self._pusher.push(run_id, diagnosis)
        except CircuitBreakerOpenError:
            logger.warning(
                "CircuitBreaker open, diagnosis skipped for %s",
                context.script_path,
            )
        except Exception:
            logger.exception(
                "Analysis failed for %s",
                context.script_path,
            )

    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attributes to the wrapped executor."""
        return getattr(self._executor, name)

    def __del__(self) -> None:
        """Ensure hooks are uninstalled on deletion."""
        try:
            self.uninstall_hook()
        except Exception:
            pass


def asyncio_create_task(coro: Any) -> None:
    """Create an asyncio task, handling the case of no running loop.

    If there is no running event loop (e.g., called from a sync context),
    the coroutine is run via ``asyncio.run`` in a background thread.
    """
    try:
        import asyncio

        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        # No running loop — run in a background thread
        import asyncio
        import threading

        def _run() -> None:
            try:
                asyncio.run(coro)
            except Exception:
                logger.exception("Background analysis task failed")

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()


__all__ = [
    "TracebackContext",
    "TracebackAnalyzer",
    "DebugProcessExecutor",
    "extract_local_vars",
    "extract_source_snippet",
]
