"""Scripts API endpoints for CRUD operations.

This module provides REST API endpoints for script management:
- GET /api/v1/scripts - List all scripts with pagination info
- GET /api/v1/scripts/{id} - Get a specific script by ID
- POST /api/v1/scripts - Create a new script
- PUT /api/v1/scripts/{id} - Update an existing script
- DELETE /api/v1/scripts/{id} - Delete a script
- GET /api/v1/scripts/{id}/content - Read script file content
- PUT /api/v1/scripts/{id}/content - Write script file content (with Git commit)
- GET /api/v1/scripts/{id}/versions - List version history for a script
- GET /api/v1/scripts/{id}/versions/{commit_hash} - Read script at a specific version
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.script import Script
from ate_cloud.schemas.script import (
    ScriptContentResponse,
    ScriptContentUpdate,
    ScriptCreate,
    ScriptResponse,
    ScriptUpdate,
    ScriptVersionInfo,
    ScriptVersionListResponse,
)
from ate_cloud.services.script_versioning import ScriptVersioningService

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

    return ScriptResponse.model_validate(script)


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
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Script name already exists") from e

    return ScriptResponse.model_validate(script)


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
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Script name already exists") from e

    return ScriptResponse.model_validate(script)


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


def _get_versioning_service(request: Request) -> ScriptVersioningService:
    """Retrieve the ScriptVersioningService from app state.

    Args:
        request: The FastAPI request object.

    Returns:
        The ScriptVersioningService instance.

    Raises:
        HTTPException: 503 if the service is not initialized.
    """
    service = getattr(request.app.state, "script_versioning", None)
    if not isinstance(service, ScriptVersioningService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Script versioning service not initialized",
        )
    return service


async def _get_script_by_id(
    script_id: str,
    db: AsyncSession,
) -> Script:
    """Look up a Script by ID or raise 404.

    Args:
        script_id: The unique script identifier.
        db: Database session.

    Returns:
        The Script ORM object.

    Raises:
        HTTPException: 404 if script not found.
    """
    result = await db.execute(select(Script).where(Script.id == script_id))
    script = result.scalar_one_or_none()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    return script


@router.get("/{script_id}/content", response_model=ScriptContentResponse)
async def get_script_content(
    script_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ScriptContentResponse:
    """Read the current content of a script file.

    Args:
        script_id: The unique script identifier.
        request: The FastAPI request (to access app.state).
        db: Database session.

    Returns:
        ScriptContentResponse with content, version hash, and last_modified.

    Raises:
        HTTPException: 404 if script or file not found.
    """
    script = await _get_script_by_id(script_id, db)
    svc = _get_versioning_service(request)

    content = svc.read_content(script.script_path)
    version = svc.get_head_commit_hash(script.script_path) or ""
    last_modified = svc.get_last_modified(script.script_path)

    return ScriptContentResponse(
        content=content,
        version=version,
        last_modified=last_modified,
    )


@router.put("/{script_id}/content", response_model=ScriptContentResponse)
async def update_script_content(
    script_id: str,
    content_data: ScriptContentUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ScriptContentResponse:
    """Write content to a script file and create a Git commit.

    Args:
        script_id: The unique script identifier.
        content_data: The content update payload.
        request: The FastAPI request (to access app.state).
        db: Database session.

    Returns:
        ScriptContentResponse with updated content, version hash, and last_modified.

    Raises:
        HTTPException: 404 if script not found.
    """
    script = await _get_script_by_id(script_id, db)
    svc = _get_versioning_service(request)

    commit_hash = svc.write_content(
        script_path=script.script_path,
        content=content_data.content,
        commit_message=content_data.commit_message,
    )
    last_modified = svc.get_last_modified(script.script_path)

    return ScriptContentResponse(
        content=content_data.content,
        version=commit_hash,
        last_modified=last_modified,
    )


@router.get("/{script_id}/versions", response_model=ScriptVersionListResponse)
async def list_script_versions(
    script_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ScriptVersionListResponse:
    """List version history for a script file.

    Args:
        script_id: The unique script identifier.
        request: The FastAPI request (to access app.state).
        db: Database session.

    Returns:
        ScriptVersionListResponse with list of version entries (newest first).

    Raises:
        HTTPException: 404 if script or file not found.
    """
    script = await _get_script_by_id(script_id, db)
    svc = _get_versioning_service(request)

    versions = svc.list_versions(script.script_path)

    return ScriptVersionListResponse(
        versions=[ScriptVersionInfo.model_validate(v) for v in versions]
    )


@router.get(
    "/{script_id}/versions/{commit_hash}",
    response_model=ScriptContentResponse,
)
async def get_script_version_content(
    script_id: str,
    commit_hash: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ScriptContentResponse:
    """Read script content at a specific Git commit.

    Args:
        script_id: The unique script identifier.
        commit_hash: The Git commit hash.
        request: The FastAPI request (to access app.state).
        db: Database session.

    Returns:
        ScriptContentResponse with content at the given version.

    Raises:
        HTTPException: 404 if script, commit, or file at commit not found.
    """
    script = await _get_script_by_id(script_id, db)
    svc = _get_versioning_service(request)

    content = svc.read_version(script.script_path, commit_hash)

    return ScriptContentResponse(
        content=content,
        version=commit_hash,
        last_modified=None,
    )
