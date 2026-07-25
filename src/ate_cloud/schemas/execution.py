"""Pydantic schemas for Execution resources.

Defines request/response models for execution CRUD and SSE streaming:
- ExecutionCreate: Schema for starting a new execution
- ExecutionUpdate: Schema for partial updates (internal use)
- ExecutionResponse: Schema for execution API responses
- ExecutionAbortResponse: Schema for abort endpoint response
"""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class ExecutionCreate(BaseModel):
    """Schema for creating a new execution.

    Attributes:
        sequence_id: The sequence to execute (required).
        config: Optional execution configuration (max_concurrency, etc.).
    """

    sequence_id: str = Field(..., min_length=1, max_length=36)
    config: dict[str, Any] | None = None


class ExecutionUpdate(BaseModel):
    """Schema for updating an existing execution.

    All fields are optional to support partial updates.

    Attributes:
        status: Updated execution status.
        result: Updated result summary.
        error: Updated error message.
        completed_at: Timestamp when execution completed.
    """

    status: str | None = Field(None, pattern=r"^(PENDING|RUNNING|COMPLETED|FAILED|ABORTED)$")
    result: dict[str, Any] | None = None
    error: str | None = None
    completed_at: datetime | None = None


class ExecutionResponse(BaseModel):
    """Schema for execution API responses.

    Attributes:
        id: Unique execution identifier (= run_id).
        sequence_id: Reference to the sequence being executed.
        status: Current execution state.
        config: Execution configuration.
        result: Final result summary.
        error: Error message on failure.
        started_at: Timestamp when execution started running.
        completed_at: Timestamp when execution completed.
        created_at: Timestamp of record creation.
        updated_at: Timestamp of last update.
    """

    id: str
    sequence_id: str | None = None
    status: str = "PENDING"
    config: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"from_attributes": True}


class ExecutionAbortResponse(BaseModel):
    """Schema for abort execution response.

    Attributes:
        id: The execution run_id.
        status: Confirmation status (ABORTING).
    """

    id: str
    status: str = "ABORTING"
