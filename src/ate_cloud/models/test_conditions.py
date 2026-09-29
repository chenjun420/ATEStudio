"""TestCondition SQLAlchemy model (P2, ATERag bundle import).

为什么需要这张表
----------------
此前测试条件只能塞进 ``test_requirements.description`` 的自由文本。那样做
有三个具体问题, 都是产测侧会立刻撞上的:

1. **不可执行**: 产测要的是"把交流源设到 110Vac、电子负载设 CC 7.401A",
   自由文本里的"额定输入+额定负载"得靠人翻译, 翻译错了就下错设备指令。
2. **不可聚合**: kind 是封闭词表(25 项), 只能按 kind 汇总才能回答
   "本次共涉及多少个测量点、多少个测量设置"。
3. **不可追溯**: 条件从哪来(规格书原文 / 业界方法补齐)无法区分, 而这两者
   的可信度不同 —— 补齐的是常识前提, 不是规格书写的。

设计取舍
--------
* **owner 用 (owner_type, owner_id) 而非外键**: 条件既可挂需求(定义该测什么),
  也可挂用例(该次测量的具体设定)。用单一 FK 就必须二选一, 而"用例继承需求
  条件"是产测的常态。代价是失去外键约束, 因此 status/kind 由应用层校验。
* **cond_fingerprint 唯一**: 幂等重放的判据。重复导入同一份 bundle 不应
  让条件翻倍 —— 那是最难发现的故障(数字翻倍, 但"看起来正常")。
* **status 默认 draft**: 未签字的条件按红线不得作为产测判据。下游挂执行
  序列前必须校验它为 approved。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from . import Base

#: 条件归属类型
OWNER_REQUIREMENT = "requirement"
OWNER_CASE = "case"

#: 条件侧别
SIDE_INPUT = "input"
SIDE_OUTPUT = "output"

#: 人审状态。未签字不得作为产测判据。
STATUS_DRAFT = "draft"
STATUS_APPROVED = "approved"


class TestCondition(Base):
    """A single test condition clause attached to a requirement or a case.

    Attributes:
        id: UUID (string form).
        owner_type: ``requirement`` | ``case``.
        owner_id: Owning row id (test_requirements.id or test_cases.id).
        side: ``input`` (external state the UUT depends on) | ``output``
            (the UUT's own signal state).
        kind: Closed vocabulary key (25 entries) — the only thing downstream
            aggregates by, so it must never be free text.
        text: Source excerpt or method note, for traceability.
        value: Structured value, e.g. ``{"min": 100, "max": 240, "unit": "Vac"}``.
        source: Provenance (notes / limits / industry_method / ...).
        confidence: rule | annotated | proposed.
        status: draft | approved — an unapproved condition must not be used
            as a production criterion.
        method_ref: ``test_methods.yaml`` method id when supplemented.
        cond_fingerprint: Stable content fingerprint; unique per owner.
        created_at / updated_at: Timestamps.
    """

    # Not a pytest test class — the name starts with "Test" so tell pytest not
    # to collect it (suppresses PytestCollectionWarning "cannot collect").
    __test__ = False

    __tablename__ = "test_conditions"
    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", "cond_fingerprint", name="uq_test_conditions_owner_fp"),
        CheckConstraint("side IN ('input', 'output')", name="ck_test_conditions_side"),
        Index("ix_test_conditions_owner", "owner_type", "owner_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="notes")
    confidence: Mapped[str] = mapped_column(String(32), nullable=False, default="rule")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_DRAFT, index=True)
    method_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cond_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
