"""Calibration record CRUD API endpoints.

This module provides REST API endpoints for instrument calibration
management:
- POST /api/v1/calibrations - Record a calibration result (create/update)
- GET /api/v1/calibrations - List records (optional instrument_id, status filters)
- GET /api/v1/calibrations/status - Check status for an instrument (query param)
- GET /api/v1/calibrations/{instrument_id} - Get latest record for an instrument
- PUT /api/v1/calibrations/{instrument_id} - Update a record
- DELETE /api/v1/calibrations/{instrument_id} - Delete records for an instrument
- POST /api/v1/calibrations/check-expiry - Refresh status for all records

The /status and /check-expiry endpoints are registered before /{instrument_id}
so FastAPI does not match them as path parameters.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.calibration import CalibrationRecord
from ate_cloud.schemas.calibration import (
    CalibrationCreate,
    CalibrationListResponse,
    CalibrationResponse,
    CalibrationStatus,
    CalibrationStatusResponse,
    CalibrationUpdate,
)
from ate_cloud.services.calibration_manager import CalibrationManager

router = APIRouter(prefix="/calibrations", tags=["calibration"])

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("", response_model=CalibrationResponse, status_code=status.HTTP_201_CREATED)
async def record_calibration(
    data: CalibrationCreate,
    db: DBSession,
) -> CalibrationResponse:
    """POST /api/v1/calibrations - Record a calibration result.

    Creates a new calibration record or updates the latest existing record
    for the same instrument (single source of truth per instrument).

    Args:
        data: Calibration creation payload (instrument_id, last_calibration,
            interval_days, notes).
        db: Database session.

    Returns:
        CalibrationResponse: The created or updated calibration record.
    """
    manager = CalibrationManager(db)
    return await manager.record_calibration(data)


@router.get("", response_model=CalibrationListResponse)
async def list_calibrations(
    db: DBSession,
    instrument_id: Annotated[
        str | None, Query(description="Filter by instrument_id")
    ] = None,
    status_filter: Annotated[
        CalibrationStatus | None,
        Query(alias="status", description="Filter by status VALID/EXPIRING/EXPIRED"),
    ] = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> CalibrationListResponse:
    """GET /api/v1/calibrations - List calibration records.

    Args:
        instrument_id: Optional instrument filter.
        status_filter: Optional status filter.
        skip: Number of records to skip.
        limit: Maximum records to return.
        db: Database session.

    Returns:
        CalibrationListResponse with items and total count.
    """
    count_stmt = select(func.count()).select_from(CalibrationRecord)
    if instrument_id is not None:
        count_stmt = count_stmt.where(
            CalibrationRecord.instrument_id == instrument_id
        )
    if status_filter is not None:
        count_stmt = count_stmt.where(CalibrationRecord.status == status_filter)
    result = await db.execute(count_stmt)
    total = result.scalar() or 0

    manager = CalibrationManager(db)
    records = await manager.list_records(
        instrument_id=instrument_id,
        status_filter=status_filter,
        skip=skip,
        limit=limit,
    )
    return CalibrationListResponse(
        items=[CalibrationResponse.model_validate(r) for r in records],
        total=total,
    )


@router.get("/status", response_model=CalibrationStatusResponse)
async def get_calibration_status(
    db: DBSession,
    instrument_id: Annotated[str, Query(description="Instrument to check")],
) -> CalibrationStatusResponse:
    """GET /api/v1/calibrations/status?instrument_id=... - Check status.

    Returns the current calibration status for an instrument. If no
    record exists, returns ``UNKNOWN`` with null fields (and does NOT
    block execution).

    Args:
        instrument_id: The instrument identifier to check.
        db: Database session.

    Returns:
        CalibrationStatusResponse with status, next_due, days_until_due,
        and the full record (if one exists).
    """
    manager = CalibrationManager(db)
    record = await manager.check_status(instrument_id)
    if record is None:
        return CalibrationStatusResponse(
            instrument_id=instrument_id,
            status="UNKNOWN",
            next_due=None,
            days_until_due=None,
            record=None,
        )
    from datetime import UTC, datetime

    from ate_cloud.services.calibration_manager import _normalize

    now = _normalize(datetime.now(UTC))
    due = _normalize(record.next_due)
    delta = due - now
    days_until = delta.days
    return CalibrationStatusResponse(
        instrument_id=instrument_id,
        status=record.status,
        next_due=record.next_due,
        days_until_due=days_until,
        record=record,
    )


@router.post("/check-expiry")
async def check_expiry(db: DBSession) -> dict[str, object]:
    """POST /api/v1/calibrations/check-expiry - Refresh all statuses.

    Recomputes the status column for every calibration record against the
    current time. Intended to be called by a background scheduler (e.g.,
    daily) or manually by an operator.

    Args:
        db: Database session.

    Returns:
        dict with ``updated`` count of records whose status changed.
    """
    manager = CalibrationManager(db)
    changed = await manager.check_expiry()
    return {"updated": changed}


@router.get("/{instrument_id}", response_model=CalibrationResponse)
async def get_calibration(
    instrument_id: str,
    db: DBSession,
) -> CalibrationResponse:
    """GET /api/v1/calibrations/{instrument_id} - Get latest record.

    Args:
        instrument_id: The instrument identifier.
        db: Database session.

    Returns:
        CalibrationResponse: The latest calibration record.

    Raises:
        HTTPException: 404 if no calibration record exists.
    """
    manager = CalibrationManager(db)
    record = await manager.check_status(instrument_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No calibration record for instrument '{instrument_id}'",
        )
    return record


@router.put("/{instrument_id}", response_model=CalibrationResponse)
async def update_calibration(
    instrument_id: str,
    data: CalibrationUpdate,
    db: DBSession,
) -> CalibrationResponse:
    """PUT /api/v1/calibrations/{instrument_id} - Update a record.

    Args:
        instrument_id: The instrument identifier (lookup key).
        data: Partial update payload.
        db: Database session.

    Returns:
        CalibrationResponse: The updated record.

    Raises:
        HTTPException: 404 if no calibration record exists.
    """
    manager = CalibrationManager(db)
    record = await manager.update_calibration(instrument_id, data)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No calibration record for instrument '{instrument_id}'",
        )
    return record


@router.delete("/{instrument_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_calibration(
    instrument_id: str,
    db: DBSession,
) -> None:
    """DELETE /api/v1/calibrations/{instrument_id} - Delete records.

    Deletes all calibration records for the given instrument.

    Args:
        instrument_id: The instrument identifier.
        db: Database session.

    Raises:
        HTTPException: 404 if no calibration record exists.
    """
    manager = CalibrationManager(db)
    deleted = await manager.delete_calibration(instrument_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No calibration record for instrument '{instrument_id}'",
        )
