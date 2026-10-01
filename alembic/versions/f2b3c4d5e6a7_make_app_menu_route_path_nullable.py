"""make_app_menu_route_path_nullable

Revision ID: f2b3c4d5e6a7
Revises: e5f6a7b8c9d0
Create Date: 2026-10-01 10:00:00.000000

分组菜单没有路由
----------------

两级 IA(``产测开发`` / ``运行监控``)要求侧栏按「产品与型号 / 需求与用例 /
流程与脚本 / 工位绑定」分组。分组是容器, 不是页面:点它应该展开, 不该跳转。
而 ``app_menus.route_path`` 是 ``NOT NULL``, 于是容器被迫给自己编一个路径 ——
而任何编出来的路径都是一个不存在的页面, 也就是菜单里一个点得进去的白屏。

``route_name`` 本来就是可空的, 因为它同样只是页面的附属信息。这里让
``route_path`` 与它对齐。

放宽而不是收紧
--------------

前端已经按 ``if (m.route_path)`` 过滤菜单项, 后端的 ``test_menu_routes_resolve``
也只校验有 route_path 的种子项。所以可空之后, 唯一需要守住的不变式是:

**有 route_path 的菜单必须能解析出真实路由, 没有的必须是分组。**

这条断言加在 ``tests/cloud/test_menu_routes_resolve.py``, 否则可空会退化成
"随便留空就不用校验"。

downgrade 会把空 route_path 的行删掉而不是填路径 —— 删掉会掉菜单, 填路径会
造出指向不存在页面的菜单, 两者都比"这个迁移回滚不了"更坏。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f2b3c4d5e6a7"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Relax app_menus.route_path so a group row can exist without a route."""
    with op.batch_alter_table("app_menus") as batch:
        batch.alter_column(
            "route_path",
            existing_type=sa.String(length=256),
            nullable=True,
        )


def downgrade() -> None:
    """Remove routeless (group) rows, then restore NOT NULL.

    Deleting rather than backfilling: an invented path is a menu entry that
    navigates to a page that does not exist, which is the exact failure this
    migration exists to prevent.
    """
    op.execute("DELETE FROM app_menus WHERE route_path IS NULL OR route_path = ''")
    with op.batch_alter_table("app_menus") as batch:
        batch.alter_column(
            "route_path",
            existing_type=sa.String(length=256),
            nullable=False,
        )
