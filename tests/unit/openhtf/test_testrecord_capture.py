"""Unit tests for OpenHTFStepExecutor TestRecord capture (Todo 19).

Tests cover the output-callback path that captures the full TestRecord on
test completion:
- The callback stores the raw TestRecord on ``_last_record`` and a
  structured summary on ``_captured_record``.
- All phases in the record are captured (no phase is dropped).
- Measurements within each phase are captured with value, outcome, units.

The fake ``htf.Test`` built here mimics OpenHTF's callback contract:
``add_output_callbacks(fn)`` registers a callback, and ``execute()`` invokes
every registered callback with the TestRecord once execution "completes".
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from ate_platform.openhtf.step_executor import OpenHTFStepExecutor


class _FakeTest:
    """Minimal stand-in for openhtf.Test that fires output callbacks.

    OpenHTF's real ``htf.Test.add_output_callbacks(fn)`` registers a callback
    and ``test.execute()`` invokes each registered callback with the final
    TestRecord. This fake replicates that contract so the executor's callback
    wiring can be exercised without a real OpenHTF run.
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
    marginal: bool | None = None,
) -> SimpleNamespace:
    """Build a fake PhaseRecord with the attributes the executor reads."""
    return SimpleNamespace(
        name=name,
        outcome=SimpleNamespace(name=outcome_name),
        marginal=marginal,
        start_time_millis=1000,
        end_time_millis=2000,
        measurements=measurements,
        attachments={},
    )


def _make_test_record(
    phases: list[SimpleNamespace],
    outcome_name: str = "PASS",
    dut_id: str = "DUT-001",
) -> SimpleNamespace:
    """Build a fake TestRecord with the full field set the executor reads."""
    return SimpleNamespace(
        dut_id=dut_id,
        station_id="STATION-01",
        start_time_millis=1000,
        end_time_millis=3000,
        outcome=SimpleNamespace(name=outcome_name),
        outcome_details=[
            SimpleNamespace(code="INFO", description="all good"),
        ],
        metadata={"version": "1.0", "operator": "alice"},
        phases=phases,
        marginal=False,
    )


def _executor_with_record(record: Any) -> OpenHTFStepExecutor:
    """Build an executor and drive one execution that fires ``record``.

    Patches ``import_module`` so the executor discovers a ``_FakeTest`` that
    fires the supplied TestRecord to all registered output callbacks when
    ``execute()`` is called.
    """
    executor = OpenHTFStepExecutor()
    mock_module = SimpleNamespace(test=_FakeTest(record))
    with patch(
        "ate_platform.openhtf.step_executor.import_module",
        return_value=mock_module,
    ):
        executor.execute("tests.openhtf.fake_module", {})
    return executor


class TestTestRecordCapture:
    """Verify the output callback captures the full TestRecord."""

    def test_callback_captures_test_record(self) -> None:
        """The callback stores the raw record and a structured summary.

        Given: a TestRecord with one phase and known top-level fields.
        When: ``execute()`` runs and the output callback fires.
        Then: ``_last_record`` is the raw TestRecord and ``_captured_record``
            mirrors every top-level field (dut_id, station_id, timing,
            outcome, outcome_details, metadata, marginal).
        """
        phase = _make_phase("power_on", {})
        record = _make_test_record([phase], outcome_name="PASS", dut_id="DUT-42")

        executor = _executor_with_record(record)

        # Raw record preserved verbatim for Todos 20/21.
        assert executor._last_record is record

        captured = executor._captured_record
        assert captured is not None
        assert captured["dut_id"] == "DUT-42"
        assert captured["station_id"] == "STATION-01"
        assert captured["start_time_millis"] == 1000
        assert captured["end_time_millis"] == 3000
        assert captured["outcome_name"] == "PASS"
        assert captured["outcome"] is record.outcome
        assert captured["marginal"] is False
        assert captured["metadata"] == {"version": "1.0", "operator": "alice"}
        # outcome_details are extracted into plain dicts.
        assert captured["outcome_details"] == [
            {"code": "INFO", "description": "all good"},
        ]
        # phases list is present (content covered by the next two tests).
        assert "phases" in captured

    def test_captures_all_phases(self) -> None:
        """Every PhaseRecord in the TestRecord is captured, in order.

        Given: a TestRecord with 3 phases (setup, measure, teardown).
        When: the output callback fires.
        Then: ``captured["phases"]`` has 3 entries whose names and outcomes
            match the input phases, preserving order.
        """
        phases = [
            _make_phase("setup", {}, outcome_name="PASS"),
            _make_phase("measure", {}, outcome_name="PASS"),
            _make_phase("teardown", {}, outcome_name="PASS"),
        ]
        record = _make_test_record(phases)

        executor = _executor_with_record(record)
        captured = executor._captured_record
        assert captured is not None

        captured_phases = captured["phases"]
        assert len(captured_phases) == 3
        assert [p["name"] for p in captured_phases] == ["setup", "measure", "teardown"]
        assert [p["outcome_name"] for p in captured_phases] == ["PASS", "PASS", "PASS"]
        # Timing fields are forwarded.
        for p in captured_phases:
            assert p["start_time_millis"] == 1000
            assert p["end_time_millis"] == 2000
            assert p["marginal"] is None
            assert p["attachment_names"] == []

    def test_captures_measurements(self) -> None:
        """Measurements inside each phase are captured with value/outcome/units.

        Given: a TestRecord with two phases, each carrying measurements
            (voltage, current, frequency) with distinct values and units.
        When: the output callback fires.
        Then: every measurement is present under its phase's ``measurements``
            dict, with ``value``, ``outcome_name``, and ``units`` preserved.
            No measurement data is lost.
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
        record = _make_test_record([phase_a, phase_b])

        executor = _executor_with_record(record)
        captured = executor._captured_record
        assert captured is not None

        captured_phases = captured["phases"]
        assert len(captured_phases) == 2

        # Phase A measurements.
        phase_a_meas = captured_phases[0]["measurements"]
        assert set(phase_a_meas.keys()) == {"voltage", "current"}
        assert phase_a_meas["voltage"]["value"] == 5.0
        assert phase_a_meas["voltage"]["units"] == "volts"
        assert phase_a_meas["voltage"]["outcome_name"] == "PASS"
        assert phase_a_meas["current"]["value"] == 0.12
        assert phase_a_meas["current"]["units"] == "amps"
        assert phase_a_meas["current"]["outcome_name"] == "PASS"

        # Phase B measurements.
        phase_b_meas = captured_phases[1]["measurements"]
        assert set(phase_b_meas.keys()) == {"frequency"}
        assert phase_b_meas["frequency"]["value"] == 16_000_000
        assert phase_b_meas["frequency"]["units"] == "Hz"
        assert phase_b_meas["frequency"]["outcome_name"] == "PASS"
