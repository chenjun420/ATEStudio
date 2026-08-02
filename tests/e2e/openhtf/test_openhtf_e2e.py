"""End-to-end tests for OpenHTF integration via OpenHTFStepExecutor.

These E2E tests use the real OpenHTF library (openhtf>=1.6.0) to run
real test stations through the full pipeline:

    TestRecord capture -> _OUTCOME_MAP -> as_base_types() -> StepResult

Three test stations (real ``htf.Test`` modules) exercise the three
outcome paths:

    - **pass_station**: 3 passing phases with voltage/current measurements
      -> PASS -> StepStatus.PASSED.
    - **fail_station**: one phase with an out-of-range measurement ->
      FAIL -> StepStatus.FAILED.
    - **timeout**: pass_station with a slow phase, executed under
      ``use_isolation=True`` with a timeout shorter than the slow phase
      -> parent terminates child -> StepStatus.ERROR.

No mocking of OpenHTF internals -- ``htf.Test``, ``htf.PhaseOptions``,
``htf.measures``, and ``htf.Measurement`` are all real.

Known limitation (pre-existing, documented in Todo 23 learnings):
    ``_extract_measurement`` and ``_serialize_measurement`` read the
    measurement value via ``getattr(meas, "value", None)`` which returns
    ``None`` for real OpenHTF Measurement objects (the value lives at
    ``measured_value.value``). These tests assert on measurement KEY
    presence and outcome, not on extracted values.
"""

from __future__ import annotations

import json

from ate_platform.openhtf import OpenHTFStepExecutor, as_base_types
from ate_platform.types import StepStatus

# Dotted module paths to the real OpenHTF test station fixtures.
_PASS_MODULE = "tests.e2e.openhtf.fixtures.pass_station"
_FAIL_MODULE = "tests.e2e.openhtf.fixtures.fail_station"
_DUT_ID = "E2E_DUT_001"


class TestOpenHTFE2E:
    """E2E tests that run real OpenHTF test stations end-to-end."""

    async def test_openhtf_pass_flow(self) -> None:
        """A passing test station yields PASSED with captured record and serializable output.

        Given: a real OpenHTF test station (pass_station) with 3 passing
            phases (setup, measure_voltage, measure_current) that take
            in-range measurements (voltage=5.0V, current=0.12A).
        When: executed via OpenHTFStepExecutor.execute_async.
        Then: StepResult.status is PASSED, error is None, measurement
            names appear in outputs, _captured_record has phases, and
            as_base_types() produces a JSON-serializable dict.
        """
        executor = OpenHTFStepExecutor()
        result = await executor.execute_async(
            _PASS_MODULE, {"dut_id": _DUT_ID}
        )

        # Status and error.
        assert result.status is StepStatus.PASSED
        assert result.error is None

        # Measurement keys collected into outputs.
        assert "voltage" in result.outputs
        assert "current" in result.outputs

        # Meta fields.
        assert result.outputs["outcome"] == "PASS"
        assert result.outputs["dut_id"] == _DUT_ID

        # _captured_record has phases (trigger + setup + measure_voltage
        # + measure_current = at least 3 user-defined phases).
        captured = executor._captured_record
        assert captured is not None
        assert captured["outcome_name"] == "PASS"
        assert captured["dut_id"] == _DUT_ID
        phases = captured["phases"]
        assert len(phases) >= 3

        phase_names = {p["name"] for p in phases}
        assert "setup" in phase_names
        assert "measure_voltage" in phase_names
        assert "measure_current" in phase_names

        # The measure_voltage phase has a voltage measurement.
        voltage_phase = next(
            p for p in phases if p["name"] == "measure_voltage"
        )
        assert "voltage" in voltage_phase["measurements"]

        # as_base_types produces JSON-serializable output.
        assert executor._last_record is not None
        serialized = as_base_types(executor._last_record)
        json.dumps(serialized)  # raises TypeError if not serializable

        assert serialized["outcome"] == "PASS"
        assert serialized["dut_id"] == _DUT_ID
        assert len(serialized["phases"]) >= 3

    async def test_openhtf_fail_flow(self) -> None:
        """A failing test station yields FAILED with error containing failure description.

        Given: a real OpenHTF test station (fail_station) with a
            measure_fail phase that sets voltage=6.0V (out of [4.5, 5.5]
            range).
        When: executed via OpenHTFStepExecutor.execute_async.
        Then: StepResult.status is FAILED (via _OUTCOME_MAP["FAIL"]),
            error is populated, and outputs["outcome"] == "FAIL".
        """
        executor = OpenHTFStepExecutor()
        result = await executor.execute_async(
            _FAIL_MODULE, {"dut_id": _DUT_ID}
        )

        assert result.status is StepStatus.FAILED
        assert result.error is not None
        assert result.outputs["outcome"] == "FAIL"

        # The error should reference the failure (outcome name or detail).
        assert "FAIL" in result.error or "voltage" in result.error

        # _captured_record reflects the failure.
        captured = executor._captured_record
        assert captured is not None
        assert captured["outcome_name"] == "FAIL"

    async def test_openhtf_timeout_flow(self) -> None:
        """A test exceeding the timeout yields ERROR via spawn-context termination.

        Given: a real OpenHTF test station (pass_station with slow=True)
            that includes a slow_phase sleeping 3 seconds.
        When: executed via OpenHTFStepExecutor.execute_async with
            use_isolation=True and timeout=1.0.
        Then: StepResult.status is ERROR (TIMEOUT maps to ERROR via
            _OUTCOME_MAP), and the error message indicates timeout.

        The parent process spawns a child via
        ``multiprocessing.get_context("spawn")``, joins with timeout=1.0,
        sees the child is still alive (sleeping in slow_phase), terminates
        it, and returns ERROR.
        """
        executor = OpenHTFStepExecutor(use_isolation=True)
        result = await executor.execute_async(
            _PASS_MODULE,
            {"slow": True, "dut_id": _DUT_ID},
            timeout=1.0,
        )

        assert result.status is StepStatus.ERROR
        assert result.error is not None
        assert "timed out" in result.error.lower()
