"""Pydantic schemas for multi-station workflow API endpoints.

Defines request/response models for the workflow management API:
- ``StationConfigCreate`` / ``StationConfigResponse`` - single station config.
- ``WorkflowCreate`` / ``WorkflowResponse`` - workflow create/read.
- ``StationHandoffResponse`` - handoff status for a station in a session.

Internal data models live in ``shared.multi_station`` (dataclasses). These
Pydantic schemas are the API boundary; conversion happens in the API layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class StationConfigCreate(BaseModel):
    """Request schema for a station within a workflow creation request.

    Attributes:
        station_id: Unique station identifier within the workflow.
        name: Human-readable station name.
        sequence_ref: Reference to the test sequence this station runs.
        upstream_stations: IDs of stations that must complete first.
        timeout: Max seconds to wait for upstream handoff (default 300).
    """

    station_id: str = Field(..., min_length=1, max_length=255)
    name: str = Field(default="", max_length=255)
    sequence_ref: str = Field(default="", max_length=255)
    upstream_stations: list[str] = Field(default_factory=list)
    timeout: float = Field(default=300.0, gt=0)


class StationConfigResponse(BaseModel):
    """Response schema for a station configuration."""

    station_id: str
    name: str = ""
    sequence_ref: str = ""
    upstream_stations: list[str] = []
    timeout: float = 300.0


class WorkflowCreate(BaseModel):
    """Request schema for creating a multi-station workflow.

    Attributes:
        workflow_id: Optional client-supplied workflow ID. If omitted, the
            server generates a UUID.
        name: Human-readable workflow name.
        stations: Ordered list of station configurations.
        handoff_rules: Optional extensibility rules mapping.
    """

    workflow_id: str | None = Field(default=None, max_length=255)
    name: str = Field(default="", max_length=255)
    stations: list[StationConfigCreate] = Field(..., min_length=1)
    handoff_rules: dict[str, str] = Field(default_factory=dict)


class WorkflowResponse(BaseModel):
    """Response schema for a multi-station workflow."""

    workflow_id: str
    name: str = ""
    stations: list[StationConfigResponse] = []
    handoff_rules: dict[str, str] = {}
    created_at: datetime


class StationHandoffResponse(BaseModel):
    """Response schema for a station's handoff status within a session.

    Attributes:
        session_id: The test session identifier.
        station_id: The station identifier.
        status: Handoff status string (one of HandoffStatus values).
        handoff: The handoff record if present, ``None`` if pending.
    """

    session_id: str
    station_id: str
    status: str
    handoff: dict[str, Any] | None = None
