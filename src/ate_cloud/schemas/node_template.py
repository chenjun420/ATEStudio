"""Pydantic schemas for NodeTemplate resources.

This module defines the data models for node template management:
- NodeTemplateBase: Common fields for node template data
- NodeTemplateCreate: Schema for creating new node templates
- NodeTemplateUpdate: Schema for updating existing node templates
- NodeTemplateResponse: Schema for node template API responses
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class NodeTemplateBase(BaseModel):
    """Base schema for node template data.

    Attributes:
        name: Human-readable template name (1-255 characters).
        type: Node type (e.g., 'start', 'script', 'end').
        appearance: JSON object for visual appearance.
        default_data: JSON object for default node configuration.
    """

    name: str = Field(..., min_length=1, max_length=255)
    type: str = Field(..., min_length=1, max_length=50)
    appearance: dict[str, Any] | None = None
    default_data: dict[str, Any] | None = None


class NodeTemplateCreate(NodeTemplateBase):
    """Schema for creating a new node template.

    Inherits all fields from NodeTemplateBase.
    """

    pass


class NodeTemplateUpdate(BaseModel):
    """Schema for updating an existing node template.

    All fields are optional to support partial updates.

    Attributes:
        name: Updated template name (1-255 characters).
        type: Updated node type (1-50 characters).
        appearance: Updated visual appearance.
        default_data: Updated default node configuration.
    """

    name: str | None = Field(None, min_length=1, max_length=255)
    type: str | None = Field(None, min_length=1, max_length=50)
    appearance: dict[str, Any] | None = None
    default_data: dict[str, Any] | None = None


class NodeTemplateResponse(NodeTemplateBase):
    """Schema for node template API responses.

    Extends NodeTemplateBase with system-managed fields.

    Attributes:
        id: Unique template identifier (UUID).
        created_at: Timestamp of template creation.
        updated_at: Timestamp of last update.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = {"from_attributes": True}
