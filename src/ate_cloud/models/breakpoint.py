"""Breakpoint SQLAlchemy model.

Durable store for simulation breakpoints. Two shapes share this table:

- T39 typed simulation breakpoints (v41-gap-analysis #39, §8.4): armed on an
  execution run via the ``/executions/{run_id}/breakpoints`` API. They carry
  ``kind`` (step / instrument_call / variable_change / condition), ``target``
  (step id / resource.method / scope.key / "*") and an optional server-side
  ``condition`` expression. ``session_id`` holds the execution ``run_id``.
  These rows are the durable source of truth the edge later receives (task 20).
- Debugpy line breakpoints (legacy, removed in task 21): created via
  ``/debug/breakpoints``. They use ``step_id`` / ``node_id`` / ``line_number``
  / ``node_data`` and leave ``kind`` / ``target`` NULL.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class Breakpoint(Base):
    """Breakpoint database model.

    断点数据库模型 -- 存储仿真断点（T39 typed）与遗留调试断点。

    Attributes:
        id: Unique identifier (UUID as string).
        session_id: Execution run_id (T39) or debug session identifier.
        kind: T39 breakpoint kind: step / instrument_call / variable_change /
            condition; NULL for legacy debugpy rows.
        target: T39 match target (step id / resource.method / scope.key / "*");
            NULL for legacy debugpy rows.
        step_id: Legacy debugpy: test step this breakpoint is attached to
            ("" for T39 rows).
        node_id: Legacy debugpy: X6 graph node for canvas visualisation (""
            for T39 rows).
        line_number: Legacy debugpy line number (0 for T39 rows).
        condition: Optional conditional expression (T39 condition kind; also
            used by legacy debugpy).
        enabled: Whether the breakpoint is active (default True).
        node_data: Legacy debugpy X6 node serialised data (JSON); NULL for
            T39 rows.
        created_at: Timestamp of creation.
        updated_at: Timestamp of last update.
    """

    __tablename__ = "breakpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    step_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True, default="")
    node_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True, default="")
    line_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    condition: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    node_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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
