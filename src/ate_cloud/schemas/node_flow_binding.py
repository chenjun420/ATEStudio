"""Pydantic schemas for NodeFlowBinding API responses.

Defines request/response models for the node-flow-binding endpoints:
- ``NodeFlowBindingCreate`` — request body for creating a binding.
- ``NodeFlowBindingUpdate`` — request body for partial updates.
- ``NodeFlowBindingResponse`` — full binding data with sequence name.
- ``NodeFlowBindingListResponse`` — paginated-style list of bindings.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NodeFlowBindingCreate(BaseModel):
    """Request body for creating a new node-flow binding.

    Attributes:
        worker_id: Worker identifier (exists in NATS KV, not DB).
        sequence_id: Target sequence UUID.
        is_active: Whether the binding is active immediately.
        priority: Ordering priority (lower = higher priority).
        config: Optional override config for executions via this binding.
    """

    worker_id: str = Field(..., min_length=1, max_length=255)
    sequence_id: str = Field(..., min_length=1, max_length=36)
    is_active: bool = True
    priority: int = 0
    config: dict[str, Any] | None = None


class NodeFlowBindingUpdate(BaseModel):
    """Request body for partially updating a node-flow binding.

    All fields are optional to support partial updates.

    Attributes:
        is_active: Updated active status.
        priority: Updated priority.
        config: Updated override config.
    """

    is_active: bool | None = None
    priority: int | None = None
    config: dict[str, Any] | None = None


class NodeFlowBindingResponse(BaseModel):
    """Full binding data returned by the API.

    Attributes:
        id: Unique binding identifier.
        worker_id: Worker identifier.
        sequence_id: Target sequence UUID.
        is_active: Whether the binding is active.
        priority: Ordering priority.
        config: Override config (may be None).
        sequence_name: Name of the bound sequence (joined from Sequence table).
        created_at: Timestamp of creation.
        updated_at: Timestamp of last update.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    worker_id: str
    sequence_id: str
    is_active: bool
    priority: int
    config: dict[str, Any] | None = None
    sequence_name: str | None = None
    created_at: datetime
    updated_at: datetime


class NodeFlowBindingListResponse(BaseModel):
    """Paginated-style list of node-flow bindings.

    Attributes:
        items: List of binding responses.
        total: Total number of bindings.
    """

    items: list[NodeFlowBindingResponse]
    total: int
