"""add_plants_stations_and_station_fault_cases

Revision ID: e5f6a7b8c9d0
Revises: c3d4e5f6a7b8
Create Date: 2026-09-30 17:20:00.000000

补上「工位」这个缺失的领域实体, 以及第一期故障诊断所需的数据底座。

为什么这三张表不是可选的
------------------------
故障诊断此前没有工位概念: ``fault_events`` 锚在 ``fixture_topologies`` /
``link_id`` 上, 记的是「哪条工装链路出了故障」。而产测语境里故障定位的主体
就是工位 —— 「W3 这台最近反复在报什么」「同一根因在几条产线出现过」这类问题
没有工位就无从聚合。规格书侧的 FMEA 本体同样如此: 其 ``ProcessStep``
(工序/阶段) 在产测里就是工位。

为什么是关系表而不是知识图谱
---------------------------
有图的形状不等于需要图数据库。FMEA 五类实体在几千行量级上用规范化关系表就够,
真要图遍历时迁移是换读路径而非重写 schema。已有的 DENSO 实证研究也显示, 图谱
相对纯向量检索的增益来自算法(领域概念化 + 工艺感知)而非数据库, 且其最佳结果
F1@20=0.52 仅在单产线 3 场景取得。

索引说明
--------
``ix_stations_plant_code`` 是唯一索引: 工位编码在厂区内唯一, 且上下文选择器
与故障关联都按它查。``ix_fault_cases_station_rpn`` / ``_verified`` 服务
「本工位 RPN 最高的故障」与「本工位已验证的措施」这两个第一期主查询。

``fix_verified`` 默认 false 且非空: 诊断建议只引用已验证的措施。未验证的可作为
候选呈现, 不能被当作「这么修就好了」—— 那等于把未签字的判断当成已签字的。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    # Foreign keys are declared inline rather than added afterwards with
    # ``op.create_foreign_key``. The reason is not stylistic: Alembic cannot add
    # a constraint to an existing SQLite table at all
    # ("No support for ALTER of constraints in SQLite dialect"), so the
    # add-then-alter form makes this migration runnable on Postgres and
    # unrunnable on SQLite — which breaks the existing SQLite migration tests
    # and, more importantly, removes the cheap place where a migration gets
    # exercised automatically.
    #
    # Inline also removes the argument-order trap: ``create_foreign_key(name,
    # source_table, referent_table)`` puts the constraint on the *second* table,
    # so a reversed call fails naming the referenced column and reads like a
    # missing column.
    op.create_table(
        "plants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        # Self-reference with SET NULL: a grouping can be re-parented, and
        # deleting a parent must not take a sub-line with it.
        sa.Column(
            "parent_id",
            sa.String(36),
            sa.ForeignKey("plants.id", name="fk_plants_parent_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        *_timestamps(),
    )
    op.create_index("ix_plants_code", "plants", ["code"], unique=True)
    op.create_index("ix_plants_parent_id", "plants", ["parent_id"])

    op.create_table(
        "stations",
        sa.Column("id", sa.String(36), primary_key=True),
        # CASCADE: a station has no meaning without its plant. Deleting the
        # line removes the stations and their cases, which is intended.
        #
        # Note that the reverse direction is guarded instead: ``plant_id`` is
        # NOT NULL, so deleting a plant that still has stations is refused
        # rather than nulling them out. Refusing is the safer of the two — an
        # orphaned station is invisible to the context selector, which is a
        # worse failure than an operation that asks you to empty the line first.
        sa.Column(
            "plant_id",
            sa.String(36),
            sa.ForeignKey("plants.id", name="fk_stations_plant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("attributes", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        *_timestamps(),
    )
    op.create_index("ix_stations_plant_id", "stations", ["plant_id"])
    op.create_index("ix_stations_code", "stations", ["code"])
    op.create_index("ix_stations_plant_code", "stations", ["plant_id", "code"], unique=True)

    op.create_table(
        "station_fault_cases",
        sa.Column("id", sa.String(36), primary_key=True),
        # CASCADE: a case has no meaning without its station.
        sa.Column(
            "station_id",
            sa.String(36),
            sa.ForeignKey("stations.id", name="fk_fault_cases_station_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("product_code", sa.String(100), nullable=True),
        sa.Column("symptom", sa.Text, nullable=False),
        sa.Column("cause", sa.Text, nullable=True),
        sa.Column("effect", sa.Text, nullable=True),
        sa.Column("fix", sa.Text, nullable=True),
        sa.Column("fix_verified", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("severity", sa.Integer, nullable=True),
        sa.Column("occurrence", sa.Integer, nullable=True),
        sa.Column("detection", sa.Integer, nullable=True),
        sa.Column("rpn", sa.Integer, nullable=True),
        # SET NULL, the opposite of the station cascade: a diagnosis is evidence
        # that can be pruned, and the fault case that outlives it must not be
        # deleted along with it. Cascading here would lose fault history
        # because someone cleaned up a diagnosis.
        sa.Column(
            "source_diagnosis_id",
            sa.String(36),
            sa.ForeignKey(
                "diagnoses.id", name="fk_fault_cases_source_diagnosis_id", ondelete="SET NULL"
            ),
            nullable=True,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        *_timestamps(),
    )
    op.create_index("ix_station_fault_cases_station_id", "station_fault_cases", ["station_id"])
    op.create_index("ix_station_fault_cases_product_code", "station_fault_cases", ["product_code"])
    op.create_index("ix_station_fault_cases_rpn", "station_fault_cases", ["rpn"])
    op.create_index(
        "ix_station_fault_cases_source_diagnosis_id", "station_fault_cases", ["source_diagnosis_id"]
    )
    # The two questions this table exists to answer, both station-scoped.
    op.create_index(
        "ix_fault_cases_station_occurred", "station_fault_cases", ["station_id", "occurred_at"]
    )
    op.create_index(
        "ix_fault_cases_station_rpn", "station_fault_cases", ["station_id", "rpn"]
    )
    op.create_index(
        "ix_fault_cases_station_verified", "station_fault_cases", ["station_id", "fix_verified"]
    )


def downgrade() -> None:
    # Child before parent throughout, so no drop depends on a cascade.
    op.drop_table("station_fault_cases")
    op.drop_table("stations")
    op.drop_table("plants")
