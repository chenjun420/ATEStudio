"""Authentication API endpoints.

Provides:
    POST /api/v1/auth/login     — authenticate with username/password, return tokens
    POST /api/v1/auth/register  — public registration, auto-login after register
    POST /api/v1/auth/refresh   — exchange refresh token for new token pair (rotation)
    GET  /api/v1/auth/me        — return current authenticated user info
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.auth.dependencies import get_current_user
from ate_cloud.auth.jwt import (
    TokenError,
    consume_refresh_token,
    create_access_token,
    create_refresh_token,
    verify_token,
)
from ate_cloud.auth.password import hash_password, verify_password
from ate_cloud.auth.rbac import get_effective_scopes
from ate_cloud.config import settings
from ate_cloud.db import get_db
from ate_cloud.models.user import User
from ate_cloud.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    request: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Authenticate a user and return access + refresh tokens.

    Args:
        request: Login credentials (username, password).
        db: Database session.

    Returns:
        TokenResponse with access and refresh tokens.

    Raises:
        HTTPException: 401 if credentials are invalid.
    """
    result = await db.execute(select(User).where(User.username == request.username))
    user = result.scalar_one_or_none()

    if (
        user is None
        or not user.is_active
        or not verify_password(request.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    scopes = get_effective_scopes(user.role, user.scopes)
    access_token = create_access_token(user.id, scopes)
    refresh_token = create_refresh_token(user.id)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.jwt_expire_minutes * 60,
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    request: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Register a new user and return tokens (auto-login).

    Creates a user with role="read" and is_active=True. If the username
    already exists, returns 409 Conflict.

    Args:
        request: Registration data (username, password).
        db: Database session.

    Returns:
        TokenResponse with access and refresh tokens for the new user.

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
        role="read",
        scopes=None,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    scopes = get_effective_scopes(user.role, user.scopes)
    access_token = create_access_token(user.id, scopes)
    refresh_token = create_refresh_token(user.id)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.jwt_expire_minutes * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Exchange a refresh token for a new access + refresh token pair.

    Implements refresh token rotation: the submitted refresh token is
    consumed (revoked) and a new pair is issued.

    Args:
        request: Refresh request containing the refresh token.
        db: Database session.

    Returns:
        TokenResponse with new access and refresh tokens.

    Raises:
        HTTPException: 401 if the refresh token is invalid, expired, or revoked.
    """
    try:
        payload = verify_token(request.refresh_token, expected_type="refresh")
    except TokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        ) from e

    jti = payload.get("jti")
    user_id = consume_refresh_token(jti) if jti else None
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token revoked or invalid",
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    scopes = get_effective_scopes(user.role, user.scopes)
    new_access = create_access_token(user.id, scopes)
    new_refresh = create_refresh_token(user.id)

    return TokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_in=settings.jwt_expire_minutes * 60,
    )


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(get_current_user)) -> UserResponse:
    """Return the current authenticated user's info.

    Args:
        user: The authenticated user (injected by get_current_user).

    Returns:
        UserResponse with user details.
    """
    return UserResponse.model_validate(user)
