"""Pydantic schemas for RBAC management endpoints.

Defines request/response models for role and permission CRUD operations.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class RoleCreateRequest(BaseModel):
    """Request body for creating a new role.

    Attributes:
        name: Role name (1-50 characters, unique).
        description: Optional human-readable description.
        permissions: Optional list of permission codes to assign to this role.
    """

    name: str = Field(..., min_length=1, max_length=50)
    description: str | None = Field(default=None, max_length=500)
    permissions: list[str] | None = None


class RoleUpdateRequest(BaseModel):
    """Request body for partially updating a role.

    All fields are optional to support partial updates.

    Attributes:
        name: Updated role name (cannot change for system roles).
        description: Updated description.
        permissions: Updated list of permission codes.
        is_active: Updated active status.
    """

    name: str | None = Field(default=None, min_length=1, max_length=50)
    description: str | None = Field(default=None, max_length=500)
    permissions: list[str] | None = None
    is_active: bool | None = None


class RoleResponse(BaseModel):
    """Role data returned by RBAC endpoints.

    Attributes:
        id: Unique role identifier.
        name: Role name.
        description: Human-readable description.
        is_system: Whether this is a system role (cannot be deleted).
        is_active: Whether the role is active.
        permissions: List of permission codes granted by this role.
        created_at: Timestamp of role creation.
    """

    id: str
    name: str
    description: str | None
    is_system: bool
    is_active: bool
    permissions: list[str] = Field(default_factory=list)
    created_at: datetime

    model_config = {"from_attributes": True}


class RoleListResponse(BaseModel):
    """Paginated-style list of roles.

    Attributes:
        items: List of role responses.
        total: Total number of roles.
    """

    items: list[RoleResponse]
    total: int


class PermissionResponse(BaseModel):
    """Permission data returned by RBAC endpoints.

    Attributes:
        id: Unique permission identifier.
        code: Permission code (e.g. "node:read", "flow:write").
        module: Module category (e.g. "node", "flow", "exec").
        description: Human-readable description.
        created_at: Timestamp of permission creation.
    """

    id: str
    code: str
    module: str
    description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class PermissionListResponse(BaseModel):
    """Paginated-style list of permissions.

    Attributes:
        items: List of permission responses.
        total: Total number of permissions.
    """

    items: list[PermissionResponse]
    total: int
