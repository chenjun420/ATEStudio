"""Reports API endpoints for test execution data export.

Provides:
- GET /api/v1/reports/atml/{execution_id} — ATML (IEEE 1636.1) XML export.
- GET /api/v1/reports/{format}/{execution_id} — parameterized format export
  (format = atml | csv | parquet).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.execution import Execution
from ate_cloud.models.measurement import Measurement
from ate_cloud.services.report_exporter import ExportFormat, ReportExporter

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
)

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


async def _fetch_execution(
    execution_id: str,
    db: AsyncSession,
) -> Execution:
    """Look up an Execution by ID or raise 404.

    Args:
        execution_id: The unique execution identifier.
        db: Database session.

    Returns:
        The Execution ORM object.

    Raises:
        HTTPException: 404 if execution not found.
    """
    result = await db.execute(
        select(Execution).where(Execution.id == execution_id)
    )
    execution = result.scalar_one_or_none()
    if execution is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution not found",
        )
    return execution


async def _fetch_measurements(
    execution_id: str,
    db: AsyncSession,
) -> list[Measurement]:
    """Fetch all measurements for an execution.

    Returns an empty list if no measurements exist — the ATML TestSteps
    element will simply be empty.

    Args:
        execution_id: The execution identifier to filter by.
        db: Database session.

    Returns:
        List of Measurement records for the execution.
    """
    result = await db.execute(
        select(Measurement).where(Measurement.execution_ref == execution_id)
    )
    return list(result.scalars().all())


@router.get("/atml/{execution_id}")
async def export_atml(
    execution_id: str,
    db: DBSession,
) -> Response:
    """GET /api/v1/reports/atml/{execution_id} — ATML XML export.

    Returns the execution data as IEEE 1636.1 TestResults XML.

    Args:
        execution_id: The execution identifier.
        db: Database session.

    Returns:
        Response with ``text/xml`` content type.

    Raises:
        HTTPException: 404 if execution not found.
    """
    execution = await _fetch_execution(execution_id, db)
    measurements = await _fetch_measurements(execution_id, db)
    exporter = ReportExporter()
    content, media_type = exporter.export(execution, measurements, "atml")
    return Response(content=content, media_type=media_type)


@router.get("/{format}/{execution_id}")
async def export_report(
    format: ExportFormat,
    execution_id: str,
    db: DBSession,
) -> Response:
    """GET /api/v1/reports/{format}/{execution_id} — parameterized format export.

    Supported formats: ``atml`` (XML), ``csv`` (flat measurements),
    ``parquet`` (columnar binary; falls back to CSV if pyarrow unavailable).

    Args:
        format: Export format — ``atml``, ``csv``, or ``parquet``.
        execution_id: The execution identifier.
        db: Database session.

    Returns:
        Response with appropriate content type for the requested format.

    Raises:
        HTTPException: 404 if execution not found.
        HTTPException: 400 if format is not supported.
    """
    execution = await _fetch_execution(execution_id, db)
    measurements = await _fetch_measurements(execution_id, db)
    exporter = ReportExporter()
    try:
        content, media_type = exporter.export(execution, measurements, format)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    return Response(content=content, media_type=media_type)


__all__ = ["router"]
