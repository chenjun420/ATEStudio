"""FMEA CRUD API endpoints (task 13).

REST surface over the task-10 ``FMEA`` ORM model
(``models/knowledge.py``). RPN (risk priority number) is ALWAYS derived
server-side as severity*occurrence*detection by the model's SQLAlchemy
mapper events — the create/update schemas accept no ``rpn`` and a
client-supplied ``rpn`` is ignored. Ratings are constrained to [1, 10] at
the Pydantic boundary (-> 422), with the ORM validator and a DB CHECK
constraint as defense in depth.

Endpoints (all JWT-protected at mount time via ``_PROTECTED_ROUTERS``):
- POST   /api/v1/fmea          create an entry (201, computed rpn)
- GET    /api/v1/fmea          list, optional component_code/fault_code filters
- GET    /api/v1/fmea/{fmea_id} get one (404 when missing)
- PUT    /api/v1/fmea/{fmea_id} partial update (rpn recomputed)
- DELETE /api/v1/fmea/{fmea_id} delete (404 when missing, like products)
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.knowledge import FMEA
from ate_cloud.schemas.knowledge import FMEACreate, FMEAResponse, FMEAUpdate

router = APIRouter(prefix="/fmea", tags=["fmea"])

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=dict[str, object])
async def list_fmeas(
    db: DBSession,
    component_code: Annotated[
        str | None, Query(description="Filter by component_code")
    ] = None,
    fault_code: Annotated[
        str | None, Query(description="Filter by fault_code")
    ] = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, object]:
    """GET /api/v1/fmea — list FMEA entries with optional filters.

    Args:
        db: Database session.
        component_code: Optional component filter.
        fault_code: Optional fault-code filter.
        skip: Number of records to skip.
        limit: Maximum records to return.

    Returns:
        dict with ``items`` (FMEAResponse list) and ``total`` count.
    """
    filters = []
    if component_code is not None:
        filters.append(FMEA.component_code == component_code)
    if fault_code is not None:
        filters.append(FMEA.fault_code == fault_code)

    count_stmt = select(func.count()).select_from(FMEA)
    list_stmt = select(FMEA).order_by(FMEA.created_at.desc())
    for condition in filters:
        count_stmt = count_stmt.where(condition)
        list_stmt = list_stmt.where(condition)

    total = (await db.execute(count_stmt)).scalar() or 0
    rows = (await db.execute(list_stmt.offset(skip).limit(limit))).scalars().all()

    return {
        "items": [FMEAResponse.model_validate(r) for r in rows],
        "total": total,
    }


@router.get("/{fmea_id}", response_model=FMEAResponse)
async def get_fmea(fmea_id: str, db: DBSession) -> FMEAResponse:
    """GET /api/v1/fmea/{fmea_id} — fetch one entry.

    Raises:
        HTTPException: 404 if no entry exists for the id.
    """
    entry = (
        await db.execute(select(FMEA).where(FMEA.id == fmea_id))
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="FMEA entry not found")
    return FMEAResponse.model_validate(entry)


@router.post("", response_model=FMEAResponse, status_code=status.HTTP_201_CREATED)
async def create_fmea(data: FMEACreate, db: DBSession) -> FMEAResponse:
    """POST /api/v1/fmea — create an FMEA entry.

    The rpn is derived server-side (S*O*D) by the model's mapper events;
    any client-supplied rpn is ignored.
    """
    entry = FMEA(id=str(uuid.uuid4()), **data.model_dump())
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return FMEAResponse.model_validate(entry)


@router.put("/{fmea_id}", response_model=FMEAResponse)
async def update_fmea(
    fmea_id: str, data: FMEAUpdate, db: DBSession
) -> FMEAResponse:
    """PUT /api/v1/fmea/{fmea_id} — partially update an entry.

    Supplied ratings are range-validated (422) and rpn is recomputed
    server-side; a client-supplied rpn is ignored.

    Raises:
        HTTPException: 404 if no entry exists for the id.
    """
    entry = (
        await db.execute(select(FMEA).where(FMEA.id == fmea_id))
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="FMEA entry not found")

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(entry, key, value)

    await db.commit()
    await db.refresh(entry)
    return FMEAResponse.model_validate(entry)


@router.delete("/{fmea_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fmea(fmea_id: str, db: DBSession) -> None:
    """DELETE /api/v1/fmea/{fmea_id} — delete an entry.

    Raises:
        HTTPException: 404 if no entry exists for the id.
    """
    entry = (
        await db.execute(select(FMEA).where(FMEA.id == fmea_id))
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="FMEA entry not found")

    await db.delete(entry)
    await db.commit()
