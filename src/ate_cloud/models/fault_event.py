"""FaultEvent SQLAlchemy model (RH-1, v41-remaining-hardening #1).

设计文档 §8.3 历史故障热力图持久化：``fault_events`` 表记录每次故障注入
（T44 链路右键注入 / T38 手动面板注入 / 预留 worker 侧 scheduler 中继），
供 ``GET /fixtures/{fixture_id}/fault-stats`` 聚合 per-link count/last_seen。

写入契约：cloud 侧写路径在 NATS 控制发布成功之后落库，DB 失败仅告警、
绝不阻断注入主流程（见 executions.py::_persist_fault_event）。
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from . import Base

# JSON 文档列：PostgreSQL 用 JSONB（规格要求），SQLite/MySQL 退化为 JSON。
JSONDocument = JSONB().with_variant(JSON(), "sqlite").with_variant(JSON(), "mysql")

# source 取值：链路右键注入 | 手动面板注入 | worker 调度中继（预留）。
SOURCE_LINK = "link"
SOURCE_MANUAL = "manual"
SOURCE_SCHEDULER = "scheduler"


class FaultEvent(Base):
    """One persisted fault-injection event (design doc §8.3 heatmap).

    Attributes:
        id: UUID (string form).
        fixture_id: FK to fixture_topologies.id; nullable — cloud-side write
            paths have no execution→fixture binding yet, so read-time
            attribution matches on link_id against the fixture's topology.
        run_id: Execution run identifier (indexed).
        link_id: Topology link id; empty string for non-link manual scopes
            (instrument/step/scheduler/protocol) so the heatmap aggregation
            never fabricates links from instrument/step ids.
        fault_type: Fault type string (§7.7 vocabulary).
        source: ``link`` | ``manual`` | ``scheduler``.
        detail: JSON payload (fault_id/scope/layer/target_id/params).
        created_at: tz-aware creation timestamp.
    """

    __tablename__ = "fault_events"
    __table_args__ = (
        Index("ix_fault_events_fixture_id_created_at", "fixture_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fixture_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("fixture_topologies.id"), nullable=True
    )
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    link_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    fault_type: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONDocument, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
