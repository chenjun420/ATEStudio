"""Pydantic schemas for debug breakpoint resources.

Defines request/response models for breakpoint CRUD and debug session events:
- BreakpointCreate: Schema for creating a new breakpoint
- BreakpointUpdate: Schema for partial updates (all optional)
- BreakpointResponse: Schema for breakpoint API responses
- DebugPauseEvent: SSE event payload for breakpoint-hit notifications
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class BreakpointCreate(BaseModel):
    """Schema for creating a new debug breakpoint.

    Attributes:
        session_id: Debug session identifier (correlates with execution run_id).
        step_id: Test step identifier this breakpoint is attached to.
        node_id: X6 graph node identifier (for canvas visualisation).
        line_number: Line number within the script (1-based; 0 = any line).
        condition: Optional conditional expression evaluated when the
            breakpoint is hit. Empty string means unconditional.
        enabled: Whether the breakpoint is active on creation (default True).
        node_data: Optional X6 node serialised data for canvas restoration.
    """

    session_id: str = Field(..., min_length=1, max_length=36)
    step_id: str = Field(..., min_length=1, max_length=255)
    node_id: str = Field(..., min_length=1, max_length=255)
    line_number: int = Field(default=0, ge=0)
    condition: str | None = Field(default=None, max_length=1024)
    enabled: bool = True
    node_data: dict[str, Any] | None = None


class BreakpointUpdate(BaseModel):
    """Schema for updating an existing debug breakpoint.

    All fields are optional to support partial updates.

    Attributes:
        step_id: Updated step identifier.
        node_id: Updated node identifier.
        line_number: Updated line number.
        condition: Updated conditional expression.
        enabled: Toggle breakpoint active state.
        node_data: Updated X6 node serialised data.
    """

    step_id: str | None = Field(None, min_length=1, max_length=255)
    node_id: str | None = Field(None, min_length=1, max_length=255)
    line_number: int | None = Field(None, ge=0)
    condition: str | None = Field(None, max_length=1024)
    enabled: bool | None = None
    node_data: dict[str, Any] | None = None


class BreakpointResponse(BaseModel):
    """Schema for debug breakpoint API responses.

    Attributes:
        id: Unique breakpoint identifier (UUID).
        session_id: Debug session identifier.
        step_id: Test step identifier.
        node_id: X6 graph node identifier.
        line_number: Line number within the script.
        condition: Conditional expression (None = unconditional).
        enabled: Whether the breakpoint is active.
        node_data: X6 node serialised data for canvas restoration.
        created_at: Timestamp of creation.
        updated_at: Timestamp of last update.
    """

    id: str
    session_id: str
    step_id: str
    node_id: str
    line_number: int
    condition: str | None = None
    enabled: bool = True
    node_data: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = {"from_attributes": True}


class DebugPauseEvent(BaseModel):
    """SSE event payload pushed when a debug breakpoint is hit.

    Published to NATS subject ``ate.debug.{session_id}`` and delivered to
    SSE clients watching the debug stream.

    Attributes:
        session_id: Debug session identifier.
        step_id: The step that was executing when the breakpoint was hit.
        node_id: The X6 node associated with the breakpoint.
        line_number: The line number where execution paused.
        thread_id: debugpy thread identifier of the paused thread.
        frames: Variable snapshot -- list of stack frames with local variables.
        reason: Why execution paused (``breakpoint``, ``exception``, ``step``).
        timestamp: Unix timestamp of the pause event.
    """

    session_id: str
    step_id: str
    node_id: str
    line_number: int
    thread_id: int | None = None
    frames: list[dict[str, Any]] = Field(default_factory=list)
    reason: str = "breakpoint"
    timestamp: float = Field(default_factory=lambda: datetime.now(UTC).timestamp())
