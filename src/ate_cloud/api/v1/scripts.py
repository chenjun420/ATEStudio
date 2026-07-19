"""Scripts API endpoints for CRUD operations.

This module provides REST API endpoints for script management:
- GET /api/v1/scripts - List all scripts with pagination info
- GET /api/v1/scripts/{id} - Get a specific script by ID
- POST /api/v1/scripts - Create a new script
- PUT /api/v1/scripts/{id} - Update an existing script
- DELETE /api/v1/scripts/{id} - Delete a script
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status

from ate_cloud.schemas.script import ScriptCreate, ScriptResponse, ScriptUpdate
from ate_cloud.storage.memory import MemoryStorage

router = APIRouter(prefix="/scripts", tags=["scripts"])

# Global storage instance (for now)
storage = MemoryStorage[ScriptResponse]()


@router.get("", response_model=dict)
async def list_scripts() -> dict:
    """List all scripts.

    Returns:
        dict: Dictionary with 'items' list and 'total' count.
    """
    items = await storage.list()
    total = await storage.count()
    return {"items": items, "total": total}


@router.get("/{id}", response_model=ScriptResponse)
async def get_script(id: str) -> ScriptResponse:
    """Get a script by ID.

    Args:
        id: The unique script identifier.

    Returns:
        ScriptResponse: The script data.

    Raises:
        HTTPException: 404 if script not found.
    """
    script = await storage.get(id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    return script


@router.post("", response_model=ScriptResponse, status_code=status.HTTP_201_CREATED)
async def create_script(script: ScriptCreate) -> ScriptResponse:
    """Create a new script.

    Args:
        script: The script creation data.

    Returns:
        ScriptResponse: The created script with generated ID and timestamps.
    """
    response = ScriptResponse(**script.model_dump())
    await storage.create(response.id, response)
    return response


@router.put("/{id}", response_model=ScriptResponse)
async def update_script(id: str, script: ScriptUpdate) -> ScriptResponse:
    """Update an existing script.

    Args:
        id: The unique script identifier.
        script: The partial update data.

    Returns:
        ScriptResponse: The updated script.

    Raises:
        HTTPException: 404 if script not found.
    """
    existing = await storage.get(id)
    if not existing:
        raise HTTPException(status_code=404, detail="Script not found")

    # Only update fields that were provided (partial update)
    update_data = {k: v for k, v in script.model_dump().items() if v is not None}
    updated = existing.model_copy(update=update_data)
    updated.updated_at = datetime.now(timezone.utc)
    await storage.update(id, updated)
    return updated


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_script(id: str) -> None:
    """Delete a script.

    Args:
        id: The unique script identifier.

    Raises:
        HTTPException: 404 if script not found.
    """
    deleted = await storage.delete(id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Script not found")