"""Products API endpoints for product configuration template CRUD.

Provides:
- POST /api/v1/products - Create a new product config
- GET /api/v1/products - List all product configs
- GET /api/v1/products/{product_type} - Get a config by product type
- PUT /api/v1/products/{product_type} - Update a config by product type
- DELETE /api/v1/products/{product_type} - Delete a config by product type

Product configs are reference data that define testing templates for different
product types -- which test sequence to run, what limits apply, which instruments
are needed, and what checkpoints exist. They are NOT execution records.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.product_config import ProductConfig
from ate_cloud.schemas.product_config import (
    ProductConfigCreate,
    ProductConfigResponse,
    ProductConfigUpdate,
)

router = APIRouter(prefix="/products", tags=["products"])

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("")
async def list_product_configs(
    db: DBSession,
    skip: int = 0,
    limit: int = 100,
) -> dict[str, object]:
    """List all product configs with pagination.

    Args:
        skip: Number of records to skip.
        limit: Maximum number of records to return.
        db: Database session.

    Returns:
        dict: Dictionary with 'items' list and 'total' count.
    """
    result = await db.execute(select(func.count()).select_from(ProductConfig))
    total = result.scalar()

    result = await db.execute(select(ProductConfig).offset(skip).limit(limit))
    configs = result.scalars().all()

    return {
        "items": [ProductConfigResponse.model_validate(c) for c in configs],
        "total": total,
    }


@router.get("/{product_type}", response_model=ProductConfigResponse)
async def get_product_config(
    product_type: str,
    db: DBSession,
) -> ProductConfigResponse:
    """Get a product config by product type.

    Args:
        product_type: The unique product type identifier.
        db: Database session.

    Returns:
        ProductConfigResponse: The product config data.

    Raises:
        HTTPException: 404 if product config not found.
    """
    result = await db.execute(
        select(ProductConfig).where(ProductConfig.product_type == product_type)
    )
    config = result.scalar_one_or_none()

    if not config:
        raise HTTPException(status_code=404, detail="Product config not found")

    return ProductConfigResponse.model_validate(config)


@router.post("", response_model=ProductConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_product_config(
    config_data: ProductConfigCreate,
    db: DBSession,
) -> ProductConfigResponse:
    """Create a new product config.

    Args:
        config_data: The product config creation data.
        db: Database session.

    Returns:
        ProductConfigResponse: The created product config with generated ID and timestamps.

    Raises:
        HTTPException: 409 if product_type already exists.
    """
    config = ProductConfig(
        id=str(uuid.uuid4()),
        product_type=config_data.product_type,
        test_sequence_ref=config_data.test_sequence_ref,
        test_limits=config_data.test_limits,
        instrument_assignments=config_data.instrument_assignments,
        checkpoints=config_data.checkpoints,
    )

    db.add(config)
    try:
        await db.commit()
        await db.refresh(config)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Product type already exists"
        ) from None

    return ProductConfigResponse.model_validate(config)


@router.put("/{product_type}", response_model=ProductConfigResponse)
async def update_product_config(
    product_type: str,
    config_data: ProductConfigUpdate,
    db: DBSession,
) -> ProductConfigResponse:
    """Update an existing product config.

    The ``product_type`` path parameter is the lookup key. If the body
    includes a new ``product_type``, the config is renamed (subject to
    uniqueness constraint).

    Args:
        product_type: The current product type identifier (lookup key).
        config_data: The partial update data.
        db: Database session.

    Returns:
        ProductConfigResponse: The updated product config.

    Raises:
        HTTPException: 404 if product config not found.
        HTTPException: 409 if the new product_type already exists.
    """
    result = await db.execute(
        select(ProductConfig).where(ProductConfig.product_type == product_type)
    )
    config = result.scalar_one_or_none()

    if not config:
        raise HTTPException(status_code=404, detail="Product config not found")

    update_data = config_data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(config, key, value)

    try:
        await db.commit()
        await db.refresh(config)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Product type already exists"
        ) from None

    return ProductConfigResponse.model_validate(config)


@router.delete("/{product_type}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product_config(
    product_type: str,
    db: DBSession,
) -> None:
    """Delete a product config.

    Args:
        product_type: The unique product type identifier.
        db: Database session.

    Raises:
        HTTPException: 404 if product config not found.
    """
    result = await db.execute(
        select(ProductConfig).where(ProductConfig.product_type == product_type)
    )
    config = result.scalar_one_or_none()

    if not config:
        raise HTTPException(status_code=404, detail="Product config not found")

    await db.delete(config)
    await db.commit()
