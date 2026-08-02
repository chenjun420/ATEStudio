"""Debug breakpoint CRUD API endpoints.

Provides REST API endpoints for managing debug breakpoints:
- POST /api/v1/debug/breakpoints - Create a new breakpoint
- GET /api/v1/debug/breakpoints - List breakpoints (optional session_id filter)
- GET /api/v1/debug/breakpoints/{bp_id} - Get a breakpoint by id
- PUT /api/v1/debug/breakpoints/{bp_id} - Update a breakpoint
- DELETE /api/v1/debug/breakpoints/{bp_id} - Delete a breakpoint

These endpoints are only functionally active when ATE_DEV_MODE=true.
In production mode, breakpoints can still be created/listed (for inspection)
but the DebugProcessExecutor will not attach debugpy to running scripts.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.config import settings
from ate_cloud.db import get_db
from ate_cloud.models.breakpoint import Breakpoint as BreakpointModel
from ate_cloud.schemas.debug import (
    BreakpointCreate,
    BreakpointResponse,
    BreakpointUpdate,
)

router = APIRouter(prefix="/debug", tags=["debug"])

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


def _check_dev_mode() -> None:
    """Raise 403 if ATE_DEV_MODE is not enabled.

    Debug endpoints are only active in development mode. The breakpoints
    table can still be queried, but creating/modifying breakpoints is
    restricted to dev mode to prevent accidental debug activation in
    production environments.
    """
    if not settings.dev_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Debug endpoints require ATE_DEV_MODE=true",
        )


@router.post(
    "/breakpoints",
    response_model=BreakpointResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_breakpoint(
    bp_data: BreakpointCreate,
    db: DBSession,
) -> BreakpointResponse:
    """Create a new debug breakpoint.

    Args:
        bp_data: The breakpoint creation data.
        db: Database session.

    Returns:
        BreakpointResponse: The created breakpoint.

    Raises:
        HTTPException: 403 if ATE_DEV_MODE is not enabled.
    """
    _check_dev_mode()

    bp = BreakpointModel(
        id=str(uuid.uuid4()),
        session_id=bp_data.session_id,
        step_id=bp_data.step_id,
        node_id=bp_data.node_id,
        line_number=bp_data.line_number,
        condition=bp_data.condition,
        enabled=bp_data.enabled,
        node_data=bp_data.node_data,
    )

    db.add(bp)
    await db.commit()
    await db.refresh(bp)

    return BreakpointResponse.model_validate(bp)


@router.get("/breakpoints")
async def list_breakpoints(
    db: DBSession,
    session_id: Annotated[
        str | None, Query(description="Filter by debug session id")
    ] = None,
    skip: int = 0,
    limit: int = 100,
) -> dict[str, object]:
    """List debug breakpoints with optional session_id filter.

    Args:
        session_id: Optional debug session id filter.
        skip: Number of records to skip.
        limit: Maximum number of records to return.
        db: Database session.

    Returns:
        dict: Dictionary with 'items' list and 'total' count.
    """
    count_stmt = select(func.count()).select_from(BreakpointModel)
    list_stmt = select(BreakpointModel)

    if session_id is not None:
        count_stmt = count_stmt.where(BreakpointModel.session_id == session_id)
        list_stmt = list_stmt.where(BreakpointModel.session_id == session_id)

    result = await db.execute(count_stmt)
    total = result.scalar()

    result = await db.execute(list_stmt.offset(skip).limit(limit))
    breakpoints = result.scalars().all()

    return {
        "items": [BreakpointResponse.model_validate(bp) for bp in breakpoints],
        "total": total,
    }


@router.get("/breakpoints/{bp_id}", response_model=BreakpointResponse)
async def get_breakpoint(
    bp_id: str,
    db: DBSession,
) -> BreakpointResponse:
    """Get a debug breakpoint by id.

    Args:
        bp_id: The breakpoint identifier.
        db: Database session.

    Returns:
        BreakpointResponse: The breakpoint data.

    Raises:
        HTTPException: 404 if breakpoint not found.
    """
    result = await db.execute(
        select(BreakpointModel).where(BreakpointModel.id == bp_id)
    )
    bp = result.scalar_one_or_none()

    if bp is None:
        raise HTTPException(status_code=404, detail="Breakpoint not found")

    return BreakpointResponse.model_validate(bp)


@router.put("/breakpoints/{bp_id}", response_model=BreakpointResponse)
async def update_breakpoint(
    bp_id: str,
    bp_data: BreakpointUpdate,
    db: DBSession,
) -> BreakpointResponse:
    """Update an existing debug breakpoint.

    Args:
        bp_id: The breakpoint identifier.
        bp_data: The partial update data.
        db: Database session.

    Returns:
        BreakpointResponse: The updated breakpoint.

    Raises:
        HTTPException: 403 if ATE_DEV_MODE is not enabled.
        HTTPException: 404 if breakpoint not found.
    """
    _check_dev_mode()

    result = await db.execute(
        select(BreakpointModel).where(BreakpointModel.id == bp_id)
    )
    bp = result.scalar_one_or_none()

    if bp is None:
        raise HTTPException(status_code=404, detail="Breakpoint not found")

    update_data = bp_data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(bp, key, value)

    await db.commit()
    await db.refresh(bp)

    return BreakpointResponse.model_validate(bp)


@router.delete("/breakpoints/{bp_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_breakpoint(
    bp_id: str,
    db: DBSession,
) -> None:
    """Delete a debug breakpoint.

    Args:
        bp_id: The breakpoint identifier.
        db: Database session.

    Raises:
        HTTPException: 403 if ATE_DEV_MODE is not enabled.
        HTTPException: 404 if breakpoint not found.
    """
    _check_dev_mode()

    result = await db.execute(
        select(BreakpointModel).where(BreakpointModel.id == bp_id)
    )
    bp = result.scalar_one_or_none()

    if bp is None:
        raise HTTPException(status_code=404, detail="Breakpoint not found")

    await db.delete(bp)
    await db.commit()
