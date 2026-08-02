"""Pydantic schemas for user management endpoints.

Defines request/response models for user CRUD, preferences, and seeding.
Reuses UserResponse from auth schemas for single-user responses.
"""

from typing import Literal

from pydantic import BaseModel, Field

from ate_cloud.schemas.auth import UserResponse


class UserCreateRequest(BaseModel):
    """Request body for creating a new user.

    Attributes:
        username: Login name (1-255 characters, unique).
        password: Plaintext password (will be hashed before storage).
        role: RBAC role (admin/read/write/execute).
        scopes: Optional explicit scopes augmenting the role.
        is_active: Whether the account is active immediately.
    """

    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1)
    role: Literal["admin", "read", "write", "execute"]
    scopes: list[str] | None = None
    is_active: bool = True


class UserUpdateRequest(BaseModel):
    """Request body for partially updating a user.

    All fields are optional to support partial updates.

    Attributes:
        password: New plaintext password (will be hashed).
        role: Updated RBAC role.
        scopes: Updated explicit scopes.
        is_active: Updated active status.
    """

    password: str | None = Field(default=None, min_length=1)
    role: Literal["admin", "read", "write", "execute"] | None = None
    scopes: list[str] | None = None
    is_active: bool | None = None


class UserListResponse(BaseModel):
    """Paginated-style list of users.

    Attributes:
        items: List of user responses.
        total: Total number of users.
    """

    items: list[UserResponse]
    total: int


class UserPreferencesRequest(BaseModel):
    """Request body for updating user preferences.

    All fields are optional to support partial updates.

    Attributes:
        theme_mode: UI theme preference ("light", "dark", "auto").
        language: UI language preference ("en", "zh-CN").
    """

    theme_mode: Literal["light", "dark", "auto"] | None = None
    language: Literal["en", "zh-CN"] | None = None


class UserPreferencesResponse(BaseModel):
    """User preferences returned by the preferences endpoints.

    Attributes:
        theme_mode: Current UI theme preference.
        language: Current UI language preference.
    """

    theme_mode: str
    language: str


class PasswordChangeRequest(BaseModel):
    """Request body for changing the current user's password.

    Attributes:
        old_password: Current plaintext password (for verification).
        new_password: New plaintext password (minimum 8 characters, will be hashed).
    """

    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)
