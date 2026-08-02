"""Pydantic schemas for authentication endpoints.

Defines request/response models for login, token refresh, and user info.
"""

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    """Login request with username and password.

    Attributes:
        username: User's login name.
        password: Plaintext password.
    """

    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1)


class RegisterRequest(BaseModel):
    """Registration request with username and password.

    Attributes:
        username: Desired login name (1-255 characters, unique).
        password: Plaintext password (minimum 8 characters).
    """

    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8)


class RefreshRequest(BaseModel):
    """Token refresh request.

    Attributes:
        refresh_token: A valid (non-consumed) refresh token.
    """

    refresh_token: str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    """Access + refresh token pair returned by login and refresh.

    Attributes:
        access_token: Short-lived JWT access token (RS256).
        refresh_token: Longer-lived JWT refresh token (rotation).
        token_type: Always "Bearer".
        expires_in: Access token lifetime in seconds.
    """

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int


class UserResponse(BaseModel):
    """User info returned by GET /auth/me.

    Attributes:
        id: Unique user identifier.
        username: Login name.
        role: RBAC role (admin/read/write/execute).
        scopes: Explicit scopes (augments role-based scopes).
        is_active: Whether the account is active.
    """

    id: str
    username: str
    role: str
    scopes: list[str] = Field(default_factory=list)
    is_active: bool

    model_config = {"from_attributes": True}

    @field_validator("scopes", mode="before")
    @classmethod
    def _scopes_none_to_list(cls, v: list[str] | None) -> list[str]:
        """Convert None scopes to empty list for consistent API output."""
        return v if v is not None else []
