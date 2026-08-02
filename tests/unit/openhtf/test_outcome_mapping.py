"""Unit tests for OpenHTFStepExecutor outcome mapping (Todo 20).

Tests cover the TestRecord -> StepResult outcome mapping:
- PASS outcome maps to StepStatus.PASSED
- FAIL outcome maps to StepStatus.FAILED with error from outcome_details
- TIMEOUT outcome maps to StepStatus.ERROR
- ABORTED outcome maps to StepStatus.SKIPPED
- Measurement values from all phases appear in StepResult.outputs with
  original precision preserved (no casting or rounding)
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from ate_platform.openhtf.step_executor import OpenHTFStepExecutor
from ate_platform.types import StepResult, StepStatus


class _FakeTest:
    """Minimal stand-in for openhtf.Test that fires output callbacks.

    Replicates OpenHTF's callback contract: ``add_output_callbacks(fn)``
    registers a callback, and ``execute()`` invokes every registered
    callback with the TestRecord once execution "completes".
    """

    def __init__(self, record: Any) -> None:
        self._record = record
        self._callbacks: list[Any] = []

    def add_output_callbacks(self, *callbacks: Any) -> None:
        self._callbacks.extend(callbacks)

    def execute(self, test_start: Any = None) -> None:
        for cb in self._callbacks:
            cb(self._record)


def _make_measurement(
    name: str,
    value: Any,
    outcome_name: str = "PASS",
    units: str | None = None,
) -> SimpleNamespace:
    """Build a fake Measurement with the attributes the executor reads."""
    return SimpleNamespace(
        name=name,
        value=value,
        outcome=SimpleNamespace(name=outcome_name),
        units=units,
    )


def _make_phase(
    name: str,
    measurements: dict[str, SimpleNamespace],
    outcome_name: str = "PASS",
) -> SimpleNamespace:
    """Build a fake PhaseRecord with the attributes the executor reads."""
    return SimpleNamespace(
        name=name,
        outcome=SimpleNamespace(name=outcome_name),
        marginal=None,
        start_time_millis=1000,
        end_time_millis=2000,
        measurements=measurements,
        attachments={},
    )


def _make_test_record(
    phases: list[SimpleNamespace],
    outcome_name: str = "PASS",
    outcome_details: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    """Build a fake TestRecord with the full field set the executor reads."""
    return SimpleNamespace(
        dut_id="DUT-001",
        station_id="STATION-01",
        start_time_millis=1000,
        end_time_millis=3000,
        outcome=SimpleNamespace(name=outcome_name),
        outcome_details=outcome_details or [],
        metadata={},
        phases=phases,
        marginal=None,
    )


def _execute_with_record(record: Any) -> StepResult:
    """Run the executor with a fake module that fires ``record``.

    Patches ``import_module`` so the executor discovers a ``_FakeTest`` that
    fires the supplied TestRecord to all registered output callbacks when
    ``execute()`` is called. Returns the resulting StepResult.
    """
    executor = OpenHTFStepExecutor()
    mock_module = SimpleNamespace(test=_FakeTest(record))
    with patch(
        "ate_platform.openhtf.step_executor.import_module",
        return_value=mock_module,
    ):
        return executor.execute("tests.openhtf.fake_module", {})


class TestOutcomeMapping:
    """Verify TestRecord.outcome maps to StepResult.status via OUTCOME_MAP."""

    def test_pass_maps_to_passed(self) -> None:
        """PASS outcome maps to StepStatus.PASSED with no error.

        Given: a TestRecord with outcome_name='PASS'.
        When: the executor runs and builds a StepResult.
        Then: StepResult.status is PASSED and error is None.
        """
        record = _make_test_record(
            [_make_phase("setup", {})],
            outcome_name="PASS",
        )
        result = _execute_with_record(record)
        assert result.status is StepStatus.PASSED
        assert result.error is None

    def test_fail_maps_to_failed(self) -> None:
        """FAIL outcome maps to StepStatus.FAILED with error from details.

        Given: a TestRecord with outcome_name='FAIL' and an outcome_detail
            carrying a description.
        When: the executor runs and builds a StepResult.
        Then: StepResult.status is FAILED and error contains the first
            outcome_detail's description.
        """
        record = _make_test_record(
            [_make_phase("measure", {}, outcome_name="FAIL")],
            outcome_name="FAIL",
            outcome_details=[
                SimpleNamespace(
                    code="VOLTAGE_OOR",
                    description="Voltage 5.2V out of range [4.8, 5.1]",
                ),
            ],
        )
        result = _execute_with_record(record)
        assert result.status is StepStatus.FAILED
        assert result.error is not None
        assert "Voltage 5.2V out of range" in result.error

    def test_timeout_maps_to_error(self) -> None:
        """TIMEOUT outcome maps to StepStatus.ERROR.

        Given: a TestRecord with outcome_name='TIMEOUT'.
        When: the executor runs and builds a StepResult.
        Then: StepResult.status is ERROR.
        """
        record = _make_test_record(
            [_make_phase("long_running", {})],
            outcome_name="TIMEOUT",
        )
        result = _execute_with_record(record)
        assert result.status is StepStatus.ERROR

    def test_aborted_maps_to_skipped(self) -> None:
        """ABORTED outcome maps to StepStatus.SKIPPED.

        Given: a TestRecord with outcome_name='ABORTED'.
        When: the executor runs and builds a StepResult.
        Then: StepResult.status is SKIPPED.
        """
        record = _make_test_record(
            [_make_phase("setup", {})],
            outcome_name="ABORTED",
        )
        result = _execute_with_record(record)
        assert result.status is StepStatus.SKIPPED

    def test_measurements_in_outputs(self) -> None:
        """Measurement values from all phases appear in StepResult.outputs.

        Given: a TestRecord with two phases carrying measurements
            (voltage=5.0, current=0.12, frequency=16_000_000).
        When: the executor runs and builds a StepResult.
        Then: every measurement value appears in outputs keyed by name,
            with original precision preserved (no rounding or casting).
        """
        phase_a = _make_phase(
            "power_rail",
            {
                "voltage": _make_measurement("voltage", 5.0, "PASS", "volts"),
                "current": _make_measurement("current", 0.12, "PASS", "amps"),
            },
        )
        phase_b = _make_phase(
            "clock",
            {
                "frequency": _make_measurement("frequency", 16_000_000, "PASS", "Hz"),
            },
        )
        record = _make_test_record([phase_a, phase_b], outcome_name="PASS")
        result = _execute_with_record(record)
        assert result.status is StepStatus.PASSED
        # Measurement values appear in outputs with original precision.
        assert result.outputs["voltage"] == 5.0
        assert result.outputs["current"] == 0.12
        assert result.outputs["frequency"] == 16_000_000
