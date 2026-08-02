"""Test limits CRUD API endpoints.

This module provides REST API endpoints for multi-versioned test limit
management:
- POST /api/v1/limits - Create a new test limit version
- GET /api/v1/limits - List all limits (optional product_type filter)
- GET /api/v1/limits/resolve - Resolve the effective limit for a date
- GET /api/v1/limits/{limit_id} - Get a limit by business identifier
- PUT /api/v1/limits/{limit_id} - Update a limit
- DELETE /api/v1/limits/{limit_id} - Delete a limit

The /resolve endpoint is registered before /{limit_id} so FastAPI does not
match "resolve" as a path parameter.
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.test_limits import TestLimit as TestLimitModel
from ate_cloud.schemas.test_limits import (
    TestLimitCreate,
    TestLimitResponse,
    TestLimitUpdate,
)
from ate_cloud.services.limit_resolver import LimitResolver

router = APIRouter()

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("", response_model=TestLimitResponse, status_code=status.HTTP_201_CREATED)
async def create_limit(
    limit_data: TestLimitCreate,
    db: DBSession,
) -> TestLimitResponse:
    """Create a new test limit version.

    Multiple versions for the same (product_type, test_name) pair can coexist
    with different effective_from dates. The limit_id should be unique across
    all versions to allow unambiguous lookup.

    Args:
        limit_data: The test limit creation data.
        db: Database session.

    Returns:
        TestLimitResponse: The created test limit with generated ID and
        timestamps.

    Raises:
        HTTPException: 409 if limit_id already exists.
    """
    limit = TestLimitModel(
        id=str(uuid.uuid4()),
        limit_id=limit_data.limit_id,
        product_type=limit_data.product_type,
        test_name=limit_data.test_name,
        spec_low=limit_data.spec_low,
        spec_high=limit_data.spec_high,
        unit=limit_data.unit,
        effective_from=limit_data.effective_from,
        effective_until=limit_data.effective_until,
    )

    db.add(limit)
    try:
        await db.commit()
        await db.refresh(limit)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Limit ID already exists"
        ) from None

    return TestLimitResponse.model_validate(limit)


@router.get("")
async def list_limits(
    db: DBSession,
    product_type: Annotated[str | None, Query(description="Filter by product type")] = None,
    skip: int = 0,
    limit: int = 100,
) -> dict[str, object]:
    """List all test limits with optional product_type filter.

    Args:
        product_type: Optional product type filter. If provided, only limits
            for that product type are returned.
        skip: Number of records to skip.
        limit: Maximum number of records to return.
        db: Database session.

    Returns:
        dict: Dictionary with 'items' list and 'total' count.
    """
    count_stmt = select(func.count()).select_from(TestLimitModel)
    list_stmt = select(TestLimitModel)

    if product_type is not None:
        count_stmt = count_stmt.where(TestLimitModel.product_type == product_type)
        list_stmt = list_stmt.where(TestLimitModel.product_type == product_type)

    result = await db.execute(count_stmt)
    total = result.scalar()

    result = await db.execute(list_stmt.offset(skip).limit(limit))
    limits = result.scalars().all()

    return {
        "items": [TestLimitResponse.model_validate(lim) for lim in limits],
        "total": total,
    }


@router.get("/resolve", response_model=TestLimitResponse)
async def resolve_limit(
    db: DBSession,
    product_type: Annotated[str, Query(description="Product type to resolve for")],
    test_name: Annotated[str, Query(description="Test measurement name to resolve for")],
    date: Annotated[
        date | None,
        Query(description="Resolution date (ISO format); today if omitted"),
    ] = None,
) -> TestLimitResponse:
    """Resolve the effective test limit for a given date.

    Finds the most recent limit version (by effective_from) that is active at
    the query date for the given product_type + test_name pair. If date is
    omitted, today's date is used.

    Args:
        product_type: Product type identifier.
        test_name: Test measurement name.
        date: Resolution date (ISO format YYYY-MM-DD). Defaults to today.
        db: Database session.

    Returns:
        TestLimitResponse: The effective test limit at the query date.

    Raises:
        HTTPException: 404 if no limit is effective at the query date.
    """
    resolver = LimitResolver(db)
    limit = await resolver.resolve(product_type, test_name, date)

    if limit is None:
        raise HTTPException(
            status_code=404,
            detail="No effective limit found for the given parameters",
        )

    return TestLimitResponse.model_validate(limit)


@router.get("/{limit_id}", response_model=TestLimitResponse)
async def get_limit(
    limit_id: str,
    db: DBSession,
) -> TestLimitResponse:
    """Get a test limit by its business identifier.

    Args:
        limit_id: The business identifier of the test limit.
        db: Database session.

    Returns:
        TestLimitResponse: The test limit data.

    Raises:
        HTTPException: 404 if limit not found.
    """
    result = await db.execute(
        select(TestLimitModel).where(TestLimitModel.limit_id == limit_id)
    )
    limit = result.scalar_one_or_none()

    if limit is None:
        raise HTTPException(status_code=404, detail="Test limit not found")

    return TestLimitResponse.model_validate(limit)


@router.put("/{limit_id}", response_model=TestLimitResponse)
async def update_limit(
    limit_id: str,
    limit_data: TestLimitUpdate,
    db: DBSession,
) -> TestLimitResponse:
    """Update an existing test limit.

    Args:
        limit_id: The business identifier of the test limit (lookup key).
        limit_data: The partial update data.
        db: Database session.

    Returns:
        TestLimitResponse: The updated test limit.

    Raises:
        HTTPException: 404 if limit not found.
        HTTPException: 409 if the new limit_id already exists.
    """
    result = await db.execute(
        select(TestLimitModel).where(TestLimitModel.limit_id == limit_id)
    )
    limit = result.scalar_one_or_none()

    if limit is None:
        raise HTTPException(status_code=404, detail="Test limit not found")

    update_data = limit_data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(limit, key, value)

    try:
        await db.commit()
        await db.refresh(limit)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Limit ID already exists"
        ) from None

    return TestLimitResponse.model_validate(limit)


@router.delete("/{limit_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_limit(
    limit_id: str,
    db: DBSession,
) -> None:
    """Delete a test limit.

    Args:
        limit_id: The business identifier of the test limit.
        db: Database session.

    Raises:
        HTTPException: 404 if limit not found.
    """
    result = await db.execute(
        select(TestLimitModel).where(TestLimitModel.limit_id == limit_id)
    )
    limit = result.scalar_one_or_none()

    if limit is None:
        raise HTTPException(status_code=404, detail="Test limit not found")

    await db.delete(limit)
    await db.commit()
