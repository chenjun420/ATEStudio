"""Pydantic schemas for CalibrationRecord resources.

Defines request/response models for the calibration CRUD API:
- CalibrationStatus: Literal type for VALID/EXPIRING/EXPIRED.
- CalibrationCreate: Schema for recording a new calibration.
- CalibrationUpdate: Schema for updating an existing record (all optional).
- CalibrationResponse: Schema for API responses (includes system-managed fields).
- CalibrationStatusResponse: Schema for the status-check endpoint.
- CalibrationListResponse: Paginated-style list response.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

# Calibration status values stored in the ``status`` column.
CalibrationStatus = Literal["VALID", "EXPIRING", "EXPIRED"]

# Number of days before next_due during which the status is EXPIRING.
EXPIRING_WINDOW_DAYS: int = 7


class CalibrationCreate(BaseModel):
    """Schema for recording a new calibration result.

    Attributes:
        instrument_id: Instrument identifier (resource name or VISA address).
        last_calibration: When the calibration was performed. Defaults to now.
        interval_days: Calibration interval in days (must be >= 1).
        notes: Optional free-text notes.
    """

    instrument_id: str = Field(..., min_length=1, max_length=255)
    last_calibration: datetime = Field(default_factory=lambda: datetime.now(UTC))
    interval_days: int = Field(..., ge=1, le=36500)
    notes: str | None = Field(default=None, max_length=65535)

    @field_validator("instrument_id")
    @classmethod
    def _instrument_id_stripped(cls, v: str) -> str:
        """Strip whitespace from instrument_id."""
        v = v.strip()
        if not v:
            raise ValueError("instrument_id must not be empty after strip")
        return v


class CalibrationUpdate(BaseModel):
    """Schema for updating an existing calibration record.

    All fields are optional to support partial updates.

    Attributes:
        instrument_id: Updated instrument identifier.
        last_calibration: Updated last-calibration timestamp. When set,
            next_due is recomputed by the service layer.
        interval_days: Updated calibration interval (days).
        notes: Updated free-text notes.
    """

    instrument_id: str | None = Field(None, min_length=1, max_length=255)
    last_calibration: datetime | None = None
    interval_days: int | None = Field(None, ge=1, le=36500)
    notes: str | None = Field(None, max_length=65535)


class CalibrationResponse(BaseModel):
    """Schema for calibration API responses.

    Attributes:
        id: Unique record identifier (UUID).
        instrument_id: Instrument identifier.
        last_calibration: When the last calibration was performed.
        interval_days: Calibration interval in days.
        next_due: Computed next-due date.
        status: VALID, EXPIRING, or EXPIRED.
        notes: Optional free-text notes.
        created_at: Timestamp of record creation.
        updated_at: Timestamp of last update.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    instrument_id: str
    last_calibration: datetime
    interval_days: int
    next_due: datetime
    status: CalibrationStatus
    notes: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = {"from_attributes": True}


class CalibrationStatusResponse(BaseModel):
    """Schema for the status-check endpoint.

    Attributes:
        instrument_id: The queried instrument identifier.
        status: VALID, EXPIRING, or EXPIRED. ``UNKNOWN`` (as a plain str)
            when no calibration record exists for the instrument.
        next_due: Next-due date if a record exists, else None.
        days_until_due: Whole days until next_due (negative if expired).
            None when no record exists.
        record: The full calibration record if one exists, else None.
    """

    instrument_id: str
    status: str
    next_due: datetime | None = None
    days_until_due: int | None = None
    record: CalibrationResponse | None = None


class CalibrationListResponse(BaseModel):
    """Paginated-style list response for calibration records.

    Attributes:
        items: List of calibration records.
        total: Number of records in the list.
    """

    items: list[CalibrationResponse]
    total: int
