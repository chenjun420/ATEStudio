"""Sequences API endpoints for CRUD operations.

This module provides REST API endpoints for sequence management:
- GET /api/v1/sequences - List all sequences with pagination info
- GET /api/v1/sequences/{id} - Get a specific sequence by ID
- POST /api/v1/sequences - Create a new sequence
- PUT /api/v1/sequences/{id} - Update an existing sequence
- DELETE /api/v1/sequences/{id} - Delete a sequence
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.auth.dependencies import require_scopes
from ate_cloud.db import get_db
from ate_cloud.models.sequence import Sequence
from ate_cloud.models.user import User
from ate_cloud.schemas.sequence import SequenceCreate, SequenceResponse, SequenceUpdate

router = APIRouter(prefix="/sequences", tags=["sequences"])


@router.get("")
async def list_sequences(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_scopes("read")),
) -> dict[str, object]:
    """List all sequences with pagination.

    Args:
        skip: Number of records to skip.
        limit: Maximum number of records to return.
        db: Database session.

    Returns:
        dict: Dictionary with 'items' list and 'total' count.
    """
    result = await db.execute(select(func.count()).select_from(Sequence))
    total = result.scalar()

    result = await db.execute(select(Sequence).offset(skip).limit(limit))
    sequences = result.scalars().all()

    return {
        "items": [SequenceResponse.model_validate(s) for s in sequences],
        "total": total,
    }


@router.get("/{sequence_id}", response_model=SequenceResponse)
async def get_sequence(
    sequence_id: str,
    db: AsyncSession = Depends(get_db),
) -> SequenceResponse:
    """Get a sequence by ID.

    Args:
        sequence_id: The unique sequence identifier.
        db: Database session.

    Returns:
        SequenceResponse: The sequence data.

    Raises:
        HTTPException: 404 if sequence not found.
    """
    result = await db.execute(select(Sequence).where(Sequence.id == sequence_id))
    sequence = result.scalar_one_or_none()

    if not sequence:
        raise HTTPException(status_code=404, detail="Sequence not found")

    return SequenceResponse.model_validate(sequence)


@router.post("", response_model=SequenceResponse, status_code=status.HTTP_201_CREATED)
async def create_sequence(
    sequence_data: SequenceCreate,
    db: AsyncSession = Depends(get_db),
) -> SequenceResponse:
    """Create a new sequence.

    Args:
        sequence_data: The sequence creation data.
        db: Database session.

    Returns:
        SequenceResponse: The created sequence with generated ID and timestamps.

    Raises:
        HTTPException: 409 if sequence name already exists.
    """
    sequence = Sequence(
        id=str(uuid.uuid4()),
        name=sequence_data.name,
        description=sequence_data.description,
        yaml_content=sequence_data.yaml_content,
    )

    db.add(sequence)
    try:
        await db.commit()
        await db.refresh(sequence)
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Sequence name already exists") from e

    return SequenceResponse.model_validate(sequence)


@router.put("/{sequence_id}", response_model=SequenceResponse)
async def update_sequence(
    sequence_id: str,
    sequence_data: SequenceUpdate,
    db: AsyncSession = Depends(get_db),
) -> SequenceResponse:
    """Update an existing sequence.

    Args:
        sequence_id: The unique sequence identifier.
        sequence_data: The partial update data.
        db: Database session.

    Returns:
        SequenceResponse: The updated sequence.

    Raises:
        HTTPException: 404 if sequence not found.
        HTTPException: 409 if sequence name already exists.
    """
    result = await db.execute(select(Sequence).where(Sequence.id == sequence_id))
    sequence = result.scalar_one_or_none()

    if not sequence:
        raise HTTPException(status_code=404, detail="Sequence not found")

    update_data = sequence_data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(sequence, key, value)

    try:
        await db.commit()
        await db.refresh(sequence)
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Sequence name already exists") from e

    return SequenceResponse.model_validate(sequence)


@router.delete("/{sequence_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sequence(
    sequence_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a sequence.

    Args:
        sequence_id: The unique sequence identifier.
        db: Database session.

    Raises:
        HTTPException: 404 if sequence not found.
    """
    result = await db.execute(select(Sequence).where(Sequence.id == sequence_id))
    sequence = result.scalar_one_or_none()

    if not sequence:
        raise HTTPException(status_code=404, detail="Sequence not found")

    await db.delete(sequence)
    await db.commit()
