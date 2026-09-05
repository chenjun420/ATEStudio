"""Pydantic schemas for the test traceability chain (T33).

Defines the response models returned by ``GET /api/v1/trace/{serial_number}``:

- ``TraceInstrument``: one instrument that participated in an execution.
- ``TraceMeasurement``: one measurement captured for the DUT in an execution.
- ``TraceStep``: one execution as a chronological step in the trace chain.
- ``TestTraceResult``: the full rebuilt chain for a DUT serial number.

The W3C PROV JSON-LD projection lives in ``TestTraceService.to_jsonld`` -
these schemas describe the human-facing structure, not the PROV graph.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TraceInstrument(BaseModel):
    """One instrument that participated in an execution for this DUT.

    Attributes:
        instrument_id: Instrument identifier (resource name or VISA address).
    """

    instrument_id: str


class TraceMeasurement(BaseModel):
    """One measurement captured for the DUT inside an execution.

    Attributes:
        measurement_id: Unique measurement identifier (UUID4).
        name: Measurement identifier (e.g. ``"voltage_3v3"``).
        value: Numeric measured value (null when the step produced no value).
        unit: Engineering unit (e.g. ``"V"``, ``"A"``).
        limits_min: Lower acceptance limit (null when unbounded).
        limits_max: Upper acceptance limit (null when unbounded).
        outcome: PASS | FAIL | WARNING verdict.
        timestamp: When the measurement was captured (UTC).
    """

    measurement_id: str
    name: str
    value: float | None = None
    unit: str | None = None
    limits_min: float | None = None
    limits_max: float | None = None
    outcome: str
    timestamp: datetime


class TraceStep(BaseModel):
    """One execution as a chronological step in the trace chain.

    A step groups the execution metadata with the measurements produced
    inside it. Steps are ordered by ``started_at`` ascending.

    Attributes:
        execution_id: The execution run identifier (= Execution.id).
        sequence_id: Reference to the sequence being executed (if any).
        station_id: The station that ran the execution (if recorded).
        status: Terminal execution status (COMPLETED, FAILED, ABORTED, ...).
        started_at: When the execution started running.
        completed_at: When the execution reached a terminal state.
        instruments: Instruments that participated in this execution.
        measurements: Measurements captured for this DUT in this execution.
    """

    execution_id: str
    sequence_id: str | None = None
    station_id: str | None = None
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    instruments: list[TraceInstrument] = Field(default_factory=list)
    measurements: list[TraceMeasurement] = Field(default_factory=list)


class TestTraceResult(BaseModel):
    """The full rebuilt trace chain for a DUT serial number.

    Attributes:
        dut_serial: The DUT serial number that was queried.
        steps: Chronologically ordered execution steps (oldest first).
    """

    # Not a pytest test class — the name starts with "Test" so tell pytest not
    # to collect it (suppresses PytestCollectionWarning "cannot collect").
    __test__ = False

    dut_serial: str
    steps: list[TraceStep] = Field(default_factory=list)
