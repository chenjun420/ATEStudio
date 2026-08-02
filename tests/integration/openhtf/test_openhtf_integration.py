"""Integration tests for OpenHTFStepExecutor with a real htf.Test.

These tests use the real OpenHTF library (openhtf>=1.6.0) -- no mocking of
``htf.Test``, ``htf.PhaseOptions``, or ``htf.measures``. A real OpenHTF
test module lives in ``fixtures/sample_test_module.py`` and is imported
dynamically by ``OpenHTFStepExecutor`` via ``importlib.import_module``.

What is verified end-to-end:
    - A real ``htf.Test`` with ``@htf.PhaseOptions`` / ``@htf.measures``
      phases executes via ``OpenHTFStepExecutor.execute`` / ``execute_async``.
    - The output callback captures a real ``TestRecord``.
    - ``_OUTCOME_MAP`` maps PASS -> PASSED and FAIL -> FAILED correctly
      against real OpenHTF outcomes.
    - ``as_base_types()`` produces a JSON-serializable dict from the real
      TestRecord.

Known limitation (pre-existing, NOT introduced by these tests):
    ``OpenHTFStepExecutor._extract_measurement`` and
    ``serialization._serialize_measurement`` both read the measurement value
    via ``getattr(meas, "value", None)``. Real OpenHTF ``Measurement``
    objects do not expose ``.value`` directly (the value lives at
    ``measured_value.value``), so the extracted value is ``None``. These
    integration tests assert on measurement KEY presence and outcome, not on
    extracted values. A follow-up fix to the source (outside this test-only
    todo's scope) should read ``measured_value`` instead.

Todo 22 (spawn context) is not yet implemented -- ``execute_async`` wraps
``execute`` via ``asyncio.to_thread`` (in-process). These tests verify the
current in-process behavior; spawn-context isolation will be tested when
Todo 22 lands.
"""

from __future__ import annotations

import json
from typing import Any

import openhtf

from ate_platform.openhtf import OpenHTFStepExecutor, as_base_types
from ate_platform.types import StepStatus

# Dotted module path to the real OpenHTF test module in fixtures/.
_SAMPLE_MODULE = "tests.integration.openhtf.fixtures.sample_test_module"
_DUT_ID = "TEST_DUT_001"


class TestRealOpenHTFExecution:
    """Integration tests that execute a real htf.Test via OpenHTFStepExecutor."""

    def test_real_openhtf_test_passes(self) -> None:
        """A passing real htf.Test yields StepStatus.PASSED with measurement keys.

        Given: a real OpenHTF test module with setup + measure_power phases
            that take in-range measurements (voltage=5.0V, current=0.12A).
        When: executed via OpenHTFStepExecutor.execute.
        Then: StepResult.status is PASSED, error is None, and the measurement
            names ("voltage", "current") appear as keys in outputs.
        """
        executor = OpenHTFStepExecutor()
        result = executor.execute(_SAMPLE_MODULE, {"fail": False, "dut_id": _DUT_ID})

        assert result.status is StepStatus.PASSED
        assert result.error is None
        # Measurement keys are collected into outputs by the executor.
        assert "voltage" in result.outputs
        assert "current" in result.outputs
        # Meta fields are present.
        assert result.outputs["outcome"] == "PASS"
        assert result.outputs["dut_id"] == _DUT_ID

    def test_real_openhtf_test_captures_record(self) -> None:
        """The output callback captures a structured TestRecord summary.

        Given: a real OpenHTF test module executed with passing phases.
        When: execution completes and _on_test_complete fires.
        Then: _captured_record is populated with outcome_name="PASS", a
            non-empty phases list (trigger + setup + measure), and
            measurement dicts keyed by name.
        """
        executor = OpenHTFStepExecutor()
        executor.execute(_SAMPLE_MODULE, {"fail": False, "dut_id": _DUT_ID})

        captured = executor._captured_record
        assert captured is not None
        assert captured["outcome_name"] == "PASS"
        assert captured["dut_id"] == _DUT_ID

        # Real OpenHTF adds a trigger phase (from test_start lambda) plus
        # the two phases from create_test (setup, measure_power).
        phases = captured["phases"]
        assert len(phases) >= 2

        phase_names = {p["name"] for p in phases}
        assert "setup" in phase_names
        assert "measure_power" in phase_names

        # The measure_power phase has voltage and current measurements.
        measure_phase = next(p for p in phases if p["name"] == "measure_power")
        assert "voltage" in measure_phase["measurements"]
        assert "current" in measure_phase["measurements"]

        # _last_record retains the raw TestRecord object (for as_base_types).
        assert executor._last_record is not None

    def test_real_openhtf_test_fails(self) -> None:
        """A failing real htf.Test yields StepStatus.FAILED via _OUTCOME_MAP.

        Given: a real OpenHTF test module with a measure_fail phase that
            sets voltage=6.0V (out of [4.5, 5.5] range).
        When: executed via OpenHTFStepExecutor.execute.
        Then: StepResult.status is FAILED (via _OUTCOME_MAP["FAIL"]) and
            error is populated.
        """
        executor = OpenHTFStepExecutor()
        result = executor.execute(_SAMPLE_MODULE, {"fail": True, "dut_id": _DUT_ID})

        assert result.status is StepStatus.FAILED
        assert result.error is not None
        assert result.outputs["outcome"] == "FAIL"

    def test_as_base_types_produces_valid_dict(self) -> None:
        """as_base_types on a real TestRecord produces a JSON-serializable dict.

        Given: a real TestRecord captured from a passing htf.Test execution.
        When: passed to as_base_types().
        Then: the output dict is JSON-serializable (json.dumps does not
            raise), the outcome is "PASS", and phases are serialized with
            their measurements.
        """
        executor = OpenHTFStepExecutor()
        executor.execute(_SAMPLE_MODULE, {"fail": False, "dut_id": _DUT_ID})

        assert executor._last_record is not None
        serialized = as_base_types(executor._last_record)

        # JSON-serializable -- the core contract of as_base_types.
        json.dumps(serialized)

        # Top-level fields mirror the real TestRecord.
        assert serialized["outcome"] == "PASS"
        assert serialized["dut_id"] == _DUT_ID

        # Phases are serialized as a list of dicts.
        assert len(serialized["phases"]) >= 2
        phase_names = {p["name"] for p in serialized["phases"]}
        assert "measure_power" in phase_names

        # The measure_power phase has serialized measurements.
        measure_phase = next(
            p for p in serialized["phases"] if p["name"] == "measure_power"
        )
        assert "voltage" in measure_phase["measurements"]
        assert "current" in measure_phase["measurements"]

    async def test_execute_async_runs_real_test(self) -> None:
        """execute_async wraps execute via asyncio.to_thread for a real test.

        Given: a real OpenHTF test module with passing phases.
        When: executed via OpenHTFStepExecutor.execute_async (awaited).
        Then: StepResult.status is PASSED, confirming the async path
            executes the real htf.Test in a worker thread.
        """
        executor = OpenHTFStepExecutor()
        result = await executor.execute_async(
            _SAMPLE_MODULE, {"fail": False, "dut_id": _DUT_ID}
        )

        assert result.status is StepStatus.PASSED
        assert result.error is None
        assert "voltage" in result.outputs

    def test_openhtf_version_is_at_least_1(self) -> None:
        """The installed openhtf must be version >= 1.0.

        pyproject.toml pins openhtf>=1.6.0; this test guards against an
        accidental downgrade to a 0.x release where the Test/PhaseOptions/
        measures API differs.
        """
        version_str: str = openhtf.__version__
        major = int(version_str.split(".")[0])
        assert major >= 1, f"openhtf version {version_str} is < 1.0"


class TestOutcomeMapWithRealOpenHTF:
    """Verify _OUTCOME_MAP correctness against real OpenHTF outcomes.

    These tests exercise the real OpenHTF Outcome enum values (not
    SimpleNamespace fakes) through the executor's _record_to_result path,
    confirming the mapping table covers the outcomes a real htf.Test
    produces in practice.
    """

    def test_pass_outcome_maps_to_passed(self) -> None:
        """Real PASS outcome maps to StepStatus.PASSED.

        Given: a real htf.Test that passes (in-range measurements).
        When: executed and the TestRecord outcome is PASS.
        Then: StepResult.status is PASSED.
        """
        executor = OpenHTFStepExecutor()
        result = executor.execute(_SAMPLE_MODULE, {"fail": False, "dut_id": _DUT_ID})
        assert result.status is StepStatus.PASSED
        assert executor._captured_record is not None
        assert executor._captured_record["outcome_name"] == "PASS"

    def test_fail_outcome_maps_to_failed(self) -> None:
        """Real FAIL outcome maps to StepStatus.FAILED.

        Given: a real htf.Test that fails (out-of-range measurement).
        When: executed and the TestRecord outcome is FAIL.
        Then: StepResult.status is FAILED.
        """
        executor = OpenHTFStepExecutor()
        result = executor.execute(_SAMPLE_MODULE, {"fail": True, "dut_id": _DUT_ID})
        assert result.status is StepStatus.FAILED
        assert executor._captured_record is not None
        assert executor._captured_record["outcome_name"] == "FAIL"


class TestCapturedRecordStructure:
    """Verify the _captured_record dict shape against a real TestRecord.

    The unit tests (Todo 19) use SimpleNamespace fakes; these tests confirm
    the extraction helpers work correctly against real OpenHTF attrs-based
    TestRecord/PhaseRecord/Measurement objects.
    """

    def test_captured_record_has_all_top_level_fields(self) -> None:
        """_captured_record includes every documented top-level field.

        Given: a real TestRecord captured via the output callback.
        When: _on_test_complete extracts it.
        Then: the dict has dut_id, station_id, start_time_millis,
            end_time_millis, outcome, outcome_name, outcome_details,
            metadata, phases, marginal keys.
        """
        executor = OpenHTFStepExecutor()
        executor.execute(_SAMPLE_MODULE, {"fail": False, "dut_id": _DUT_ID})

        captured: dict[str, Any] = executor._captured_record  # type: ignore[assignment]
        expected_keys = {
            "dut_id",
            "station_id",
            "start_time_millis",
            "end_time_millis",
            "outcome",
            "outcome_name",
            "outcome_details",
            "metadata",
            "phases",
            "marginal",
        }
        assert expected_keys.issubset(captured.keys())

    def test_phase_records_have_measurement_dicts(self) -> None:
        """Each PhaseRecord in _captured_record has a measurements dict.

        Given: a real TestRecord with phases carrying measurements.
        When: extracted by _on_test_complete.
        Then: each phase dict has a "measurements" key whose value is a
            dict (possibly empty for the setup phase).
        """
        executor = OpenHTFStepExecutor()
        executor.execute(_SAMPLE_MODULE, {"fail": False, "dut_id": _DUT_ID})

        captured = executor._captured_record
        assert captured is not None
        for phase in captured["phases"]:
            assert isinstance(phase["measurements"], dict)
            assert isinstance(phase["name"], str)
            assert phase["outcome_name"] is not None
