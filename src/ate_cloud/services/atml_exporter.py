"""ATML (IEEE 1636.1) TestResults XML exporter.

Generates well-formed XML conforming to the IEEE 1636.1 TestResults schema
structure using ``xml.etree.ElementTree`` from the standard library (no
external dependencies).

The XML document contains:
- ``TestResults`` root element with ATML namespace.
- ``TestStation`` — station metadata from measurement records.
- ``UUT`` — unit-under-test identification (serial, product type).
- ``Session`` — execution timing and operator context.
- ``Outcome`` — overall Pass/Fail/Abort verdict.
- ``TestSteps`` — per-measurement test steps with limits and outcomes.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime

from ate_cloud.models.execution import Execution
from ate_cloud.models.measurement import Measurement

ATML_NS = "urn:IEEE-1636.1:2012:TestResults"
ATML_PREFIX = f"{{{ATML_NS}}}"

# Register the namespace prefix so ElementTree emits ``xmlns:tr="..."`` on
# the root element and uses ``tr:`` in serialized output.
ET.register_namespace("tr", ATML_NS)


def _fmt_dt(dt: datetime | None) -> str:
    """Format a datetime as ISO-8601, returning empty string for None."""
    if dt is None:
        return ""
    return dt.isoformat()


def _fmt_value(value: float | None) -> str:
    """Format a numeric value for XML text, returning empty for None."""
    if value is None:
        return ""
    return repr(value)


def _verdict_from_status(status: str) -> str:
    """Map execution status to an ATML outcome verdict.

    Args:
        status: Execution status string (PENDING, RUNNING, COMPLETED, FAILED, ABORTED).

    Returns:
        ATML verdict: ``Passed``, ``Failed``, ``Aborted``, or ``Inconclusive``.
    """
    mapping = {
        "COMPLETED": "Passed",
        "FAILED": "Failed",
        "ABORTED": "Aborted",
    }
    return mapping.get(status, "Inconclusive")


def _measurement_verdict(outcome: str) -> str:
    """Map a measurement outcome string to an ATML verdict."""
    mapping = {
        "PASS": "Passed",
        "FAIL": "Failed",
        "WARNING": "Inconclusive",
    }
    return mapping.get(outcome, "Inconclusive")


def _first_or(
    measurements: list[Measurement],
    extractor: Callable[[Measurement], str | None],
    default: str,
) -> str:
    """Extract the first non-None value from measurements, or ``default``.

    Args:
        measurements: List of measurement records.
        extractor: Callable that extracts a value from a Measurement.
        default: Fallback value if no measurement provides a value.
    """
    for m in measurements:
        val = extractor(m)
        if val:
            return val
    return default


def _extract_operator(execution: Execution) -> str:
    """Extract operator name from execution config, if present."""
    if execution.config and isinstance(execution.config, dict):
        operator = execution.config.get("operator")
        if operator and isinstance(operator, str):
            return operator
    return "unknown"


def _compute_verdict(
    execution: Execution, measurements: list[Measurement]
) -> str:
    """Compute the overall ATML verdict from execution status and measurements.

    If any measurement has outcome ``FAIL``, the verdict is ``Failed``
    regardless of execution status. Otherwise, the verdict is derived from
    execution status.
    """
    if any(m.outcome == "FAIL" for m in measurements):
        return "Failed"
    return _verdict_from_status(execution.status)


class ATMLExporter:
    """Export execution + measurement data to ATML TestResults XML.

    Uses ``xml.etree.ElementTree`` (stdlib) for XML generation — no external
    dependencies required.
    """

    def generate_atml(
        self,
        execution: Execution,
        measurements: list[Measurement],
    ) -> str:
        """Generate ATML TestResults XML for a single execution.

        Args:
            execution: The execution record.
            measurements: List of measurement records associated with the
                execution. May be empty — ``TestSteps`` will be empty.

        Returns:
            Pretty-printed XML string with ATML namespace declaration.
        """
        root = ET.Element(f"{ATML_PREFIX}TestResults")

        self._add_test_station(root, measurements)
        self._add_uut(root, measurements)
        self._add_session(root, execution)
        self._add_outcome(root, execution, measurements)
        self._add_test_steps(root, measurements)

        ET.indent(root, space="  ")
        body = ET.tostring(root, encoding="unicode")
        return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}'

    def _add_test_station(
        self, parent: ET.Element, measurements: list[Measurement]
    ) -> None:
        """Add ``TestStation`` element with station metadata."""
        station = ET.SubElement(parent, f"{ATML_PREFIX}TestStation")
        station_id = _first_or(measurements, lambda m: m.station_ref, "unknown")
        ET.SubElement(station, f"{ATML_PREFIX}stationId").text = station_id
        ET.SubElement(station, f"{ATML_PREFIX}stationName").text = station_id
        ET.SubElement(station, f"{ATML_PREFIX}stationType").text = "TestStation"

    def _add_uut(
        self, parent: ET.Element, measurements: list[Measurement]
    ) -> None:
        """Add ``UUT`` element with unit-under-test identification."""
        uut = ET.SubElement(parent, f"{ATML_PREFIX}UUT")
        serial = _first_or(measurements, lambda m: m.dut_serial, "unknown")
        product = _first_or(measurements, lambda m: m.product_ref, "unknown")
        ET.SubElement(uut, f"{ATML_PREFIX}serialNumber").text = serial
        ET.SubElement(uut, f"{ATML_PREFIX}productType").text = product
        ET.SubElement(uut, f"{ATML_PREFIX}partNumber").text = product

    def _add_session(self, parent: ET.Element, execution: Execution) -> None:
        """Add ``Session`` element with execution timing and operator."""
        session = ET.SubElement(parent, f"{ATML_PREFIX}Session")
        ET.SubElement(session, f"{ATML_PREFIX}sessionId").text = execution.id
        ET.SubElement(
            session, f"{ATML_PREFIX}startDateTime"
        ).text = _fmt_dt(execution.started_at)
        ET.SubElement(
            session, f"{ATML_PREFIX}endDateTime"
        ).text = _fmt_dt(execution.completed_at)
        operator = _extract_operator(execution)
        ET.SubElement(session, f"{ATML_PREFIX}operator").text = operator

    def _add_outcome(
        self,
        parent: ET.Element,
        execution: Execution,
        measurements: list[Measurement],
    ) -> None:
        """Add ``Outcome`` element with overall verdict and descriptive text."""
        outcome = ET.SubElement(parent, f"{ATML_PREFIX}Outcome")
        verdict = _compute_verdict(execution, measurements)
        ET.SubElement(outcome, f"{ATML_PREFIX}verdict").text = verdict
        text_parts: list[str] = [f"Execution status: {execution.status}"]
        if execution.error:
            text_parts.append(f"Error: {execution.error}")
        if measurements:
            fail_count = sum(1 for m in measurements if m.outcome == "FAIL")
            pass_count = sum(1 for m in measurements if m.outcome == "PASS")
            text_parts.append(
                f"Measurements: {pass_count} passed, {fail_count} failed, "
                f"{len(measurements)} total"
            )
        ET.SubElement(
            outcome, f"{ATML_PREFIX}outcomeText"
        ).text = "; ".join(text_parts)

    def _add_test_steps(
        self, parent: ET.Element, measurements: list[Measurement]
    ) -> None:
        """Add ``TestSteps`` element with per-measurement step details."""
        steps = ET.SubElement(parent, f"{ATML_PREFIX}TestSteps")
        for idx, m in enumerate(measurements, start=1):
            step = ET.SubElement(steps, f"{ATML_PREFIX}TestStep")
            ET.SubElement(step, f"{ATML_PREFIX}stepId").text = str(idx)
            ET.SubElement(step, f"{ATML_PREFIX}name").text = m.name
            step_outcome = ET.SubElement(step, f"{ATML_PREFIX}outcome")
            ET.SubElement(
                step_outcome, f"{ATML_PREFIX}verdict"
            ).text = _measurement_verdict(m.outcome)
            meas = ET.SubElement(step, f"{ATML_PREFIX}Measurement")
            ET.SubElement(
                meas, f"{ATML_PREFIX}value"
            ).text = _fmt_value(m.value)
            ET.SubElement(meas, f"{ATML_PREFIX}unit").text = m.unit or ""
            if m.limits_min is not None:
                ET.SubElement(
                    meas, f"{ATML_PREFIX}limitsMin"
                ).text = _fmt_value(m.limits_min)
            if m.limits_max is not None:
                ET.SubElement(
                    meas, f"{ATML_PREFIX}limitsMax"
                ).text = _fmt_value(m.limits_max)
            ET.SubElement(
                meas, f"{ATML_PREFIX}timestamp"
            ).text = _fmt_dt(m.timestamp)


__all__ = ["ATMLExporter"]
