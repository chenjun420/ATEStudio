"""add_requirement_spec_provenance

Revision ID: b2c3d4e5f6a7
Revises: b7c8d9e0f1a2
Create Date: 2026-09-30 11:30:00.000000

ATERag bundle 导入时暴露的缺口 (P2 补漏): ``test_requirements`` 缺
``section_path`` 与 ``notes`` 两列, 而 bundle 的每条需求都带着它们。

为什么必须补, 而不是"反正没人用就丢了"
----------------------------------------
``section_path`` 存的是规格书条款号 (如 ``4.3.1``)。它是"这条测试来自
规格书哪一段"这个问题的唯一答案 —— 规格驱动测试的全部价值都建立在
"能从用例反查到规格书原文"这条链上。丢掉它, 一次规格书改版的
影响面分析就只能靠人肉通读全文, 而这正是引入 ATERag 想消灭的工作。

``notes`` 存的是规格书原文备注 (如"铭牌标称电压, 在此电压范围内进行
安规认证")。这类句子常常是判定依据的真正出处: 数字可能在表格里,
但"这个数字是什么意思"只在备注里。

为什么另起一个迁移, 而不是改 b7c8d9e0f1a2
-----------------------------------------
b7c8d9e0f1a2 已经推到远端 dev 分支。改写已推送的迁移会迫使所有人做
force pull, 并且让任何已经按该迁移建过库的环境处于"版本号与内容
不符"的状态 —— 比多一个迁移糟糕得多。多一个 revision 只是历史稍长,
代价为零。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — add spec provenance columns to test_requirements.

    Both are NULLABLE and indexed: ``section_path`` is the lookup key for
    "everything this spec clause requires" (frequent query, hence the index),
    ``notes`` is only ever read alongside it.
    """
    with op.batch_alter_table("test_requirements") as batch:
        batch.add_column(
            sa.Column("section_path", sa.String(length=64), nullable=True, index=True)
        )
        batch.add_column(sa.Column("notes", sa.Text, nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the index explicitly first — on SQLite ``batch_alter_table``
    rebuilds the table, and a surviving index definition that references a
    dropped column aborts the whole rollback with "no such column". A
    migration that cannot be reversed has sealed off the path.
    """
    op.drop_index("ix_test_requirements_section_path", table_name="test_requirements")

    with op.batch_alter_table("test_requirements") as batch:
        batch.drop_column("notes")
        batch.drop_column("section_path")
