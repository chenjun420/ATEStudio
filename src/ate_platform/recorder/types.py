"""Recorded event types and the RecordedEvent Pydantic model.

A RecordedEvent is the unit of data written to the
``ate.execution.{session_id}.events`` JetStream stream by
:class:`~ate_platform.recorder.execution_recorder.ExecutionRecorder` and
read back by
:class:`~ate_platform.recorder.replay_executor.ReplayExecutor`.

Each event carries:
- timestamp: monotonically increasing wall-clock time (UTC, ISO 8601 string).
- event_type: one of the RecordedEventType variants.
- session_id: the execution session this event belongs to.
- step_id: optional step identifier (for step_transition, measurement_result).
- data: event-type-specific payload as a dict.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RecordedEventType(StrEnum):
    """Enumeration of recorded event types.

    Each variant maps to a category of execution activity captured during
    recording. The str mixin makes the enum JSON-serializable directly.
    """

    STEP_TRANSITION = "step_transition"
    MEASUREMENT_RESULT = "measurement_result"
    OPERATOR_INTERACTION = "operator_interaction"
    SCHEDULER_DECISION = "scheduler_decision"
    NATS_MESSAGE = "nats_message"


class RecordedEvent(BaseModel):
    """A single recorded execution event.

    Serialized as one JSONL line in the
    ``ate.execution.{session_id}.events`` JetStream stream. The model is
    the shared contract between ExecutionRecorder (writer) and
    ReplayExecutor (reader).

    Attributes:
        timestamp: When the event occurred, in UTC ISO 8601 format.
        event_type: The category of event (see RecordedEventType).
        session_id: The execution session identifier.
        step_id: Optional step identifier; None for non-step events.
        data: Event-type-specific payload.
    """

    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp of the event occurrence.",
    )
    event_type: RecordedEventType
    session_id: str = Field(..., min_length=1, description="Execution session identifier.")
    step_id: str | None = Field(default=None, description="Optional step identifier.")
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Event-type-specific payload.",
    )

    def to_jsonl(self) -> str:
        """Serialize the event as a single JSONL line (no trailing newline).

        Returns:
            Compact JSON string suitable for writing as one line in a
            JSONL stream.
        """
        return self.model_dump_json()

    @classmethod
    def from_jsonl(cls, line: str) -> RecordedEvent:
        """Parse a single JSONL line into a RecordedEvent.

        Args:
            line: A JSON string (with or without a trailing newline).

        Returns:
            A RecordedEvent instance.

        Raises:
            pydantic.ValidationError: If the line is not valid JSON or
                does not match the RecordedEvent schema.
        """
        return cls.model_validate_json(line.strip())
