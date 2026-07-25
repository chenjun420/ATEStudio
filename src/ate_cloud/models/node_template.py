"""NodeTemplate SQLAlchemy model.

This module defines the NodeTemplate database model for storing
node templates that can be used in flow graphs.
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import String, JSON, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class NodeTemplate(Base):
    """NodeTemplate database model.

    Attributes:
        id: Unique identifier (UUID as string).
        name: Human-readable template name.
        type: Node type (e.g., 'start', 'script', 'end').
        appearance: JSON object for visual appearance (position, color, icon).
        default_data: JSON object for default node configuration.
        created_at: Timestamp of creation.
        updated_at: Timestamp of last update.
    """

    __tablename__ = "node_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    appearance: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    default_data: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )