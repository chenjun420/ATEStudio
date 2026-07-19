"""Pydantic schemas for Script resources.

This module defines the data models for script management:
- ScriptBase: Common fields for script data
- ScriptCreate: Schema for creating new scripts
- ScriptUpdate: Schema for updating existing scripts
- ScriptResponse: Schema for script API responses
"""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class ScriptBase(BaseModel):
    """Base schema for script data.

    Attributes:
        name: Human-readable script name (1-255 characters)
        description: Optional script description
        script_path: Path to the script file
        params_schema: JSON Schema for script parameters
        tags: List of tags for categorization
    """

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    script_path: str = Field(..., min_length=1)
    params_schema: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)


class ScriptCreate(ScriptBase):
    """Schema for creating a new script.

    Inherits all fields from ScriptBase.
    """

    pass


class ScriptUpdate(BaseModel):
    """Schema for updating an existing script.

    All fields are optional to support partial updates.

    Attributes:
        name: Updated script name (1-255 characters)
        description: Updated script description
        script_path: Updated script file path
        params_schema: Updated JSON Schema for parameters
        tags: Updated list of tags
    """

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    script_path: str | None = Field(None, min_length=1)
    params_schema: dict[str, Any] | None = None
    tags: list[str] | None = None


class ScriptResponse(ScriptBase):
    """Schema for script API responses.

    Extends ScriptBase with system-managed fields.

    Attributes:
        id: Unique script identifier (UUID)
        created_at: Timestamp of script creation
        updated_at: Timestamp of last update
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
