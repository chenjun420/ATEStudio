"""User model for authentication and authorization.

Stores user credentials, role, and explicit scopes for RBAC.
Password is stored as an Argon2 hash via pwdlib.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class User(Base):
    """SQLAlchemy model for user accounts.

    Attributes:
        id: Unique user identifier (UUID4).
        username: Login username (unique).
        password_hash: Argon2 password hash.
        role: RBAC role (admin/read/write/execute).
        scopes: Optional explicit scopes that augment role-based scopes.
        is_active: Whether the user can authenticate.
        theme_mode: UI theme preference ("light", "dark", "auto").
        language: UI language preference ("en", "zh-CN").
        created_at: Timestamp of account creation.
        updated_at: Timestamp of last update.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    scopes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    theme_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="auto")
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="en")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
