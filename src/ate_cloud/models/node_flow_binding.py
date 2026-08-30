"""NodeFlowBinding model — links workers (NATS KV) to sequences (DB).

Each binding associates a worker_id (stored in the ``ate-workers`` JetStream
KV bucket, NOT a DB FK) with a sequence (FK to ``sequences.id``). A single
worker may have multiple bindings with different priorities, and a single
sequence may be bound to multiple workers.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from . import Base

if TYPE_CHECKING:
    from .sequence import Sequence


class NodeFlowBinding(Base):
    """SQLAlchemy model for node-flow bindings.

    Attributes:
        id: Unique binding identifier (UUID4, String(36)).
        worker_id: Worker identifier (not a FK — workers live in NATS KV).
        sequence_id: FK to ``sequences.id``.
        is_active: Whether this binding is currently active.
        priority: Ordering priority for multiple bindings (lower = higher priority).
        config: Optional override config for executions triggered via this binding.
        created_at: Timestamp of record creation.
        updated_at: Timestamp of last update.
        sequence: Relationship to the :class:`~ate_cloud.models.sequence.Sequence` model.
    """

    __tablename__ = "node_flow_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    sequence_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sequences.id"), nullable=False, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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

    sequence: Mapped["Sequence"] = relationship("Sequence")
