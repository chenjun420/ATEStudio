"""厂区 / 工位 / 工位故障案例。

为什么要有这三张表
------------------
故障诊断这条链路此前**没有「工位」这个概念**。``fault_events`` 锚在
``fixture_topologies`` / ``link_id`` 上 —— 记的是「哪条工装链路出了故障」,
不是「哪个工位出了故障」, 其文档字符串还专门说明热力图聚合
"never fabricates links from instrument/step ids"。

而产测语境里故障定位的主体就是工位:「W3 这台最近反复在报什么」、
「同一根因在几条产线出现过」这类问题, 没有工位就无从聚合。规格书侧的
FMEA 本体同样如此 —— 论文里的 ``ProcessStep``(工序/阶段)在产测里就是工位,
没有这张表它无处安放。

为什么是关系表而不是知识图谱
---------------------------
有图的形状不等于需要图数据库。FMEA 的五类实体(现象/原因/后果/措施 + 工位)
在几千行量级上, 规范化关系表就能满足全部聚合查询; 真需要图遍历时, 迁移是换
读路径而不是重写 schema。DENSO 的实证研究亦显示, 图谱方案相对纯向量检索的
增益来自算法(领域概念化 + 工艺感知), 而非数据库 —— 且其最佳结果
(F1@20 = 0.52) 仅在单产线 3 个场景上取得。

``plant`` 保留 ``parent_id`` 而不做成纯两级:上下文选择器本期是「厂区 → 工位」
两级, 但产线分组(总装/分装)在实践中几乎必然出现, 预留一个自引用列的成本
远低于日后改表。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

# Base is declared in this package's __init__ (not a base submodule), and every
# model in the repo imports it from there.
from ate_cloud.models import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Plant(Base):
    """厂区(或产线分组)。运行阶段上下文选择器的顶层。

    产品与型号不在这里 —— 它们由 ATERag 注册表持有, ATEStudio 侧不建表,
    避免同一事实存在两个真源。
    """

    __tablename__ = "plants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Self-reference reserved for the two-level context becoming three.
    #: NULL = 顶级厂区。
    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    #: NOTE: no delete-orphan cascade here. The database foreign key is
    #: ``ON DELETE SET NULL`` on purpose — deleting a plant grouping must not
    #: take a line's stations and their fault history with it. An ORM-level
    #: ``delete-orphan`` would silently override that intent and issue DELETEs
    #: for the children, so the ORM default (null out the FK) is what we want:
    #: it matches the schema, and the two agreeing is the point.
    stations: Mapped[list[Station]] = relationship(
        back_populates="plant", lazy="selectin"
    )
    parent: Mapped[Plant | None] = relationship(
        remote_side="Plant.id", back_populates="children", lazy="selectin"
    )
    children: Mapped[list[Plant]] = relationship(
        back_populates="parent", cascade="all, delete-orphan", lazy="selectin"
    )


class Station(Base):
    """工位。运行时被观察的对象, 开发时被配置的对象(绑定流程)。

    「阶段 ↔ 工位」对应: 一个测试阶段就是一台工位上的一次执行。
    """

    __tablename__ = "stations"
    __table_args__ = (
        # Codes are looked up constantly by the context selector and by the
        # fault join; a global unique index serves both and keeps the per-plant
        # composite alternative from allowing two stations to share a name
        # within one plant and differ only in case.
        Index("ix_stations_plant_code", "plant_id", "code", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    plant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Free-form station attributes (fixture id, capabilities, calibration
    #: cadence). Text rather than columns: what belongs here differs per line,
    #: and a fixed schema would have to be migrated every time.
    attributes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    plant: Mapped[Plant] = relationship(back_populates="stations", lazy="selectin")
    fault_cases: Mapped[list[StationFaultCase]] = relationship(
        back_populates="station", cascade="all, delete-orphan", lazy="selectin"
    )


class StationFaultCase(Base):
    """一条工位故障案例 —— 第一期「数据采集」的主要产出。

    对应 FMEA 的五类实体: 工位(:attr:`station_id`)、现象、原因、后果、措施。
    风险三因子 S/O/D 与 RPN 是**本实体的属性而非独立节点** —— 这与 FMEA 的
    定义一致(RPN = S x O x D), 也让「本工位 RPN 最高的故障」退化为一次
    ORDER BY, 不需要图遍历。

    ``fix_verified`` 是这张表最要紧的一列: 诊断建议只能引用**已验证**的措施。
    未验证的措施可以是候选, 但不能被当作「这么修就好了」呈现 —— 那等于把
    未签字的判断当成已签字的, 与本项目对产测判据的既有纪律(未批准的条件不得
    成为判据)是同一条线。
    """

    __tablename__ = "station_fault_cases"
    __table_args__ = (
        # The two questions this table exists to answer, both station-scoped:
        # "what keeps happening here" and "what has already been verified here".
        Index("ix_fault_cases_station_occurred", "station_id", "occurred_at"),
        Index("ix_fault_cases_station_rpn", "station_id", "rpn"),
        Index("ix_fault_cases_station_verified", "station_id", "fix_verified"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    station_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Nullable: not every fault is product-specific. A fixture or tooling
    #: fault belongs to the station regardless of what is on the line.
    product_code: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    #: Verbatim observation. The text a future retrieval is matched against, so
    #: it is stored as written rather than normalised into a vocabulary.
    symptom: Mapped[str] = mapped_column(Text, nullable=False)
    cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    effect: Mapped[str | None] = mapped_column(Text, nullable=True)
    fix: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: A measure is only quotable as a remedy once someone has confirmed it
    #: worked. See the class docstring.
    fix_verified: Mapped[bool] = mapped_column(default=False, nullable=False)

    severity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurrence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detection: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Stored, not derived: RPN is recorded as it was assessed, because a
    #: recomputed S*O*D can silently disagree with a number a person signed.
    rpn: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    #: Links a suggestion back to the evidence it came from.
    source_diagnosis_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("diagnoses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    station: Mapped[Station] = relationship(back_populates="fault_cases", lazy="selectin")


__all__ = ["Plant", "Station", "StationFaultCase"]
