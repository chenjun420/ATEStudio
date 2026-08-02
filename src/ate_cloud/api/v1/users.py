"""User management API endpoints.

Provides:
    POST   /api/v1/users                   — create user (admin)
    GET    /api/v1/users                   — list all users (read)
    GET    /api/v1/users/me/preferences    — get current user preferences
    PUT    /api/v1/users/me/preferences    — update current user preferences
    PUT    /api/v1/users/me/password       — change current user password
    POST   /api/v1/users/me/deactivate     — deactivate current user account
    POST   /api/v1/users/seed-admin        — seed default admin (idempotent)
    GET    /api/v1/users/{user_id}         — get user by id (read)
    PUT    /api/v1/users/{user_id}         — update user (admin)
    DELETE /api/v1/users/{user_id}         — delete user (admin)
"""

import os
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, Security, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.auth.dependencies import get_current_user
from ate_cloud.auth.password import hash_password, verify_password
from ate_cloud.db import get_db
from ate_cloud.models.user import User
from ate_cloud.schemas.auth import UserResponse
from ate_cloud.schemas.user import (
    PasswordChangeRequest,
    UserCreateRequest,
    UserListResponse,
    UserPreferencesRequest,
    UserPreferencesResponse,
    UserUpdateRequest,
)

router = APIRouter(prefix="/users", tags=["users"])

DBSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    request: UserCreateRequest,
    db: DBSession,
    current_user: User = Security(get_current_user, scopes=["admin"]),
) -> UserResponse:
    """Create a new user (admin scope required).

    Args:
        request: User creation data.
        db: Database session.
        current_user: Authenticated admin user.

    Returns:
        UserResponse: The created user.

    Raises:
        HTTPException: 409 if username already exists.
    """
    existing = await db.execute(select(User).where(User.username == request.username))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already exists",
        )

    user = User(
        id=str(uuid.uuid4()),
        username=request.username,
        password_hash=hash_password(request.password),
        role=request.role,
        scopes=request.scopes,
        is_active=request.is_active,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.get("", response_model=UserListResponse)
async def list_users(
    db: DBSession,
    current_user: User = Security(get_current_user, scopes=["read"]),
) -> UserListResponse:
    """List all users (read scope required).

    Args:
        db: Database session.
        current_user: Authenticated user with read scope.

    Returns:
        UserListResponse with items and total count.
    """
    count_result = await db.execute(select(func.count()).select_from(User))
    total: int = count_result.scalar() or 0

    result = await db.execute(select(User).order_by(User.created_at.asc()))
    users = result.scalars().all()

    items = [UserResponse.model_validate(u) for u in users]
    return UserListResponse(items=items, total=total)


@router.get("/me/preferences", response_model=UserPreferencesResponse)
async def get_my_preferences(
    current_user: User = Depends(get_current_user),
) -> UserPreferencesResponse:
    """Get the current user's preferences (any authenticated user).

    Args:
        current_user: The authenticated user.

    Returns:
        UserPreferencesResponse with theme_mode and language.
    """
    return UserPreferencesResponse(
        theme_mode=current_user.theme_mode,
        language=current_user.language,
    )


@router.put("/me/preferences", response_model=UserPreferencesResponse)
async def update_my_preferences(
    request: UserPreferencesRequest,
    db: DBSession,
    current_user: User = Depends(get_current_user),
) -> UserPreferencesResponse:
    """Update the current user's preferences (any authenticated user).

    Args:
        request: Partial preferences update data.
        db: Database session.
        current_user: The authenticated user.

    Returns:
        UserPreferencesResponse with updated theme_mode and language.
    """
    update_data = request.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(current_user, key, value)

    await db.commit()
    await db.refresh(current_user)
    return UserPreferencesResponse(
        theme_mode=current_user.theme_mode,
        language=current_user.language,
    )


@router.put("/me/password", response_model=UserResponse)
async def change_my_password(
    request: PasswordChangeRequest,
    db: DBSession,
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    """Change the current user's password (any authenticated user).

    Verifies the old password against the stored hash before updating.

    Args:
        request: Password change data (old_password, new_password).
        db: Database session.
        current_user: The authenticated user.

    Returns:
        UserResponse: The updated user.

    Raises:
        HTTPException: 400 if the old password is incorrect.
    """
    if not verify_password(request.old_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Old password is incorrect",
        )

    current_user.password_hash = hash_password(request.new_password)
    await db.commit()
    await db.refresh(current_user)
    return UserResponse.model_validate(current_user)


@router.post("/me/deactivate", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_my_account(
    db: DBSession,
    current_user: User = Depends(get_current_user),
) -> None:
    """Deactivate the current user's account (any authenticated user).

    Sets is_active=False, preventing future logins. The account data is
    retained. Cannot deactivate if the user is the last active admin.

    Args:
        db: Database session.
        current_user: The authenticated user.

    Raises:
        HTTPException: 403 if the user is the last active admin.
    """
    if current_user.role == "admin":
        admin_count_result = await db.execute(
            select(func.count())
            .select_from(User)
            .where(User.role == "admin", User.is_active.is_(True))
        )
        active_admin_count: int = admin_count_result.scalar() or 0
        if active_admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot deactivate the last active admin account",
            )

    current_user.is_active = False
    await db.commit()


@router.post("/seed-admin", response_model=UserResponse)
async def seed_admin(
    db: DBSession,
    response: Response,
) -> UserResponse:
    """Seed a default admin user if no users exist (idempotent).

    Username defaults to "admin". Password is read from the
    ATE_ADMIN_PASSWORD environment variable.

    Args:
        db: Database session.
        response: FastAPI response (status_code set dynamically).

    Returns:
        UserResponse: The seeded or existing admin user.

    Raises:
        HTTPException: 500 if ATE_ADMIN_PASSWORD is not set and no users exist.
    """
    count_result = await db.execute(select(func.count()).select_from(User))
    user_count: int = count_result.scalar() or 0

    if user_count > 0:
        result = await db.execute(select(User).where(User.role == "admin").limit(1))
        existing_admin = result.scalar_one_or_none()
        if existing_admin is not None:
            response.status_code = status.HTTP_200_OK
            return UserResponse.model_validate(existing_admin)
        result = await db.execute(select(User).limit(1))
        first_user = result.scalar_one_or_none()
        if first_user is not None:
            response.status_code = status.HTTP_200_OK
            return UserResponse.model_validate(first_user)

    admin_password = os.environ.get("ATE_ADMIN_PASSWORD")
    if not admin_password:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ATE_ADMIN_PASSWORD environment variable not set",
        )

    admin_username = os.environ.get("ATE_ADMIN_USERNAME", "admin")
    user = User(
        id=str(uuid.uuid4()),
        username=admin_username,
        password_hash=hash_password(admin_password),
        role="admin",
        scopes=None,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    response.status_code = status.HTTP_201_CREATED
    return UserResponse.model_validate(user)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    db: DBSession,
    current_user: User = Security(get_current_user, scopes=["read"]),
) -> UserResponse:
    """Get a single user by ID (read scope required).

    Args:
        user_id: The unique user identifier.
        db: Database session.
        current_user: Authenticated user with read scope.

    Returns:
        UserResponse: The user data.

    Raises:
        HTTPException: 404 if user not found.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return UserResponse.model_validate(user)


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    request: UserUpdateRequest,
    db: DBSession,
    current_user: User = Security(get_current_user, scopes=["admin"]),
) -> UserResponse:
    """Update an existing user (admin scope required).

    Args:
        user_id: The unique user identifier.
        request: Partial update data.
        db: Database session.
        current_user: Authenticated admin user.

    Returns:
        UserResponse: The updated user.

    Raises:
        HTTPException: 404 if user not found.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    update_data = request.model_dump(exclude_unset=True)
    if "password" in update_data:
        user.password_hash = hash_password(update_data.pop("password"))
    for key, value in update_data.items():
        setattr(user, key, value)

    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    db: DBSession,
    current_user: User = Security(get_current_user, scopes=["admin"]),
) -> None:
    """Delete a user (admin scope required).

    Args:
        user_id: The unique user identifier.
        db: Database session.
        current_user: Authenticated admin user.

    Raises:
        HTTPException: 404 if user not found.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    await db.delete(user)
    await db.commit()
