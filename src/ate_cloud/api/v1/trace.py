"""Test traceability API endpoints (T33).

Provides:
- ``GET /api/v1/trace/{serial_number}`` - rebuild the full trace chain for
  a DUT serial number and return it as a W3C PROV JSON-LD document.

The endpoint queries executions and measurements by ``dut_serial``,
rebuilds the chain via ``TestTraceService.build_trace``, and projects it
to JSON-LD via ``TestTraceService.to_jsonld``. The response is a bare
JSON-LD dict (``@context`` + ``@graph``) so PROV-aware consumers can
ingest it directly.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.schemas.trace import TestTraceResult
from ate_cloud.services.test_trace_service import TestTraceService

router = APIRouter(prefix="/trace", tags=["trace"])

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("/{serial_number}")
async def get_trace(
    serial_number: str,
    db: DBSession,
) -> dict[str, object]:
    """GET /api/v1/trace/{serial_number} - rebuild the DUT trace chain.

    Returns a W3C PROV JSON-LD document describing the full chain from
    the DUT serial number through every execution (station, instruments)
    to every measurement (value, limits, outcome). When the serial number
    has no executions and no measurements, the document contains only the
    DUT entity node.

    Args:
        serial_number: The DUT serial number to trace.
        db: Database session.

    Returns:
        A JSON-LD dict with ``@context`` and ``@graph`` keys. The graph
        always contains at least the DUT entity; it contains activity /
        instrument / measurement nodes only when the chain has them.

    Raises:
        HTTPException: 404 if no trace data exists for the serial number
            (neither executions nor measurements).
    """
    service = TestTraceService(db)
    trace = await service.build_trace(serial_number)

    if not trace.steps:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No trace data found for serial number '{serial_number}'",
        )

    return service.to_jsonld(trace)


@router.get("/{serial_number}/structured", response_model=TestTraceResult)
async def get_trace_structured(
    serial_number: str,
    db: DBSession,
) -> TestTraceResult:
    """GET /api/v1/trace/{serial_number}/structured - structured trace.

    Returns the same chain as ``GET /{serial_number}`` but as the
    structured ``TestTraceResult`` model (chronologically ordered steps
    with instruments and measurements) rather than the PROV JSON-LD
    projection. Intended for the frontend timeline viewer and for
    debugging.

    Args:
        serial_number: The DUT serial number to trace.
        db: Database session.

    Returns:
        TestTraceResult with chronologically ordered steps. The steps
        list is empty when no executions and no measurements exist.

    Raises:
        HTTPException: 404 if no trace data exists for the serial number.
    """
    service = TestTraceService(db)
    trace = await service.build_trace(serial_number)

    if not trace.steps:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No trace data found for serial number '{serial_number}'",
        )

    return trace


__all__ = ["router"]
