"""OpenHTF-based StepExecutor implementation.

This module provides OpenHTFStepExecutor, which implements the StepExecutor
Protocol by wrapping OpenHTF's htf.Test execution model. The script_path
parameter is treated as a Python module path (e.g.,
``"tests.openhtf.my_test_module"``) rather than a file system path.

Test discovery convention:
    - If the module has a ``create_test()`` factory function, it is called
      with params as keyword arguments.
    - Otherwise, if the module has a module-level ``test`` variable, it is
      used directly.
    - If neither exists, execution returns an ERROR StepResult.

Process isolation:
    When ``use_isolation=True``, tests run in a spawned child process via
    ``multiprocessing.get_context("spawn")``. The child imports the module,
    executes the test, serializes the TestRecord via ``as_base_types()``,
    and sends ``(pid, StepResult)`` back via a Queue. Default
    ``use_isolation=False`` preserves in-process behavior for backward
    compatibility.
"""
# allow: SIZE_OK — The _extract_* helpers, _execute_isolated, and the
# top-level _run_openhtf_in_child / _serialized_to_result functions are
# all cohesive with OpenHTFStepExecutor's test-execution responsibility.
# The natural extraction seam is a future record_serializer module that
# houses _extract_*, as_base_types, and _serialized_to_result together.

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
from importlib import import_module
from typing import Any, cast

from ..types import StepResult, StepStatus
from .serialization import as_base_types

logger = logging.getLogger(__name__)

# Maps OpenHTF Outcome names to StepStatus. TIMEOUT collapses to ERROR;
# ABORTED (operator/system cancel) maps to SKIPPED.
_OUTCOME_MAP: dict[str, StepStatus] = {
    "PASS": StepStatus.PASSED,
    "FAIL": StepStatus.FAILED,
    "ERROR": StepStatus.ERROR,
    "TIMEOUT": StepStatus.ERROR,
    "ABORTED": StepStatus.SKIPPED,
}


class OpenHTFStepExecutor:
    """StepExecutor that wraps OpenHTF's htf.Test for test execution.

    Implements the StepExecutor Protocol by dynamically importing OpenHTF test
    modules and executing their htf.Test instances. The ``script_path``
    parameter is a Python dotted module path, not a file system path.

    Attributes:
        _max_workers: Maximum concurrent test executions for pool_stats.
        _active_workers: Count of currently executing tasks.
        _last_record: Last captured TestRecord (full object, retained for
            Todos 20/21 serialization and outcome mapping).
        _captured_record: Structured extraction of the last TestRecord's
            key fields (outcome, phases, measurements, metadata) for
            convenient access without re-walking the TestRecord attrs tree.
        _use_isolation: Whether tests run in a spawned child process.
        _mp_ctx: Spawn multiprocessing context (None when use_isolation=False).
            Typed as Any because multiprocessing context stubs do not
            expose Process/Queue on BaseContext.
        _last_child_pid: PID of the last spawned child process (for testing).
    """

    def __init__(
        self,
        max_workers: int = 4,
        use_isolation: bool = False,
        start_method: str = "spawn",
    ) -> None:
        """Initialize the OpenHTF step executor.

        Args:
            max_workers: Maximum number of concurrent test executions.
            use_isolation: If True, execute tests in a spawned child process
                via ``multiprocessing.get_context("spawn")`` for process
                isolation. Default False preserves in-process behavior.
            start_method: Multiprocessing start method. Only "spawn" is
                supported; "fork" is rejected because OpenHTF callbacks and
                TestRecord objects do not pickle reliably across fork
                boundaries.

        Raises:
            RuntimeError: If ``use_isolation=True`` and ``start_method`` is
                not "spawn".
        """
        self._max_workers = max_workers
        self._active_workers: int = 0
        self._last_record: Any = None
        self._captured_record: dict[str, Any] | None = None
        self._use_isolation = use_isolation
        self._mp_ctx: Any = None
        self._last_child_pid: int | None = None
        if use_isolation:
            if start_method != "spawn":
                raise RuntimeError("Only spawn context is supported for process isolation")
            self._mp_ctx = multiprocessing.get_context("spawn")

    def execute(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str | None = None,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> StepResult:
        """Execute an OpenHTF test module synchronously.

        When ``use_isolation=True``, the test runs in a spawned child
        process via ``multiprocessing.get_context("spawn")``. The child
        imports the module, executes the test, serializes the TestRecord
        via ``as_base_types()``, and sends ``(pid, StepResult)`` back via
        a Queue. When ``use_isolation=False`` (default), the test runs
        in-process via ``_run_openhtf_test``.

        Args:
            script_path: Python module path to the OpenHTF test module.
            params: Parameters passed to ``create_test()`` or as phase inputs.
            step_id: Optional step identifier.
            timeout: Optional timeout override in seconds. Enforced via
                ``proc.join(timeout=...)`` when isolated; logged-but-ignored
                in in-process mode.
            run_id: Optional execution run identifier.

        Returns:
            StepResult containing execution outcome.
        """
        self._active_workers += 1
        try:
            if self._use_isolation:
                return self._execute_isolated(script_path, params, timeout)
            return self._run_openhtf_test(script_path, params, timeout)
        finally:
            self._active_workers -= 1

    def _execute_isolated(
        self,
        script_path: str,
        params: dict[str, Any],
        timeout: float | None,
    ) -> StepResult:
        """Execute an OpenHTF test in a spawned child process.

        Creates a Queue and Process via ``self._mp_ctx`` (a spawn context),
        starts the child, joins with the given timeout, and reads the
        ``(pid, StepResult)`` tuple from the Queue.

        Error paths:
            - If the child is still alive after ``join`` (timeout exceeded):
              terminate, join, return ERROR with "execution timed out".
            - If the Queue is empty after ``join`` (child crashed without
              producing a result): return ERROR with "child process failed
              to produce result".

        Args:
            script_path: Python module path to the OpenHTF test module.
            params: Parameters for test creation.
            timeout: Optional timeout in seconds. If None or falsy, waits
                indefinitely for the child to finish.

        Returns:
            StepResult containing execution outcome.
        """
        assert self._mp_ctx is not None  # set by __init__ when use_isolation=True
        queue: Any = self._mp_ctx.Queue()
        proc = self._mp_ctx.Process(
            target=_run_openhtf_in_child,
            args=(script_path, params, timeout, queue),
        )
        proc.start()
        proc.join(timeout=timeout if timeout else None)

        if proc.is_alive():
            proc.terminate()
            proc.join()
            error_msg = f"OpenHTF test '{script_path}' execution timed out"
            logger.warning(error_msg)
            return StepResult(status=StepStatus.ERROR, error=error_msg)

        try:
            pid, result = queue.get(timeout=5)
        except Exception:
            error_msg = f"Child process failed to produce result for '{script_path}'"
            logger.error(error_msg)
            return StepResult(status=StepStatus.ERROR, error=error_msg)

        self._last_child_pid = pid
        return cast(StepResult, result)

    async def execute_async(
        self,
        script_path: str,
        params: dict[str, Any],
        step_id: str | None = None,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> StepResult:
        """Execute an OpenHTF test module asynchronously.

        Args:
            script_path: Python module path to the OpenHTF test module.
            params: Parameters passed to ``create_test()`` or as phase inputs.
            step_id: Optional step identifier.
            timeout: Optional timeout override in seconds.
            run_id: Optional execution run identifier.

        Returns:
            StepResult containing execution outcome.
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
        """Execute multiple OpenHTF tests concurrently with bounded concurrency.

        Args:
            tasks: List of ExecuteTask instances describing test modules to run.
            max_concurrency: Maximum number of concurrent executions.

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

    def pool_stats(self) -> dict[str, Any]:
        """Return current worker pool statistics.

        Returns:
            Dict with active, max, utilization, queued keys.
        """
        utilization = (
            self._active_workers / self._max_workers if self._max_workers > 0 else 0.0
        )
        return {
            "active": self._active_workers,
            "max": self._max_workers,
            "utilization": utilization,
            "queued": 0,
        }

    def _on_test_complete(self, record: Any) -> None:
        """Output callback -- captures the full TestRecord.

        Stores the raw TestRecord on ``self._last_record`` (preserving all
        phase data, measurements, attachments, and metadata for Todos 20 and
        21) and extracts a structured summary into ``self._captured_record``
        for convenient field-by-field access without re-walking the attrs
        tree.

        Thread-safety: this callback fires inside ``test.execute()``, which
        is a blocking call running in a single thread (the caller's thread,
        or the worker thread when invoked via ``execute_async``). Per-execution
        safety is provided by the local ``captured_records`` list in
        ``_run_openhtf_test``; the instance attributes reflect the most
        recently completed execution and are intended for inspection, not for
        concurrent StepResult building.

        Args:
            record: The OpenHTF TestRecord from test execution.
        """
        self._last_record = record
        self._captured_record = self._extract_record(record)

    def _extract_record(self, record: Any) -> dict[str, Any]:
        """Extract a structured summary from a TestRecord.

        Captures the full set of TestRecord fields enumerated in the OpenHTF
        API reference: ``dut_id``, ``station_id``, ``start_time_millis``,
        ``end_time_millis``, ``outcome``, ``outcome_details``, ``metadata``,
        ``phases`` (each with its measurements), and ``marginal``. No
        measurement data is dropped -- every phase's measurements are
        included.

        Args:
            record: The OpenHTF TestRecord (attrs-based).

        Returns:
            Dict with the extracted fields; ``outcome`` is stored both as
            the raw enum (under ``outcome``) and its name string (under
            ``outcome_name``) for downstream consumers.
        """
        outcome = getattr(record, "outcome", None)
        outcome_details = [
            {
                "code": getattr(d, "code", None),
                "description": getattr(d, "description", None),
            }
            for d in getattr(record, "outcome_details", []) or []
        ]
        phases = [
            self._extract_phase(p) for p in getattr(record, "phases", []) or []
        ]
        return {
            "dut_id": getattr(record, "dut_id", None),
            "station_id": getattr(record, "station_id", None),
            "start_time_millis": getattr(record, "start_time_millis", None),
            "end_time_millis": getattr(record, "end_time_millis", None),
            "outcome": outcome,
            "outcome_name": outcome.name if outcome is not None else None,
            "outcome_details": outcome_details,
            "metadata": dict(getattr(record, "metadata", {}) or {}),
            "phases": phases,
            "marginal": getattr(record, "marginal", None),
        }

    def _extract_phase(self, phase: Any) -> dict[str, Any]:
        """Extract a structured summary from a PhaseRecord.

        Captures the phase's name, outcome, timing, marginal flag, the full
        measurements dict (name -> {value, outcome, units}), and the list of
        attachment names (attachment payloads are skipped -- they may be
        binary; the raw TestRecord on ``self._last_record`` retains them).

        Args:
            phase: The OpenHTF PhaseRecord (attrs-based).

        Returns:
            Dict with the extracted phase fields.
        """
        outcome = getattr(phase, "outcome", None)
        measurements = getattr(phase, "measurements", {}) or {}
        return {
            "name": getattr(phase, "name", None),
            "outcome": outcome,
            "outcome_name": outcome.name if outcome is not None else None,
            "start_time_millis": getattr(phase, "start_time_millis", None),
            "end_time_millis": getattr(phase, "end_time_millis", None),
            "marginal": getattr(phase, "marginal", None),
            "measurements": {
                name: self._extract_measurement(meas)
                for name, meas in measurements.items()
            },
            "attachment_names": list(
                (getattr(phase, "attachments", {}) or {}).keys()
            ),
        }

    def _extract_measurement(self, meas: Any) -> dict[str, Any]:
        """Extract a structured summary from a Measurement.

        Captures the measurement's value, outcome, and units. The value is
        read via ``getattr`` to avoid coupling to the ``MeasuredValue`` vs
        ``DimensionedMeasuredValue`` internal shape.

        Args:
            meas: The OpenHTF Measurement (attrs-based).

        Returns:
            Dict with ``value``, ``outcome``, ``outcome_name``, and ``units``.
        """
        outcome = getattr(meas, "outcome", None)
        return {
            "value": getattr(meas, "value", None),
            "outcome": outcome,
            "outcome_name": outcome.name if outcome is not None else None,
            "units": getattr(meas, "units", None),
        }

    def _run_openhtf_test(
        self,
        script_path: str,
        params: dict[str, Any],
        timeout: float | None = None,
    ) -> StepResult:
        """Import and execute an OpenHTF test module.

        Args:
            script_path: Python module path to import.
            params: Parameters for test creation.
            timeout: Optional timeout (not yet fully implemented -- Todo 22).

        Returns:
            StepResult with execution outcome.
        """
        if timeout is not None:
            logger.warning(
                "Timeout not enforced in in-process mode for %s; "
                "use use_isolation=True for timeout enforcement",
                script_path,
            )

        # Import the test module dynamically.
        try:
            module = import_module(script_path)
        except ImportError as e:
            error_msg = f"Failed to import OpenHTF test module '{script_path}': {e}"
            logger.error(error_msg)
            return StepResult(status=StepStatus.ERROR, error=error_msg)
        except Exception as e:
            error_msg = f"Error importing OpenHTF test module '{script_path}': {e}"
            logger.exception(error_msg)
            return StepResult(status=StepStatus.ERROR, error=error_msg)

        # Discover the htf.Test instance via convention.
        create_test_fn = getattr(module, "create_test", None)
        if callable(create_test_fn):
            logger.debug("Found create_test() factory in '%s'", script_path)
            test = create_test_fn(**params)
        else:
            test = getattr(module, "test", None)

        if test is None:
            error_msg = (
                f"No htf.Test found in module '{script_path}'; "
                "expected module-level 'test' variable or create_test() factory"
            )
            logger.error(error_msg)
            return StepResult(status=StepStatus.ERROR, error=error_msg)

        # Register output callbacks to capture TestRecord.
        # The local list captures the record for StepResult building (per-
        # execution thread-safe); _on_test_complete stores the raw record
        # and a structured summary on the instance for inspection and for
        # downstream Todos 20 (outcome mapping) and 21 (serialization).
        captured_records: list[Any] = []
        test.add_output_callbacks(captured_records.append)
        test.add_output_callbacks(self._on_test_complete)

        # Execute the test. DUT ID comes from params or defaults to UNKNOWN.
        dut_id = str(params.get("dut_id", "UNKNOWN"))
        try:
            test.execute(test_start=lambda: dut_id)
        except Exception as e:
            error_msg = f"OpenHTF test execution failed for '{script_path}': {e}"
            logger.exception(error_msg)
            return StepResult(status=StepStatus.ERROR, error=error_msg)

        # Build StepResult from captured TestRecord.
        record = captured_records[0] if captured_records else None
        return self._record_to_result(record, script_path)

    def _record_to_result(
        self,
        record: Any,
        script_path: str,
    ) -> StepResult:
        """Convert a captured TestRecord to a StepResult.

        Maps the TestRecord outcome to StepStatus via ``_OUTCOME_MAP``,
        collects measurement values from every phase into ``outputs``, and
        forwards timing and error details. Measurement values are stored
        verbatim -- no casting or rounding -- so downstream consumers see
        the original precision.

        Uses ``self._captured_record`` (populated by the
        ``_on_test_complete`` output callback) for field access. The
        callback fires inside ``test.execute()`` before this method is
        called, so ``_captured_record`` is guaranteed non-None when
        ``record`` is non-None.

        Args:
            record: The OpenHTF TestRecord, or None if not captured.
            script_path: Module path (for error messages).

        Returns:
            StepResult with status, outputs (measurements + meta + timing),
            and error populated from the TestRecord.
        """
        if record is None:
            error_msg = f"No TestRecord captured for '{script_path}'"
            logger.warning(error_msg)
            return StepResult(status=StepStatus.ERROR, error=error_msg)

        captured = self._captured_record
        assert captured is not None  # callback contract: fires before _record_to_result

        outcome_name = captured["outcome_name"] or "ERROR"
        status = _OUTCOME_MAP.get(outcome_name, StepStatus.ERROR)
        if outcome_name not in _OUTCOME_MAP:
            logger.warning(
                "Unknown OpenHTF outcome '%s' for '%s'; defaulting to ERROR",
                outcome_name,
                script_path,
            )

        # Build outputs: meta fields, timing, then measurement values.
        # Measurements go last so domain data takes precedence on key
        # collision (unlikely -- measurement names are domain-specific).
        outputs: dict[str, Any] = {"outcome": outcome_name}
        outputs["dut_id"] = captured["dut_id"]
        metadata = captured["metadata"]
        if metadata:
            outputs["metadata"] = dict(metadata)
        # StepResult has no dedicated timing fields; record in outputs.
        outputs["start_time_millis"] = captured["start_time_millis"]
        outputs["end_time_millis"] = captured["end_time_millis"]
        # Collect measurement values from all phases, keyed by name.
        for phase in captured["phases"]:
            for name, meas in phase["measurements"].items():
                outputs[name] = meas["value"]

        # Error: include the first outcome_detail's description if present.
        error: str | None = None
        if status != StepStatus.PASSED:
            details = captured["outcome_details"]
            if details:
                first_desc = details[0].get("description")
                error = first_desc if first_desc else outcome_name
            else:
                error = outcome_name

        return StepResult(status=status, outputs=outputs, error=error)


def _run_openhtf_in_child(
    script_path: str,
    params: dict[str, Any],
    timeout: float | None,
    queue: Any,
) -> None:
    """Run an OpenHTF test in a spawned child process.

    This function executes in a child process created via
    ``multiprocessing.get_context("spawn").Process(...)``. It imports the
    test module (real import, not patched), discovers the htf.Test via
    convention (``create_test()`` factory or module-level ``test`` variable),
    registers an output callback to capture the TestRecord, executes the
    test, serializes the TestRecord via ``as_base_types()``, builds a
    StepResult via ``_serialized_to_result``, and puts ``(pid, result)``
    on the Queue.

    On ANY exception, puts ``(pid, StepResult(status=ERROR, error=str(e)))``
    on the Queue so the parent always receives a result.

    The ``timeout`` parameter is forwarded from the parent but is NOT
    enforced here — the parent enforces it via ``proc.join(timeout=...)``
    and terminates the child if it exceeds the deadline.

    Args:
        script_path: Python module path to import.
        params: Parameters for test creation.
        timeout: Optional timeout (not enforced in child; parent uses
            ``proc.join``).
        queue: Multiprocessing Queue for sending ``(pid, StepResult)`` back.
    """
    try:
        module = import_module(script_path)

        create_test_fn = getattr(module, "create_test", None)
        if callable(create_test_fn):
            test = create_test_fn(**params)
        else:
            test = getattr(module, "test", None)

        if test is None:
            error_msg = (
                f"No htf.Test found in module '{script_path}'; "
                "expected module-level 'test' variable or create_test() factory"
            )
            queue.put((os.getpid(), StepResult(status=StepStatus.ERROR, error=error_msg)))
            return

        captured_records: list[Any] = []
        test.add_output_callbacks(captured_records.append)

        dut_id = str(params.get("dut_id", "UNKNOWN"))
        test.execute(test_start=lambda: dut_id)

        record = captured_records[0] if captured_records else None
        if record is None:
            error_msg = f"No TestRecord captured for '{script_path}'"
            queue.put((os.getpid(), StepResult(status=StepStatus.ERROR, error=error_msg)))
            return

        serialized = as_base_types(record)
        step_result = _serialized_to_result(serialized, script_path)
        queue.put((os.getpid(), step_result))

    except Exception as e:
        error_msg = f"OpenHTF test execution failed in child for '{script_path}': {e}"
        queue.put((os.getpid(), StepResult(status=StepStatus.ERROR, error=error_msg, outputs={})))


def _serialized_to_result(
    serialized: dict[str, Any],
    script_path: str,
) -> StepResult:
    """Build a StepResult from an ``as_base_types()`` serialized TestRecord dict.

    Mirrors the logic of ``OpenHTFStepExecutor._record_to_result`` but
    operates on the ``as_base_types()`` output shape: the outcome is a name
    string under the ``outcome`` key (not ``outcome_name``), and measurements
    use ``unit`` (not ``units``).

    Args:
        serialized: The serialized TestRecord dict from ``as_base_types()``.
        script_path: Module path (for error messages).

    Returns:
        StepResult with status, outputs (measurements + meta + timing),
        and error populated from the serialized TestRecord.
    """
    outcome_name = serialized.get("outcome") or "ERROR"
    status = _OUTCOME_MAP.get(outcome_name, StepStatus.ERROR)
    if outcome_name not in _OUTCOME_MAP:
        logger.warning(
            "Unknown OpenHTF outcome '%s' for '%s'; defaulting to ERROR",
            outcome_name,
            script_path,
        )

    outputs: dict[str, Any] = {"outcome": outcome_name}
    outputs["dut_id"] = serialized.get("dut_id")
    metadata = serialized.get("metadata", {})
    if metadata:
        outputs["metadata"] = dict(metadata)
    outputs["start_time_millis"] = serialized.get("start_time_millis")
    outputs["end_time_millis"] = serialized.get("end_time_millis")
    for phase in serialized.get("phases", []):
        for name, meas in phase.get("measurements", {}).items():
            outputs[name] = meas.get("value")

    error: str | None = None
    if status != StepStatus.PASSED:
        details = serialized.get("outcome_details", [])
        if details:
            first_desc = details[0].get("description")
            error = first_desc if first_desc else outcome_name
        else:
            error = outcome_name

    return StepResult(status=status, outputs=outputs, error=error)
