"""add_requirement_extraction_signals

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-30 12:40:00.000000

评审界面看不出"该建注记了"
----------------------------
``b2c3d4e5f6a7`` 把 ``section_path`` 与 ``notes`` 补进了 ``test_requirements``,
但 bundle 里另有两个字段被静默丢弃:

  ``flags``       抽取时打在该需求上的标记, 例如 ``annotation_draft``
                  (这条需求的条件来自人工注记而非规则)
  ``assessment``  充分性评估, 例如 ``needs_review`` / ``rule_only``

丢掉它们的直接后果不是"少显示一列", 而是**评审的人看不到该做什么**。
评审界面的职责是回答"这条判据能不能用", 而这条需求的条件究竟来自规则
还是来自人写的注记, 正是这个问题的一部分。一条 ``annotation_draft`` 的
需求, 它的条件在签字前不能作为产测判据 —— 界面必须能一眼看出这一点,
否则工程师只会看到一堆看起来正常的条件并签掉。

这也是"注记起草目前只在命令行"这件事为什么在界面上是死路: 界面能签字,
却看不到"这条该建注记"的信号, 于是没有任何东西把人导向注记流程。

存储形式
--------
两列都存成 ``Text``, 内容为 JSON 字符串, 而不是建 JSONB 列:

* 两者都是稀疏的 —— 绝大多数需求的 ``flags`` 是空列表, ``assessment``
  只有两三个键, 建 JSONB 列带来的查询收益在这个数据量级上为零;
* SQLite 与 PostgreSQL 都用同一套 Text + ``json.dumps``, 避免为了两个稀疏
  字段引入方言分支 —— 本项目在两种库上都要跑测试。

读取侧 ``TestRequirementOut`` 负责反序列化, 写侧导入器负责序列化。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the two extraction-signal columns.

    Both nullable with no default: an empty string would be indistinguishable
    from "imported and genuinely empty", and the difference matters when
    reading a requirement that predates this migration.
    """
    with op.batch_alter_table("test_requirements") as batch:
        batch.add_column(sa.Column("flags", sa.Text, nullable=True))
        batch.add_column(sa.Column("assessment", sa.Text, nullable=True))


def downgrade() -> None:
    """Drop both columns.

    No index to remove: neither column is used as a lookup key. They are read
    alongside a requirement row that has already been selected.
    """
    with op.batch_alter_table("test_requirements") as batch:
        batch.drop_column("assessment")
        batch.drop_column("flags")
