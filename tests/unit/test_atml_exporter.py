"""Unit tests for ATML (IEEE 1636.1) TestResults XML exporter.

Tests verify:
- XML is well-formed (parseable by ElementTree).
- Root element is ``TestResults`` with ATML namespace.
- Required child elements exist: TestStation, UUT, Session, Outcome, TestSteps.
- TestStation contains stationId, stationName, stationType.
- UUT contains serialNumber, productType, partNumber.
- Session contains sessionId, startDateTime, endDateTime, operator.
- Outcome contains verdict (Passed/Failed/Aborted/Inconclusive) and outcomeText.
- TestSteps contain stepId, name, outcome, Measurement with value/unit/limits.
- Empty measurements produce empty TestSteps.
- Verdict logic: any FAIL measurement → Failed verdict.
- Operator extraction from execution config.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from ate_cloud.models.execution import Execution
from ate_cloud.models.measurement import Measurement
from ate_cloud.services.atml_exporter import ATML_NS, ATMLExporter

NS = {"tr": ATML_NS}


def _make_execution(
    status: str = "COMPLETED",
    error: str | None = None,
    config: dict | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> Execution:
    """Create a test Execution instance."""
    return Execution(
        id="exec-001",
        sequence_id="seq-001",
        status=status,
        config=config,
        result=None,
        error=error,
        started_at=started_at or datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC),
        completed_at=completed_at or datetime(2026, 1, 15, 10, 35, 0, tzinfo=UTC),
    )


def _make_measurement(
    name: str = "voltage_3v3",
    value: float = 3.3,
    outcome: str = "PASS",
    limits_min: float | None = 3.2,
    limits_max: float | None = 3.4,
    unit: str = "V",
    station_ref: str = "station-A",
    product_ref: str = "comm_module_v2",
    dut_serial: str = "SN-12345",
) -> Measurement:
    """Create a test Measurement instance."""
    return Measurement(
        measurement_id="meas-001",
        execution_ref="exec-001",
        station_ref=station_ref,
        product_ref=product_ref,
        dut_serial=dut_serial,
        timestamp=datetime(2026, 1, 15, 10, 32, 0, tzinfo=UTC),
        name=name,
        value=value,
        limits_min=limits_min,
        limits_max=limits_max,
        unit=unit,
        outcome=outcome,
    )


class TestATMLStructure:
    """Tests for ATML XML document structure."""

    def test_xml_is_well_formed(self) -> None:
        """Generated XML parses without error."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [_make_measurement()])
        ET.fromstring(xml)  # raises ParseError if not well-formed

    def test_xml_declaration_present(self) -> None:
        """XML starts with ``<?xml version="1.0" encoding="UTF-8"?>``."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [])
        assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')

    def test_root_element_is_test_results(self) -> None:
        """Root element is ``TestResults`` in the ATML namespace."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [])
        root = ET.fromstring(xml)
        assert root.tag == f"{{{ATML_NS}}}TestResults"

    def test_namespace_declaration_on_root(self) -> None:
        """Root element declares the ATML namespace with prefix ``tr``."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [])
        # ElementTree collapses known namespaces; the tag prefix is "tr"
        # because we registered it via register_namespace.
        assert "tr:" in xml or ATML_NS in xml

    def test_test_station_exists(self) -> None:
        """TestStation element exists as a child of TestResults."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [_make_measurement()])
        root = ET.fromstring(xml)
        station = root.find("tr:TestStation", NS)
        assert station is not None

    def test_uut_exists(self) -> None:
        """UUT element exists as a child of TestResults."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [_make_measurement()])
        root = ET.fromstring(xml)
        uut = root.find("tr:UUT", NS)
        assert uut is not None

    def test_session_exists(self) -> None:
        """Session element exists as a child of TestResults."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [])
        root = ET.fromstring(xml)
        session = root.find("tr:Session", NS)
        assert session is not None

    def test_outcome_exists(self) -> None:
        """Outcome element exists as a child of TestResults."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [])
        root = ET.fromstring(xml)
        outcome = root.find("tr:Outcome", NS)
        assert outcome is not None

    def test_test_steps_exists(self) -> None:
        """TestSteps element exists as a child of TestResults."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [])
        root = ET.fromstring(xml)
        steps = root.find("tr:TestSteps", NS)
        assert steps is not None


class TestTestStation:
    """Tests for TestStation element content."""

    def test_station_id_from_measurement(self) -> None:
        """stationId is populated from measurement's station_ref."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(), [_make_measurement(station_ref="station-X")]
        )
        root = ET.fromstring(xml)
        station = root.find("tr:TestStation", NS)
        assert station is not None
        sid = station.find("tr:stationId", NS)
        assert sid is not None
        assert sid.text == "station-X"

    def test_station_name_matches_id(self) -> None:
        """stationName mirrors stationId value."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(), [_make_measurement(station_ref="station-Y")]
        )
        root = ET.fromstring(xml)
        station = root.find("tr:TestStation", NS)
        assert station is not None
        name = station.find("tr:stationName", NS)
        assert name is not None
        assert name.text == "station-Y"

    def test_station_type_is_test_station(self) -> None:
        """stationType is always 'TestStation'."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [_make_measurement()])
        root = ET.fromstring(xml)
        station = root.find("tr:TestStation", NS)
        assert station is not None
        stype = station.find("tr:stationType", NS)
        assert stype is not None
        assert stype.text == "TestStation"

    def test_station_id_defaults_to_unknown_when_no_measurements(self) -> None:
        """stationId is 'unknown' when no measurements are available."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [])
        root = ET.fromstring(xml)
        station = root.find("tr:TestStation", NS)
        assert station is not None
        sid = station.find("tr:stationId", NS)
        assert sid is not None
        assert sid.text == "unknown"


class TestUUT:
    """Tests for UUT element content."""

    def test_serial_number_from_measurement(self) -> None:
        """serialNumber is populated from measurement's dut_serial."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(), [_make_measurement(dut_serial="SN-99999")]
        )
        root = ET.fromstring(xml)
        uut = root.find("tr:UUT", NS)
        assert uut is not None
        serial = uut.find("tr:serialNumber", NS)
        assert serial is not None
        assert serial.text == "SN-99999"

    def test_product_type_from_measurement(self) -> None:
        """productType is populated from measurement's product_ref."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(), [_make_measurement(product_ref="widget_pro")]
        )
        root = ET.fromstring(xml)
        uut = root.find("tr:UUT", NS)
        assert uut is not None
        ptype = uut.find("tr:productType", NS)
        assert ptype is not None
        assert ptype.text == "widget_pro"

    def test_part_number_matches_product_type(self) -> None:
        """partNumber mirrors productType value."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(), [_make_measurement(product_ref="board_v3")]
        )
        root = ET.fromstring(xml)
        uut = root.find("tr:UUT", NS)
        assert uut is not None
        pn = uut.find("tr:partNumber", NS)
        assert pn is not None
        assert pn.text == "board_v3"


class TestSession:
    """Tests for Session element content."""

    def test_session_id_matches_execution_id(self) -> None:
        """sessionId matches execution.id."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [])
        root = ET.fromstring(xml)
        session = root.find("tr:Session", NS)
        assert session is not None
        sid = session.find("tr:sessionId", NS)
        assert sid is not None
        assert sid.text == "exec-001"

    def test_start_datetime_populated(self) -> None:
        """startDateTime is populated from execution.started_at."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [])
        root = ET.fromstring(xml)
        session = root.find("tr:Session", NS)
        assert session is not None
        sdt = session.find("tr:startDateTime", NS)
        assert sdt is not None
        assert "2026-01-15" in sdt.text

    def test_end_datetime_populated(self) -> None:
        """endDateTime is populated from execution.completed_at."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [])
        root = ET.fromstring(xml)
        session = root.find("tr:Session", NS)
        assert session is not None
        edt = session.find("tr:endDateTime", NS)
        assert edt is not None
        assert "2026-01-15" in edt.text

    def test_operator_from_config(self) -> None:
        """operator is extracted from execution.config['operator']."""
        exporter = ATMLExporter()
        exec_with_op = _make_execution(config={"operator": "alice"})
        xml = exporter.generate_atml(exec_with_op, [])
        root = ET.fromstring(xml)
        session = root.find("tr:Session", NS)
        assert session is not None
        op = session.find("tr:operator", NS)
        assert op is not None
        assert op.text == "alice"

    def test_operator_defaults_to_unknown(self) -> None:
        """operator is 'unknown' when config has no operator key."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [])
        root = ET.fromstring(xml)
        session = root.find("tr:Session", NS)
        assert session is not None
        op = session.find("tr:operator", NS)
        assert op is not None
        assert op.text == "unknown"


class TestOutcome:
    """Tests for Outcome element verdict logic."""

    def test_verdict_passed_for_completed(self) -> None:
        """Verdict is 'Passed' for COMPLETED status with all PASS measurements."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(status="COMPLETED"), [_make_measurement(outcome="PASS")]
        )
        root = ET.fromstring(xml)
        outcome = root.find("tr:Outcome", NS)
        assert outcome is not None
        verdict = outcome.find("tr:verdict", NS)
        assert verdict is not None
        assert verdict.text == "Passed"

    def test_verdict_failed_for_failed_status(self) -> None:
        """Verdict is 'Failed' for FAILED status."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(status="FAILED"), [_make_measurement(outcome="PASS")]
        )
        root = ET.fromstring(xml)
        outcome = root.find("tr:Outcome", NS)
        assert outcome is not None
        verdict = outcome.find("tr:verdict", NS)
        assert verdict is not None
        assert verdict.text == "Failed"

    def test_verdict_aborted_for_aborted_status(self) -> None:
        """Verdict is 'Aborted' for ABORTED status."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(status="ABORTED"), [_make_measurement(outcome="PASS")]
        )
        root = ET.fromstring(xml)
        outcome = root.find("tr:Outcome", NS)
        assert outcome is not None
        verdict = outcome.find("tr:verdict", NS)
        assert verdict is not None
        assert verdict.text == "Aborted"

    def test_verdict_failed_when_any_measurement_fails(self) -> None:
        """Verdict is 'Failed' if any measurement has outcome FAIL, even if
        execution status is COMPLETED."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(status="COMPLETED"), [_make_measurement(outcome="FAIL")]
        )
        root = ET.fromstring(xml)
        outcome = root.find("tr:Outcome", NS)
        assert outcome is not None
        verdict = outcome.find("tr:verdict", NS)
        assert verdict is not None
        assert verdict.text == "Failed"

    def test_verdict_inconclusive_for_pending(self) -> None:
        """Verdict is 'Inconclusive' for PENDING status."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(status="PENDING"), []
        )
        root = ET.fromstring(xml)
        outcome = root.find("tr:Outcome", NS)
        assert outcome is not None
        verdict = outcome.find("tr:verdict", NS)
        assert verdict is not None
        assert verdict.text == "Inconclusive"

    def test_outcome_text_contains_status(self) -> None:
        """outcomeText includes the execution status."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(status="COMPLETED"), [])
        root = ET.fromstring(xml)
        outcome = root.find("tr:Outcome", NS)
        assert outcome is not None
        text = outcome.find("tr:outcomeText", NS)
        assert text is not None
        assert "COMPLETED" in text.text

    def test_outcome_text_contains_error(self) -> None:
        """outcomeText includes error message when present."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(status="FAILED", error="Connection timeout"), []
        )
        root = ET.fromstring(xml)
        outcome = root.find("tr:Outcome", NS)
        assert outcome is not None
        text = outcome.find("tr:outcomeText", NS)
        assert text is not None
        assert "Connection timeout" in text.text

    def test_outcome_text_contains_measurement_summary(self) -> None:
        """outcomeText includes pass/fail counts when measurements exist."""
        exporter = ATMLExporter()
        measurements = [
            _make_measurement(name="m1", outcome="PASS"),
            _make_measurement(name="m2", outcome="PASS"),
            _make_measurement(name="m3", outcome="FAIL"),
        ]
        xml = exporter.generate_atml(_make_execution(), measurements)
        root = ET.fromstring(xml)
        outcome = root.find("tr:Outcome", NS)
        assert outcome is not None
        text = outcome.find("tr:outcomeText", NS)
        assert text is not None
        assert "2 passed" in text.text
        assert "1 failed" in text.text
        assert "3 total" in text.text


class TestTestSteps:
    """Tests for TestSteps element content."""

    def test_empty_measurements_produce_empty_test_steps(self) -> None:
        """No measurements → TestSteps element exists but has no children."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [])
        root = ET.fromstring(xml)
        steps = root.find("tr:TestSteps", NS)
        assert steps is not None
        step_list = steps.findall("tr:TestStep", NS)
        assert len(step_list) == 0

    def test_step_count_matches_measurement_count(self) -> None:
        """Number of TestStep elements equals number of measurements."""
        exporter = ATMLExporter()
        measurements = [
            _make_measurement(name="m1"),
            _make_measurement(name="m2"),
            _make_measurement(name="m3"),
        ]
        xml = exporter.generate_atml(_make_execution(), measurements)
        root = ET.fromstring(xml)
        steps = root.find("tr:TestSteps", NS)
        assert steps is not None
        step_list = steps.findall("tr:TestStep", NS)
        assert len(step_list) == 3

    def test_step_has_step_id(self) -> None:
        """Each TestStep has a stepId element."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [_make_measurement()])
        root = ET.fromstring(xml)
        steps = root.find("tr:TestSteps", NS)
        assert steps is not None
        step = steps.find("tr:TestStep", NS)
        assert step is not None
        sid = step.find("tr:stepId", NS)
        assert sid is not None
        assert sid.text == "1"

    def test_step_has_name(self) -> None:
        """Each TestStep has a name element matching the measurement name."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(), [_make_measurement(name="current_5v")]
        )
        root = ET.fromstring(xml)
        steps = root.find("tr:TestSteps", NS)
        assert steps is not None
        step = steps.find("tr:TestStep", NS)
        assert step is not None
        name = step.find("tr:name", NS)
        assert name is not None
        assert name.text == "current_5v"

    def test_step_has_outcome_verdict(self) -> None:
        """Each TestStep has an outcome with a verdict."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(), [_make_measurement(outcome="PASS")]
        )
        root = ET.fromstring(xml)
        steps = root.find("tr:TestSteps", NS)
        assert steps is not None
        step = steps.find("tr:TestStep", NS)
        assert step is not None
        outcome = step.find("tr:outcome", NS)
        assert outcome is not None
        verdict = outcome.find("tr:verdict", NS)
        assert verdict is not None
        assert verdict.text == "Passed"

    def test_step_has_measurement_with_value(self) -> None:
        """Each TestStep has a Measurement element with a value."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(), [_make_measurement(value=3.3)]
        )
        root = ET.fromstring(xml)
        steps = root.find("tr:TestSteps", NS)
        assert steps is not None
        step = steps.find("tr:TestStep", NS)
        assert step is not None
        meas = step.find("tr:Measurement", NS)
        assert meas is not None
        value = meas.find("tr:value", NS)
        assert value is not None
        assert "3.3" in value.text

    def test_step_has_measurement_with_unit(self) -> None:
        """Each TestStep has a Measurement element with a unit."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(), [_make_measurement(unit="A")]
        )
        root = ET.fromstring(xml)
        steps = root.find("tr:TestSteps", NS)
        assert steps is not None
        step = steps.find("tr:TestStep", NS)
        assert step is not None
        meas = step.find("tr:Measurement", NS)
        assert meas is not None
        unit = meas.find("tr:unit", NS)
        assert unit is not None
        assert unit.text == "A"

    def test_step_has_limits_when_present(self) -> None:
        """TestStep Measurement includes limitsMin/limitsMax when set."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(),
            [_make_measurement(limits_min=3.2, limits_max=3.4)],
        )
        root = ET.fromstring(xml)
        steps = root.find("tr:TestSteps", NS)
        assert steps is not None
        step = steps.find("tr:TestStep", NS)
        assert step is not None
        meas = step.find("tr:Measurement", NS)
        assert meas is not None
        lmin = meas.find("tr:limitsMin", NS)
        lmax = meas.find("tr:limitsMax", NS)
        assert lmin is not None
        assert lmax is not None
        assert "3.2" in lmin.text
        assert "3.4" in lmax.text

    def test_step_omits_limits_when_absent(self) -> None:
        """TestStep Measurement omits limitsMin/limitsMax when None."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(
            _make_execution(),
            [_make_measurement(limits_min=None, limits_max=None)],
        )
        root = ET.fromstring(xml)
        steps = root.find("tr:TestSteps", NS)
        assert steps is not None
        step = steps.find("tr:TestStep", NS)
        assert step is not None
        meas = step.find("tr:Measurement", NS)
        assert meas is not None
        assert meas.find("tr:limitsMin", NS) is None
        assert meas.find("tr:limitsMax", NS) is None

    def test_step_has_timestamp(self) -> None:
        """Each TestStep Measurement has a timestamp."""
        exporter = ATMLExporter()
        xml = exporter.generate_atml(_make_execution(), [_make_measurement()])
        root = ET.fromstring(xml)
        steps = root.find("tr:TestSteps", NS)
        assert steps is not None
        step = steps.find("tr:TestStep", NS)
        assert step is not None
        meas = step.find("tr:Measurement", NS)
        assert meas is not None
        ts = meas.find("tr:timestamp", NS)
        assert ts is not None
        assert "2026-01-15" in ts.text


class TestMultipleMeasurements:
    """Tests with multiple measurements from different stations/DUTs."""

    def test_station_id_from_first_measurement(self) -> None:
        """stationId uses the first measurement's station_ref."""
        exporter = ATMLExporter()
        measurements = [
            _make_measurement(station_ref="station-A"),
            _make_measurement(station_ref="station-B"),
        ]
        xml = exporter.generate_atml(_make_execution(), measurements)
        root = ET.fromstring(xml)
        station = root.find("tr:TestStation", NS)
        assert station is not None
        sid = station.find("tr:stationId", NS)
        assert sid is not None
        assert sid.text == "station-A"

    def test_step_ids_are_sequential(self) -> None:
        """stepId values are sequential integers starting from 1."""
        exporter = ATMLExporter()
        measurements = [
            _make_measurement(name="m1"),
            _make_measurement(name="m2"),
            _make_measurement(name="m3"),
        ]
        xml = exporter.generate_atml(_make_execution(), measurements)
        root = ET.fromstring(xml)
        steps = root.find("tr:TestSteps", NS)
        assert steps is not None
        step_list = steps.findall("tr:TestStep", NS)
        ids = [
            s.find("tr:stepId", NS).text  # type: ignore[union-attr]
            for s in step_list
        ]
        assert ids == ["1", "2", "3"]
