"""FastAPI dependencies for authentication and authorization.

Provides:
    get_current_user — validates JWT, returns User, checks required scopes.
    require_scopes   — factory returning a dependency that enforces specific scopes.

When ATE_DEV_MODE=true, auth is bypassed entirely (synthetic admin user).
"""

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, SecurityScopes
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.auth.jwt import TokenError, verify_token
from ate_cloud.config import settings
from ate_cloud.db import get_db
from ate_cloud.models.user import User

# auto_error=False so dev_mode bypass works without a token.
_bearer_scheme = HTTPBearer(auto_error=False)


def _dev_user() -> User:
    """Create a synthetic admin user for dev mode bypass."""
    return User(
        id="dev",
        username="dev",
        password_hash="",
        role="admin",
        scopes=[],
        is_active=True,
    )


async def get_current_user(
    security_scopes: SecurityScopes,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Validate the bearer token and return the authenticated user.

    In dev mode, returns a synthetic admin user without checking the token.
    Otherwise, decodes the JWT, fetches the user from the database, and
    verifies the token carries all required scopes.

    Args:
        security_scopes: Scopes required by the protected route (auto-injected).
        credentials: Bearer token credentials (None if dev mode and no header).
        db: Database session.

    Returns:
        The authenticated User.

    Raises:
        HTTPException: 401 if not authenticated or token invalid.
        HTTPException: 403 if required scopes are missing.
    """
    if settings.dev_mode:
        return _dev_user()

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = verify_token(credentials.credentials)
    except TokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify the token carries all required scopes.
    token_scopes = set(payload.get("scopes", []))
    required = set(security_scopes.scopes)
    if required and not required.issubset(token_scopes):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    return user


def require_scopes(*scopes: str) -> Callable[..., Awaitable[User]]:
    """Create a dependency that enforces specific RBAC scopes.

    Usage:
        dependencies=[Depends(require_scopes("read"))]

    The scope check is performed inside get_current_user via SecurityScopes,
    which receives the scopes from the Security() wrapper.

    Args:
        scopes: Required scope strings (e.g., "read", "write", "admin").

    Returns:
        A FastAPI dependency callable.
    """

    async def _dependency(
        user: User = Security(get_current_user, scopes=list(scopes)),
    ) -> User:
        return user

    _dependency.__name__ = f"require_scopes_{'_'.join(scopes) or 'none'}"
    return _dependency
