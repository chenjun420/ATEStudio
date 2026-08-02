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
        step_results: Per-step results (JSON list).
        error: Error message on failure.
        started_at: Timestamp when execution started running.
        completed_at: Timestamp when execution completed.
        dut_serial: Device-under-test serial number.
        station_id: Station that ran the execution.
        instrument_ids: List of instrument IDs used.
        created_at: Timestamp of record creation.
        updated_at: Timestamp of last update.
    """

    id: str
    sequence_id: str | None = None
    status: str = "PENDING"
    config: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    step_results: list[dict[str, Any]] | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    dut_serial: str | None = None
    station_id: str | None = None
    instrument_ids: list[str] | None = None
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


class ExecutionSearchRequest(BaseModel):
    """Schema for searching executions with advanced filters.

    All fields are optional. When multiple filters are provided they are
    combined with AND logic.

    Attributes:
        serial_number: Filter by DUT serial number (partial match).
        product_type: Filter by product type (exact match on config.product_type).
        status: Filter by execution status (PENDING/RUNNING/COMPLETED/FAILED/ABORTED).
        date_from: Filter executions started at or after this ISO datetime.
        date_to: Filter executions started at or before this ISO datetime.
        skip: Pagination offset.
        limit: Maximum number of results (default 50, max 500).
    """

    serial_number: str | None = None
    product_type: str | None = None
    status: str | None = Field(None, pattern=r"^(PENDING|RUNNING|COMPLETED|FAILED|ABORTED)$")
    date_from: datetime | None = None
    date_to: datetime | None = None
    skip: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=500)


class ExecutionListItem(BaseModel):
    """Compact execution item for list/search responses.

    Attributes:
        id: Execution identifier.
        sequence_id: Sequence reference.
        status: Execution status.
        dut_serial: DUT serial number.
        product_type: Product type from config.
        started_at: Execution start timestamp.
        completed_at: Execution completion timestamp.
        pass_rate: Pass rate percentage from result.
        error: Error message if failed.
    """

    id: str
    sequence_id: str | None = None
    status: str = "PENDING"
    dut_serial: str | None = None
    product_type: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    pass_rate: float | None = None
    error: str | None = None

    model_config = {"from_attributes": True}


class ExecutionSearchResponse(BaseModel):
    """Paginated response for execution search.

    Attributes:
        items: List of execution items.
        total: Total matching count (for pagination).
        skip: Pagination offset.
        limit: Page size.
    """

    items: list[ExecutionListItem]
    total: int
    skip: int
    limit: int
