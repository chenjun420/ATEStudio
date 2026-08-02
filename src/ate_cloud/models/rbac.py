"""RBAC models for database-driven role and permission management.

Stores roles and permissions in the database, enabling dynamic RBAC
configuration without code changes. System roles (admin, read, write,
execute) are seeded via the /rbac/roles/seed endpoint and marked with
is_system=True to prevent deletion.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class Role(Base):
    """SQLAlchemy model for RBAC roles.

    Attributes:
        id: Unique role identifier (UUID4).
        name: Role name (unique, e.g. "admin", "read", "write", "execute").
        description: Optional human-readable description.
        is_system: System roles cannot be deleted (admin, read, write, execute).
        is_active: Whether the role is active and grants permissions.
        created_at: Timestamp of role creation.
        updated_at: Timestamp of last update.
        permissions: List of permission codes granted by this role.
    """

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
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
    permissions: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)


class Permission(Base):
    """SQLAlchemy model for RBAC permissions.

    Attributes:
        id: Unique permission identifier (UUID4).
        code: Permission code (unique, e.g. "node:read", "flow:write", "exec:run").
        module: Module category ("node", "flow", "exec", "system", "auth", "user").
        description: Optional human-readable description.
        created_at: Timestamp of permission creation.
    """

    __tablename__ = "permissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    module: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
