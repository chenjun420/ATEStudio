"""Pydantic schemas for Sequence resources.

This module defines the data models for sequence management:
- SequenceBase: Common fields for sequence data
- SequenceCreate: Schema for creating new sequences
- SequenceUpdate: Schema for updating existing sequences
- SequenceResponse: Schema for sequence API responses
"""

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field


class SequenceBase(BaseModel):
    """Base schema for sequence data.

    Attributes:
        name: Human-readable sequence name (1-255 characters)
        description: Optional sequence description
        yaml_content: YAML content of the sequence
    """

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    yaml_content: str = Field(..., min_length=1)


class SequenceCreate(SequenceBase):
    """Schema for creating a new sequence.

    Inherits all fields from SequenceBase.
    """

    pass


class SequenceUpdate(BaseModel):
    """Schema for updating an existing sequence.

    All fields are optional to support partial updates.

    Attributes:
        name: Updated sequence name (1-255 characters)
        description: Updated sequence description
        yaml_content: Updated YAML content
    """

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    yaml_content: str | None = Field(None, min_length=1)


class SequenceResponse(SequenceBase):
    """Schema for sequence API responses.

    Extends SequenceBase with system-managed fields.

    Attributes:
        id: Unique sequence identifier (UUID)
        created_at: Timestamp of sequence creation
        updated_at: Timestamp of last update
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"from_attributes": True}