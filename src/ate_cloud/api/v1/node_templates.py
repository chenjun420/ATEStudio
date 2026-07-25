"""Node Templates API endpoints for CRUD operations.

This module provides REST API endpoints for node template management:
- GET /api/v1/node-templates - List all node templates with pagination
- GET /api/v1/node-templates/{id} - Get a specific node template by ID
- POST /api/v1/node-templates - Create a new node template
- PUT /api/v1/node-templates/{id} - Update an existing node template
- DELETE /api/v1/node-templates/{id} - Delete a node template
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.node_template import NodeTemplate
from ate_cloud.schemas.node_template import (
    NodeTemplateCreate,
    NodeTemplateResponse,
    NodeTemplateUpdate,
)

router = APIRouter(prefix="/node-templates", tags=["node-templates"])


@router.get("")
async def list_node_templates(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """List all node templates with pagination.

    Args:
        skip: Number of records to skip.
        limit: Maximum number of records to return.
        db: Database session.

    Returns:
        dict: Dictionary with 'items' list and 'total' count.
    """
    result = await db.execute(select(func.count()).select_from(NodeTemplate))
    total = result.scalar()

    result = await db.execute(select(NodeTemplate).offset(skip).limit(limit))
    templates = result.scalars().all()

    return {
        "items": [NodeTemplateResponse.model_validate(t) for t in templates],
        "total": total,
    }


@router.get("/{template_id}", response_model=NodeTemplateResponse)
async def get_node_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> NodeTemplateResponse:
    """Get a node template by ID.

    Args:
        template_id: The unique template identifier.
        db: Database session.

    Returns:
        NodeTemplateResponse: The template data.

    Raises:
        HTTPException: 404 if template not found.
    """
    result = await db.execute(
        select(NodeTemplate).where(NodeTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()

    if not template:
        raise HTTPException(status_code=404, detail="Node template not found")

    return template


@router.post(
    "", response_model=NodeTemplateResponse, status_code=status.HTTP_201_CREATED
)
async def create_node_template(
    template_data: NodeTemplateCreate,
    db: AsyncSession = Depends(get_db),
) -> NodeTemplateResponse:
    """Create a new node template.

    Args:
        template_data: The template creation data.
        db: Database session.

    Returns:
        NodeTemplateResponse: The created template with generated ID and timestamps.

    Raises:
        HTTPException: 409 if template name already exists.
    """
    template = NodeTemplate(
        id=str(uuid.uuid4()),
        name=template_data.name,
        type=template_data.type,
        appearance=template_data.appearance,
        default_data=template_data.default_data,
    )

    db.add(template)
    try:
        await db.commit()
        await db.refresh(template)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Node template name already exists"
        )

    return template


@router.put("/{template_id}", response_model=NodeTemplateResponse)
async def update_node_template(
    template_id: str,
    template_data: NodeTemplateUpdate,
    db: AsyncSession = Depends(get_db),
) -> NodeTemplateResponse:
    """Update an existing node template.

    Args:
        template_id: The unique template identifier.
        template_data: The partial update data.
        db: Database session.

    Returns:
        NodeTemplateResponse: The updated template.

    Raises:
        HTTPException: 404 if template not found.
        HTTPException: 409 if template name already exists.
    """
    result = await db.execute(
        select(NodeTemplate).where(NodeTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()

    if not template:
        raise HTTPException(status_code=404, detail="Node template not found")

    update_data = template_data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(template, key, value)

    try:
        await db.commit()
        await db.refresh(template)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Node template name already exists"
        )

    return template


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a node template.

    Args:
        template_id: The unique template identifier.
        db: Database session.

    Raises:
        HTTPException: 404 if template not found.
    """
    result = await db.execute(
        select(NodeTemplate).where(NodeTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()

    if not template:
        raise HTTPException(status_code=404, detail="Node template not found")

    await db.delete(template)
    await db.commit()