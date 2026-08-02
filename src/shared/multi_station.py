"""Multi-station orchestration models for ATE Studio.

Defines the data structures used by multi-station test workflows where a DUT
(serial number) flows through a sequence of test stations, each running on a
separate edge worker. Stations coordinate via a NATS JetStream KV handshake:
when a station finishes, it writes a handoff record; the downstream station
watches for that record before starting.

This module is shared between ``ate_platform`` (the StationOrchestrator edge
component) and ``ate_cloud`` (the workflow management API), so it must not
import from either package - only stdlib and the ``shared`` package itself.

Naming conventions (per AGENTS.md and T7 ISA-95 standardization):
    - KV bucket: ``ate-handoffs`` (lower-kebab)
    - KV key:     ``session.{session_id}.station.{station_id}.done`` (lower.dot)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class HandoffStatus(StrEnum):
    """Lifecycle status of a station-to-station handoff.

    Attributes:
        PENDING: Upstream station has not yet reported completion. The
            downstream station is waiting (or about to wait) on the KV key.
        DONE: Upstream station wrote the handoff record and the downstream
            station has observed it.
        TIMEOUT: The downstream station gave up waiting after ``timeout``
            seconds without observing the upstream handoff.
        FAILED: The upstream station completed but reported a failure
            (``pass_fail=False``), so the downstream station should not
            proceed with normal execution.
    """

    PENDING = "pending"
    DONE = "done"
    TIMEOUT = "timeout"
    FAILED = "failed"


@dataclass
class StationWorkflowConfig:
    """Configuration for a single station within a multi-station workflow.

    Attributes:
        station_id: Unique identifier for the station (e.g., ``"station-1"``).
            Must be unique within a workflow.
        name: Human-readable station name (e.g., ``"RF test station"``).
        sequence_ref: Reference to the test sequence this station runs
            (e.g., a sequence ID or name).
        upstream_stations: IDs of stations that must complete before this
            station can start. An empty list means the station is a head
            (first) station with no upstream dependency.
        timeout: Maximum seconds this station will wait for upstream
            handoffs before reporting TIMEOUT. Defaults to 300 (5 minutes).
    """

    station_id: str
    name: str = ""
    sequence_ref: str = ""
    upstream_stations: list[str] = field(default_factory=list)
    timeout: float = 300.0


@dataclass
class StationWorkflow:
    """A multi-station test workflow definition.

    A workflow describes the ordered set of stations a DUT passes through.
    The ordering is expressed via each StationConfig's ``upstream_stations``
    field (a DAG), not by the list order alone.

    Attributes:
        workflow_id: Unique identifier for the workflow instance.
        name: Human-readable workflow name.
        stations: Ordered list of station configurations. The list order is
            the nominal display order; actual execution order is derived
            from ``upstream_stations`` dependencies.
        handoff_rules: Optional mapping of rule names to values for future
            extensibility (e.g., ``{"on_failure": "skip_downstream"}``).
            Currently unused by the orchestrator but stored for round-trip.
        created_at: When the workflow was created (auto-generated).
    """

    workflow_id: str
    name: str = ""
    stations: list[StationWorkflowConfig] = field(default_factory=list)
    handoff_rules: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class StationHandoff:
    """Handoff record written by an upstream station on completion.

    Serialized as JSON and stored at KV key
    ``session.{session_id}.station.{station_id}.done`` in the
    ``ate-handoffs`` bucket.

    Attributes:
        session_id: The test session (DUT flow) identifier. Ties together
            all handoffs for a single DUT's journey through the workflow.
        station_id: The station that produced this handoff.
        serial_number: The DUT serial number under test.
        pass_fail: Whether the station's test passed (``True``) or failed.
        measurement_summary: Optional summary of measurements (free-form
            dict). Typically includes pass/fail counts and key values.
        timestamp: When the handoff was written (auto-generated).
    """

    session_id: str
    station_id: str
    serial_number: str = ""
    pass_fail: bool = True
    measurement_summary: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def handoff_to_dict(handoff: StationHandoff) -> dict[str, Any]:
    """Serialize a StationHandoff to a JSON-encodable dict.

    The ``timestamp`` is converted to ISO 8601 string for JSON transport.
    """
    return {
        "session_id": handoff.session_id,
        "station_id": handoff.station_id,
        "serial_number": handoff.serial_number,
        "pass_fail": handoff.pass_fail,
        "measurement_summary": handoff.measurement_summary,
        "timestamp": handoff.timestamp.isoformat(),
    }


def handoff_from_dict(data: dict[str, Any]) -> StationHandoff:
    """Deserialize a StationHandoff from a dict (inverse of handoff_to_dict).

    Tolerates a missing or malformed ``timestamp`` by falling back to
    ``datetime.now()`` - the authoritative timestamp is the KV entry's
    ``created`` field, not this value.
    """
    raw_ts = data.get("timestamp")
    if isinstance(raw_ts, str):
        try:
            timestamp = datetime.fromisoformat(raw_ts)
        except ValueError:
            timestamp = datetime.now()
    else:
        timestamp = datetime.now()
    return StationHandoff(
        session_id=data.get("session_id", ""),
        station_id=data.get("station_id", ""),
        serial_number=data.get("serial_number", ""),
        pass_fail=bool(data.get("pass_fail", True)),
        measurement_summary=dict(data.get("measurement_summary", {})),
        timestamp=timestamp,
    )


def workflow_to_dict(workflow: StationWorkflow) -> dict[str, Any]:
    """Serialize a StationWorkflow to a JSON-encodable dict."""
    return {
        "workflow_id": workflow.workflow_id,
        "name": workflow.name,
        "stations": [
            {
                "station_id": s.station_id,
                "name": s.name,
                "sequence_ref": s.sequence_ref,
                "upstream_stations": list(s.upstream_stations),
                "timeout": s.timeout,
            }
            for s in workflow.stations
        ],
        "handoff_rules": dict(workflow.handoff_rules),
        "created_at": workflow.created_at.isoformat(),
    }


def workflow_from_dict(data: dict[str, Any]) -> StationWorkflow:
    """Deserialize a StationWorkflow from a dict (inverse of workflow_to_dict)."""
    raw_ts = data.get("created_at")
    if isinstance(raw_ts, str):
        try:
            created_at = datetime.fromisoformat(raw_ts)
        except ValueError:
            created_at = datetime.now()
    else:
        created_at = datetime.now()
    stations = [
        StationWorkflowConfig(
            station_id=s.get("station_id", ""),
            name=s.get("name", ""),
            sequence_ref=s.get("sequence_ref", ""),
            upstream_stations=list(s.get("upstream_stations", [])),
            timeout=float(s.get("timeout", 300.0)),
        )
        for s in data.get("stations", [])
    ]
    return StationWorkflow(
        workflow_id=data.get("workflow_id", ""),
        name=data.get("name", ""),
        stations=stations,
        handoff_rules=dict(data.get("handoff_rules", {})),
        created_at=created_at,
    )
