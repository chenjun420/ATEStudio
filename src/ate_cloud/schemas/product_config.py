"""Pydantic schemas for ProductConfig resources.

This module defines the data models for product configuration CRUD:
- ProductConfigBase: Common fields for product config data
- ProductConfigCreate: Schema for creating new product configs
- ProductConfigUpdate: Schema for updating existing product configs (all optional)
- ProductConfigResponse: Schema for product config API responses
"""

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field


class ProductConfigBase(BaseModel):
    """Base schema for product configuration data.

    Attributes:
        product_type: Unique product type identifier (1-255 characters).
        test_sequence_ref: Reference to the test sequence to run.
        test_limits: List of test limit references.
        instrument_assignments: Mapping of instrument role to instrument identifier.
        checkpoints: List of checkpoint identifiers.
    """

    product_type: str = Field(..., min_length=1, max_length=255)
    test_sequence_ref: str = Field(..., min_length=1, max_length=255)
    test_limits: list[str] = Field(default_factory=list)
    instrument_assignments: dict[str, str] = Field(default_factory=dict)
    checkpoints: list[str] = Field(default_factory=list)


class ProductConfigCreate(ProductConfigBase):
    """Schema for creating a new product config.

    Inherits all fields from ProductConfigBase.
    """

    pass


class ProductConfigUpdate(BaseModel):
    """Schema for updating an existing product config.

    All fields are optional to support partial updates. The product_type
    field is included to allow renaming, but the path parameter is the
    lookup key.

    Attributes:
        product_type: Updated product type identifier.
        test_sequence_ref: Updated test sequence reference.
        test_limits: Updated list of test limit references.
        instrument_assignments: Updated instrument role mapping.
        checkpoints: Updated checkpoint identifiers.
    """

    product_type: str | None = Field(None, min_length=1, max_length=255)
    test_sequence_ref: str | None = Field(None, min_length=1, max_length=255)
    test_limits: list[str] | None = None
    instrument_assignments: dict[str, str] | None = None
    checkpoints: list[str] | None = None


class ProductConfigResponse(ProductConfigBase):
    """Schema for product config API responses.

    Extends ProductConfigBase with system-managed fields.

    Attributes:
        id: Unique product config identifier (UUID).
        created_at: Timestamp of creation.
        updated_at: Timestamp of last update.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = {"from_attributes": True}
