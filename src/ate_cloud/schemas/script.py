"""Pydantic schemas for Script resources.

This module defines the data models for script management:
- ScriptBase: Common fields for script data
- ScriptCreate: Schema for creating new scripts
- ScriptUpdate: Schema for updating existing scripts
- ScriptResponse: Schema for script API responses
- ScriptContentResponse: Schema for script content read responses
- ScriptContentUpdate: Schema for script content write requests
- ScriptVersionInfo: Schema for a single version entry
- ScriptVersionListResponse: Schema for version history responses
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

    model_config = {"from_attributes": True}


class ScriptContentUpdate(BaseModel):
    """Schema for updating script file content.

    Attributes:
        content: The new script file content.
        commit_message: Optional Git commit message. Auto-generated if omitted.
    """

    content: str = Field(..., min_length=1)
    commit_message: str | None = None


class ScriptContentResponse(BaseModel):
    """Schema for script content API responses.

    Attributes:
        content: The script file content.
        version: The Git commit hash of the current version.
        last_modified: Timestamp of the last Git commit for this file.
    """

    content: str
    version: str
    last_modified: datetime | None = None


class ScriptVersionInfo(BaseModel):
    """Schema for a single script version entry.

    Attributes:
        hash: The Git commit hash.
        message: The commit message.
        author: The commit author name.
        timestamp: The commit timestamp (UTC).
    """

    hash: str
    message: str
    author: str
    timestamp: datetime


class ScriptVersionListResponse(BaseModel):
    """Schema for script version history responses.

    Attributes:
        versions: List of version entries, newest first.
    """

    versions: list[ScriptVersionInfo]


class WorkerVersionTag(BaseModel):
    """A worker-side tag binding a script path to a Git commit hash.

    Stored as JSON in the ``ate-scripts`` JetStream KV bucket under
    ``workers.{worker_id}.{script_path}``.

    Attributes:
        worker_id: Unique worker identifier.
        script_path: Relative script path (forward slashes).
        commit_hash: Git commit hash the worker should run.
        tagged_at: When the tag was written (auto-generated).
    """

    worker_id: str
    script_path: str
    commit_hash: str
    tagged_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkerVersionDiff(BaseModel):
    """Difference between a script's tagged hash and its current head.

    Attributes:
        script_path: Relative script path.
        tagged_hash: Hash recorded in the worker tag.
        current_hash: Hash of the current Git HEAD for this path.
        needs_update: True when the head has advanced past the tagged hash.
    """

    script_path: str
    tagged_hash: str
    current_hash: str
    needs_update: bool


class WorkerVersionCheckResponse(BaseModel):
    """Result of checking a worker's script versions against Git HEAD.

    Attributes:
        worker_id: Unique worker identifier.
        scripts: Per-script version diffs.
    """

    worker_id: str
    scripts: list[WorkerVersionDiff]
