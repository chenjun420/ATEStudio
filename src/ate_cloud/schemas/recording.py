"""Pydantic schemas for execution recording and replay API.

Defines request/response models for the recording endpoints:
- RecordStartRequest / RecordStartResponse: POST /executions/{id}/record
- ReplayStartRequest / ReplayStartResponse: POST /executions/{id}/replay
- RecordedEventResponse: a single recorded event in API responses
- ReplayResultResponse: summary of a completed replay
- ReplayDiffResponse: diff between original and replayed sequences
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from ate_platform.recorder.types import RecordedEventType


class RecordStartRequest(BaseModel):
    """Request body for POST /api/v1/executions/{id}/record.

    Attributes:
        auto_stop_on_complete: If True, recording stops automatically when
            an EXECUTION_COMPLETED event is observed. Defaults to True.
    """

    auto_stop_on_complete: bool = True


class RecordStartResponse(BaseModel):
    """Response for POST /api/v1/executions/{id}/record.

    Attributes:
        session_id: The execution session being recorded.
        subject: The JetStream subject events are written to.
        status: "recording" on success.
        started_at: UTC timestamp when recording started.
    """

    session_id: str
    subject: str
    status: str = "recording"
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReplayStartRequest(BaseModel):
    """Request body for POST /api/v1/executions/{id}/replay.

    Attributes:
        speed_multiplier: Time acceleration factor (1.0=real-time,
            2.0=2x, 5.0=5x, 10.0=10x). Defaults to 1.0.
        max_events: Optional limit on number of events to replay.
            None means replay all.
    """

    speed_multiplier: float = Field(default=1.0, gt=0, le=100.0)
    max_events: int | None = Field(default=None, ge=1)


class RecordedEventResponse(BaseModel):
    """A single recorded event in API responses.

    Attributes:
        timestamp: UTC ISO 8601 timestamp.
        event_type: The event category.
        session_id: Execution session identifier.
        step_id: Optional step identifier.
        data: Event-type-specific payload.
    """

    timestamp: datetime
    event_type: RecordedEventType
    session_id: str
    step_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ReplayResultResponse(BaseModel):
    """Summary of a completed replay.

    Attributes:
        session_id: The replayed execution session.
        status: "completed" or "cancelled".
        events_replayed: Number of events replayed.
        events_total: Total events available.
        speed_multiplier: The time acceleration factor used.
        duration_seconds: Wall-clock time the replay took.
        events: The replayed events in timestamp order.
    """

    session_id: str
    status: str
    events_replayed: int
    events_total: int
    speed_multiplier: float
    duration_seconds: float
    events: list[RecordedEventResponse] = Field(default_factory=list)


class ReplayDiffEntry(BaseModel):
    """A single diff entry (added, removed, or changed event).

    Attributes:
        kind: "added", "removed", or "changed".
        step_id: The step identifier (may be empty for non-step events).
        event_type: The event type.
        original: The original event (None for "added").
        replayed: The replayed event (None for "removed").
    """

    kind: str
    step_id: str = ""
    event_type: str = ""
    original: dict[str, Any] | None = None
    replayed: dict[str, Any] | None = None


class ReplayDiffSummary(BaseModel):
    """Summary statistics of a diff.

    Attributes:
        original_count: Number of events in the original sequence.
        replayed_count: Number of events in the replayed sequence.
        added: Number of events added in replay.
        removed: Number of events removed in replay.
        changed: Number of events with changed data.
    """

    original_count: int
    replayed_count: int
    added: int
    removed: int
    changed: int


class ReplayDiffResponse(BaseModel):
    """Diff between original and replayed event sequences.

    Attributes:
        session_id: The execution session.
        summary: Diff summary statistics.
        entries: List of diff entries (added/removed/changed).
    """

    session_id: str
    summary: ReplayDiffSummary
    entries: list[ReplayDiffEntry] = Field(default_factory=list)


class RecordingStatusResponse(BaseModel):
    """Recording status for GET /api/v1/executions/{id}/recording.

    Attributes:
        session_id: The execution session identifier.
        is_recording: Whether the recorder is actively running.
        event_count: Number of events published so far.
        subject: The JetStream subject events are written to.
    """

    session_id: str
    is_recording: bool
    event_count: int = 0
    subject: str = ""


class ReplayControlResponse(BaseModel):
    """Response for replay pause/resume control endpoints.

    Attributes:
        session_id: The execution session identifier.
        action: The control action ("pause" or "resume").
        status: The resulting replay state ("paused" or "resumed").
    """

    session_id: str
    action: str
    status: str
