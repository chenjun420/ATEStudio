"""add_test_conditions_table_and_aterag_bundle

Revision ID: b7c8d9e0f1a2
Revises: d1e2f3a4b5c6
Create Date: 2026-09-30 10:00:00.000000

ATERag bundle 导入所需的三处 schema 变更 (P2):

1. **新表 ``test_conditions``** —— 测试条件 (输入/输出子句) 的一等公民。
   此前条件只能塞进 ``test_requirements.description`` 的自由文本, 不可执行、
   不可按 kind 聚合, 产测拿不到"该设什么激励"。本表按 (owner_type, owner_id)
   挂在需求或用例下, 带 kind 与结构化值, 并以 ``cond_fingerprint`` 唯一索引
   保证幂等重放。``status`` 区分 approved/draft: 未签字的条件按红线不得生效。

2. **``test_limits.spec_low`` / ``spec_high`` 改可空 + 新增 ``spec_typ``**。
   原模型两侧都 NOT NULL 且校验 high >= low, 但规格书里单边限值是常态
   (只有上限的过压保护、只有下限的欠压保护), 三值并存的"整定值"
   (min/typ/max) 更无处安放。硬塞会直接写不进去, 或者被迫编造缺失的那一侧
   —— 编造判据等于伪造质量标准。故允许单边, 禁止两侧皆空, 并新增
   ``spec_typ`` 保留典型值语义。

3. **``test_cases.created_by``** —— 记录用例来源 (人工 / ATERag bundle /
   Agent), 供追溯与冲突处理: ATERag 权威字段覆盖时需先告知谁改过。

跨方言注意: 本迁移只用 SQLAlchemy Core 构造, 不含 ON CONFLICT / JSONB 等
PostgreSQL 专有语法 —— 开发与 CI 跑 SQLite, 生产跑 PostgreSQL。
``sa.JSON`` 在两侧分别映射为 TEXT(JSON1) 与 JSONB, 行为一致。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — test_conditions table + one-sided limits + created_by."""
    # ---- 1. test_conditions ----
    op.create_table(
        "test_conditions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        # owner 用 (type, id) 而非单一 FK: 条件既可挂需求也可挂用例,
        # 单一外键会让"用例继承需求条件"这条模型无处落地。
        sa.Column("owner_type", sa.String(length=16), nullable=False, index=True),
        sa.Column("owner_id", sa.String(length=36), nullable=False, index=True),
        sa.Column("side", sa.String(length=8), nullable=False),  # input | output
        sa.Column("kind", sa.String(length=64), nullable=False, index=True),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("value", sa.JSON, nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, default="draft", index=True),
        sa.Column("method_ref", sa.String(length=64), nullable=True),
        sa.Column("cond_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        # 同一 owner 下指纹唯一 —— 幂等重放靠它判定"这条是否已存在",
        # 重复插入会让条件翻倍而下游毫无察觉。
        sa.UniqueConstraint("owner_type", "owner_id", "cond_fingerprint", name="uq_test_conditions_owner_fp"),
        # 两侧皆空即无意义(既无下限也无上限), 用 CHECK 在库层挡住。
        sa.CheckConstraint("side IN ('input', 'output')", name="ck_test_conditions_side"),
    )
    op.create_index("ix_test_conditions_owner", "test_conditions", ["owner_type", "owner_id"])

    # ---- 2. test_limits 单边化 + spec_typ ----
    # batch_alter_table 在 SQLite 上走表重建, 跨方言安全。
    with op.batch_alter_table("test_limits") as batch:
        batch.alter_column("spec_low", existing_type=sa.Float(), nullable=True)
        batch.alter_column("spec_high", existing_type=sa.Float(), nullable=True)
        batch.add_column(sa.Column("spec_typ", sa.Float(), nullable=True))

    # ---- 2b. test_requirements.status + req_fingerprint ----
    # status: a requirement that vanishes from a newer spec revision is marked
    # ``stale``, never deleted — deleting it would break the traceability chain
    # back to test results already produced against it.
    # req_fingerprint: lets an import distinguish "unchanged" (no writes) from
    # "changed" (conditions rewritten, cases reset to draft).
    with op.batch_alter_table("test_requirements") as batch:
        batch.add_column(
            sa.Column("status", sa.String(length=20), nullable=False, server_default="active", index=True)
        )
        batch.add_column(sa.Column("req_fingerprint", sa.String(length=64), nullable=True, index=True))

    # ---- 3. test_cases.created_by + cond_fingerprint ----
    # cond_fingerprint stores the *scenario* criterion fingerprint, so an import
    # can tell "this case's criteria actually changed" (reset to draft) from
    # "something else in the requirement changed" (leave the status alone).
    with op.batch_alter_table("test_cases") as batch:
        batch.add_column(sa.Column("created_by", sa.String(length=120), nullable=True, index=True))
        batch.add_column(sa.Column("cond_fingerprint", sa.String(length=64), nullable=True, index=True))


def downgrade() -> None:
    """Downgrade schema.

    索引必须先于列显式删除, 且只删 upgrade 里真正建过的那些:
    SQLite 上 batch_alter_table 通过重建表实现改表, 重建时若索引定义仍引用
    待删列就会报 "no such column", 整个回滚失败 —— 迁移不可回退等于这条路
    走死。索引名取自实际建表结果, 不靠推断(推断会漏, 漏了就是回滚失败)。

    spec_low/spec_high 保持可空, 不改回 NOT NULL: 若已导入单边限值(如只有
    上限), 回填缺失一侧会凭空造出判据。宁可留下更宽松的列, 也不静默编造
    数值 —— 这与导入侧"不补限值"的红线一致。
    """
    for idx, table in (
        ("ix_test_requirements_req_fingerprint", "test_requirements"),
        ("ix_test_requirements_status", "test_requirements"),
        ("ix_test_cases_cond_fingerprint", "test_cases"),
        ("ix_test_cases_created_by", "test_cases"),
        ("ix_test_conditions_status", "test_conditions"),
        ("ix_test_conditions_kind", "test_conditions"),
        ("ix_test_conditions_owner_id", "test_conditions"),
        ("ix_test_conditions_owner_type", "test_conditions"),
        ("ix_test_conditions_owner", "test_conditions"),
    ):
        op.drop_index(idx, table_name=table)

    with op.batch_alter_table("test_requirements") as batch:
        batch.drop_column("req_fingerprint")
        batch.drop_column("status")

    with op.batch_alter_table("test_cases") as batch:
        batch.drop_column("cond_fingerprint")
        batch.drop_column("created_by")

    with op.batch_alter_table("test_limits") as batch:
        batch.drop_column("spec_typ")
        batch.alter_column("spec_low", existing_type=sa.Float(), nullable=True)
        batch.alter_column("spec_high", existing_type=sa.Float(), nullable=True)

    op.drop_table("test_conditions")
