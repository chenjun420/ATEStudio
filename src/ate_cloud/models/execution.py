"""Execution model for tracking test run lifecycle.

Stores execution state including run_id, sequence association,
status progression, timing, and result/error data.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class Execution(Base):
    """SQLAlchemy model for execution records.

    Attributes:
        id: Unique execution identifier (= run_id, UUID4).
        sequence_id: Optional reference to the sequence being executed.
        status: Current execution state (PENDING, RUNNING, COMPLETED, FAILED, ABORTED).
        config: Optional execution configuration (max_concurrency, etc.).
        result: Optional final result summary.
        error: Optional error message on failure.
        started_at: Timestamp when execution transitioned to RUNNING.
        completed_at: Timestamp when execution reached a terminal state.
        created_at: Timestamp of record creation.
        updated_at: Timestamp of last update.
    """

    __tablename__ = "executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sequence_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
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
