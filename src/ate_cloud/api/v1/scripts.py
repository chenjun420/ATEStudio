"""Scripts API endpoints for CRUD operations.

This module provides REST API endpoints for script management:
- GET /api/v1/scripts - List all scripts with pagination info
- GET /api/v1/scripts/{id} - Get a specific script by ID
- POST /api/v1/scripts - Create a new script
- PUT /api/v1/scripts/{id} - Update an existing script
- DELETE /api/v1/scripts/{id} - Delete a script
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.script import Script
from ate_cloud.schemas.script import ScriptCreate, ScriptResponse, ScriptUpdate

router = APIRouter(prefix="/scripts", tags=["scripts"])


@router.get("")
async def list_scripts(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """List all scripts with pagination.

    Args:
        skip: Number of records to skip.
        limit: Maximum number of records to return.
        db: Database session.

    Returns:
        dict: Dictionary with 'items' list and 'total' count.
    """
    result = await db.execute(select(func.count()).select_from(Script))
    total = result.scalar()

    result = await db.execute(select(Script).offset(skip).limit(limit))
    scripts = result.scalars().all()

    return {
        "items": [ScriptResponse.model_validate(s) for s in scripts],
        "total": total,
    }


@router.get("/{script_id}", response_model=ScriptResponse)
async def get_script(
    script_id: str,
    db: AsyncSession = Depends(get_db),
) -> ScriptResponse:
    """Get a script by ID.

    Args:
        script_id: The unique script identifier.
        db: Database session.

    Returns:
        ScriptResponse: The script data.

    Raises:
        HTTPException: 404 if script not found.
    """
    result = await db.execute(select(Script).where(Script.id == script_id))
    script = result.scalar_one_or_none()

    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    return script


@router.post("", response_model=ScriptResponse, status_code=status.HTTP_201_CREATED)
async def create_script(
    script_data: ScriptCreate,
    db: AsyncSession = Depends(get_db),
) -> ScriptResponse:
    """Create a new script.

    Args:
        script_data: The script creation data.
        db: Database session.

    Returns:
        ScriptResponse: The created script with generated ID and timestamps.

    Raises:
        HTTPException: 409 if script name already exists.
    """
    script = Script(
        id=str(uuid.uuid4()),
        name=script_data.name,
        description=script_data.description,
        script_path=script_data.script_path,
        params_schema=script_data.params_schema,
        tags=script_data.tags,
    )

    db.add(script)
    try:
        await db.commit()
        await db.refresh(script)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Script name already exists")

    return script


@router.put("/{script_id}", response_model=ScriptResponse)
async def update_script(
    script_id: str,
    script_data: ScriptUpdate,
    db: AsyncSession = Depends(get_db),
) -> ScriptResponse:
    """Update an existing script.

    Args:
        script_id: The unique script identifier.
        script_data: The partial update data.
        db: Database session.

    Returns:
        ScriptResponse: The updated script.

    Raises:
        HTTPException: 404 if script not found.
        HTTPException: 409 if script name already exists.
    """
    result = await db.execute(select(Script).where(Script.id == script_id))
    script = result.scalar_one_or_none()

    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    update_data = script_data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(script, key, value)

    try:
        await db.commit()
        await db.refresh(script)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Script name already exists")

    return script


@router.delete("/{script_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_script(
    script_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a script.

    Args:
        script_id: The unique script identifier.
        db: Database session.

    Raises:
        HTTPException: 404 if script not found.
    """
    result = await db.execute(select(Script).where(Script.id == script_id))
    script = result.scalar_one_or_none()

    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    await db.delete(script)
    await db.commit()
