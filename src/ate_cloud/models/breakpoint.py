"""Breakpoint SQLAlchemy model.

Stores debug breakpoints for the debugpy-based breakpoint debugging framework.
Each breakpoint is associated with a debug session, a test step, and an X6
graph node, enabling the sequence editor to visualise breakpoints on the canvas.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class Breakpoint(Base):
    """Breakpoint database model.

    断点数据库模型 -- 存储调试断点，关联调试会话、测试步骤和 X6 节点。

    Attributes:
        id: Unique identifier (UUID as string).
        session_id: Debug session identifier (correlates with execution run_id).
        step_id: Test step identifier this breakpoint is attached to.
        node_id: X6 graph node identifier (for canvas visualisation).
        line_number: Line number within the script (1-based; 0 = any line).
        condition: Optional conditional expression (evaluated when hit).
        enabled: Whether the breakpoint is active (default True).
        node_data: X6 node serialised data for canvas restoration (JSON).
        created_at: Timestamp of creation.
        updated_at: Timestamp of last update.
    """

    __tablename__ = "breakpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    step_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
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
