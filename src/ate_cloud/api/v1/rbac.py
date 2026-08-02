"""RBAC management API endpoints.

Provides:
    GET    /api/v1/rbac/permissions       — list all permissions (read)
    GET    /api/v1/rbac/roles             — list all roles (read)
    GET    /api/v1/rbac/roles/{role_id}   — get role by id (read)
    POST   /api/v1/rbac/roles             — create role (admin)
    PUT    /api/v1/rbac/roles/{role_id}   — update role (admin)
    DELETE /api/v1/rbac/roles/{role_id}   — delete role (admin)
    POST   /api/v1/rbac/roles/seed        — idempotent seed of default permissions + system roles
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Security, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.auth.dependencies import get_current_user
from ate_cloud.db import get_db
from ate_cloud.models.rbac import Permission, Role
from ate_cloud.models.user import User
from ate_cloud.schemas.rbac import (
    PermissionListResponse,
    PermissionResponse,
    RoleCreateRequest,
    RoleListResponse,
    RoleResponse,
    RoleUpdateRequest,
)

router = APIRouter(prefix="/rbac", tags=["rbac"])

DBSession = Annotated[AsyncSession, Depends(get_db)]

# Default permissions to seed: (code, module, description)
_DEFAULT_PERMISSIONS: list[tuple[str, str, str]] = [
    ("node:read", "node", "Read node templates"),
    ("node:write", "node", "Create and modify node templates"),
    ("flow:read", "flow", "Read test flows and sequences"),
    ("flow:write", "flow", "Create and modify test flows and sequences"),
    ("exec:run", "exec", "Start test executions"),
    ("exec:read", "exec", "View execution history and status"),
    ("system:read", "system", "Read system configuration"),
    ("system:write", "system", "Modify system configuration"),
    ("auth:read", "auth", "View auth and user information"),
    ("auth:write", "auth", "Modify auth settings"),
    ("user:read", "user", "View user accounts"),
    ("user:write", "user", "Create and modify user accounts"),
    ("admin", "system", "Full administrative access"),
]

# Default system roles to seed: (name, description, permissions)
_DEFAULT_SYSTEM_ROLES: list[tuple[str, str, list[str]]] = [
    (
        "admin",
        "Administrator with full access to all modules",
        [p[0] for p in _DEFAULT_PERMISSIONS],
    ),
    (
        "write",
        "Read and write access to most modules",
        [
            "node:read", "node:write",
            "flow:read", "flow:write",
            "exec:run", "exec:read",
            "system:read",
            "auth:read",
            "user:read", "user:write",
        ],
    ),
    (
        "read",
        "Read-only access to most modules",
        [
            "node:read",
            "flow:read",
            "exec:read",
            "system:read",
            "auth:read",
            "user:read",
        ],
    ),
    (
        "execute",
        "Execution-only access (run and view executions)",
        ["exec:run", "exec:read"],
    ),
]


@router.get("/permissions", response_model=PermissionListResponse)
async def list_permissions(
    db: DBSession,
    current_user: User = Security(get_current_user, scopes=["read"]),
) -> PermissionListResponse:
    """List all permissions (read scope required).

    Args:
        db: Database session.
        current_user: Authenticated user with read scope.

    Returns:
        PermissionListResponse with items and total count.
    """
    count_result = await db.execute(select(func.count()).select_from(Permission))
    total: int = count_result.scalar() or 0

    result = await db.execute(select(Permission).order_by(Permission.code.asc()))
    permissions = result.scalars().all()

    items = [PermissionResponse.model_validate(p) for p in permissions]
    return PermissionListResponse(items=items, total=total)


@router.get("/roles", response_model=RoleListResponse)
async def list_roles(
    db: DBSession,
    current_user: User = Security(get_current_user, scopes=["read"]),
) -> RoleListResponse:
    """List all roles (read scope required).

    Args:
        db: Database session.
        current_user: Authenticated user with read scope.

    Returns:
        RoleListResponse with items and total count.
    """
    count_result = await db.execute(select(func.count()).select_from(Role))
    total: int = count_result.scalar() or 0

    result = await db.execute(select(Role).order_by(Role.created_at.asc()))
    roles = result.scalars().all()

    items = [RoleResponse.model_validate(r) for r in roles]
    return RoleListResponse(items=items, total=total)


@router.get("/roles/{role_id}", response_model=RoleResponse)
async def get_role(
    role_id: str,
    db: DBSession,
    current_user: User = Security(get_current_user, scopes=["read"]),
) -> RoleResponse:
    """Get a single role by ID (read scope required).

    Args:
        role_id: The unique role identifier.
        db: Database session.
        current_user: Authenticated user with read scope.

    Returns:
        RoleResponse: The role data.

    Raises:
        HTTPException: 404 if role not found.
    """
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    return RoleResponse.model_validate(role)


@router.post("/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    request: RoleCreateRequest,
    db: DBSession,
    current_user: User = Security(get_current_user, scopes=["admin"]),
) -> RoleResponse:
    """Create a new role (admin scope required).

    Args:
        request: Role creation data.
        db: Database session.
        current_user: Authenticated admin user.

    Returns:
        RoleResponse: The created role.

    Raises:
        HTTPException: 409 if role name already exists.
    """
    existing = await db.execute(select(Role).where(Role.name == request.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Role name already exists",
        )

    role = Role(
        id=str(uuid.uuid4()),
        name=request.name,
        description=request.description,
        is_system=False,
        is_active=True,
        permissions=request.permissions,
    )
    db.add(role)
    await db.commit()
    await db.refresh(role)
    return RoleResponse.model_validate(role)


@router.put("/roles/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: str,
    request: RoleUpdateRequest,
    db: DBSession,
    current_user: User = Security(get_current_user, scopes=["admin"]),
) -> RoleResponse:
    """Update an existing role (admin scope required).

    System roles' name cannot be changed.

    Args:
        role_id: The unique role identifier.
        request: Partial update data.
        db: Database session.
        current_user: Authenticated admin user.

    Returns:
        RoleResponse: The updated role.

    Raises:
        HTTPException: 404 if role not found.
        HTTPException: 400 if trying to rename a system role.
        HTTPException: 409 if the new name already exists.
    """
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")

    update_data = request.model_dump(exclude_unset=True)

    if "name" in update_data and role.is_system and update_data["name"] != role.name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change the name of a system role",
        )

    if "name" in update_data and update_data["name"] != role.name:
        name_existing = await db.execute(
            select(Role).where(Role.name == update_data["name"])
        )
        if name_existing.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Role name already exists",
            )

    for key, value in update_data.items():
        setattr(role, key, value)

    await db.commit()
    await db.refresh(role)
    return RoleResponse.model_validate(role)


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: str,
    db: DBSession,
    current_user: User = Security(get_current_user, scopes=["admin"]),
) -> None:
    """Delete a role (admin scope required).

    System roles cannot be deleted.

    Args:
        role_id: The unique role identifier.
        db: Database session.
        current_user: Authenticated admin user.

    Raises:
        HTTPException: 404 if role not found.
        HTTPException: 403 if the role is a system role.
    """
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")

    if role.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete a system role",
        )

    await db.delete(role)
    await db.commit()


@router.post("/roles/seed", response_model=RoleListResponse)
async def seed_roles(
    db: DBSession,
    current_user: User = Security(get_current_user, scopes=["admin"]),
) -> RoleListResponse:
    """Idempotently seed default permissions and system roles (admin scope required).

    Seeds all default permissions and the four system roles (admin, write,
    read, execute) with their respective permission sets. If a permission
    code or role name already exists, it is skipped.

    Args:
        db: Database session.
        current_user: Authenticated admin user.

    Returns:
        RoleListResponse with all roles after seeding.
    """
    # Seed permissions
    for code, module, description in _DEFAULT_PERMISSIONS:
        existing = await db.execute(select(Permission).where(Permission.code == code))
        if existing.scalar_one_or_none() is None:
            db.add(
                Permission(
                    id=str(uuid.uuid4()),
                    code=code,
                    module=module,
                    description=description,
                )
            )

    # Seed system roles
    for name, description, permissions in _DEFAULT_SYSTEM_ROLES:
        existing = await db.execute(select(Role).where(Role.name == name))
        if existing.scalar_one_or_none() is None:
            db.add(
                Role(
                    id=str(uuid.uuid4()),
                    name=name,
                    description=description,
                    is_system=True,
                    is_active=True,
                    permissions=permissions,
                )
            )

    await db.commit()

    # Return all roles
    result = await db.execute(select(Role).order_by(Role.created_at.asc()))
    roles = result.scalars().all()
    items = [RoleResponse.model_validate(r) for r in roles]
    return RoleListResponse(items=items, total=len(items))
