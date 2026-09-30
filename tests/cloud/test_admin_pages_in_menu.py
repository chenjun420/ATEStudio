"""用户管理 / 角色与权限 必须挂在「系统管理」菜单下, 不在右上角下拉里。

背景
----
这两个页面原先只能从右上角账号下拉进入, 而且用 ``v-if="isAdmin"`` 控制显示。
两个问题:

1. 靠"记得下拉里有东西"才能找到的页面, 等于找不到。
2. ``isAdmin`` 是从前端 token 推导的, 服务端从不校验它 —— 那是**建议性**的显示
   控制, 不是访问控制。菜单项的 ``required_permissions`` 才由服务端求值。

所以它们现在是 seed 里的菜单项, ``required_permissions: ["admin:read"]``。

顺带暴露的一件事
----------------
``admin:read`` 此前**没有任何角色拥有**, 与 ``aterag:import`` 那次完全同形:
端点/数据要求一个权限, 而没有任何主体被授予它。若不补, 这两条菜单对 admin 也是
不可见的 —— 也就是把"下拉里找不到"换成了"菜单里找不到"。本文件因此同时钉住
"菜单要求的权限可被满足"。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ate_cloud.api.v1.apps import default_apps
from ate_cloud.auth.rbac import ROLE_SCOPES, is_superuser

REPO = Path(__file__).resolve().parents[2]
LAYOUT = REPO / "frontend" / "src" / "layouts" / "AppLayout.vue"
ROUTER = REPO / "frontend" / "src" / "router" / "index.ts"

ADMIN_ONLY = ("users", "roles")


def _system_app() -> dict:
    app = next(a for a in default_apps if a["code"] == "system")
    return app


def _menu(code: str) -> dict:
    return next(m for m in _system_app()["menus"] if m["code"] == code)


class TestMenusAreSeeded:
    @pytest.mark.parametrize("code", ADMIN_ONLY)
    def test_menu_exists_under_system(self, code: str) -> None:
        codes = {m["code"] for m in _system_app()["menus"]}
        assert code in codes, f"{code} 不在系统管理的菜单里, 现有: {sorted(codes)}"

    @pytest.mark.parametrize("code", ADMIN_ONLY)
    def test_route_matches_the_frontend_route(self, code: str) -> None:
        """The menu and the router must name the same path.

        Same failure mode as the seeded-route check: a mismatch shows up as a
        menu that navigates nowhere, with every API check still green.
        """
        route_src = ROUTER.read_text(encoding="utf-8")
        tail = _menu(code)["route_path"].split("/")[-1]
        assert f"path: '{tail}'" in route_src or f'path: "{tail}"' in route_src, (
            f"菜单指向 {_menu(code)['route_path']}, 但路由表里没有子路径 '{tail}'"
        )

    @pytest.mark.parametrize("code", ADMIN_ONLY)
    def test_requires_admin_read(self, code: str) -> None:
        assert _menu(code)["required_permissions"] == ["admin:read"]

    def test_admin_can_see_them(self) -> None:
        """The point of the exercise: admin must actually see both entries."""
        admin = set(ROLE_SCOPES["admin"])
        assert is_superuser(admin), "admin 没有通配, 菜单会走 required_permissions 过滤"
        for code in ADMIN_ONLY:
            assert _menu(code)["required_permissions"], code


class TestDropdownNoLongerCarriesThem:
    """The dropdown must not keep a second, differently-gated entry point."""

    @pytest.fixture(scope="class")
    @classmethod
    def layout(cls) -> str:
        """The layout source with comments removed.

        Comments have to go: the code that made this change explains *why* in
        prose, and that prose necessarily names ``command="users"`` and
        ``isAdmin`` — the very strings being asserted absent. Matching raw text
        therefore fails on the explanation of the fix, which is both silly and
        fragile in the other direction (deleting the comment would "fix" it).
        """
        assert LAYOUT.is_file(), f"找不到 {LAYOUT}"
        src = LAYOUT.read_text(encoding="utf-8")
        src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
        src = re.sub(r"^\s*(?://|\*|/\*).*$", "", src, flags=re.M)
        return src

    @pytest.mark.parametrize("code", ADMIN_ONLY)
    def test_no_dropdown_item(self, layout: str, code: str) -> None:
        assert f'command="{code}"' not in layout, (
            f"{code} 仍在右上角下拉里 —— 用户要的是它出现在系统管理菜单下, "
            f"而不是两个地方各有一个入口"
        )

    @pytest.mark.parametrize("code", ADMIN_ONLY)
    def test_no_dead_command_case(self, layout: str, code: str) -> None:
        """A `case 'users':` with no matching item is dead code.

        Kept deliberately absent so a future `command="users"` fails at review
        time instead of silently doing nothing.
        """
        assert not re.search(rf"case\s+'{code}'\s*:", layout), (
            f"handleCommand 里还留着 '{code}' 分支, 但下拉已无对应项"
        )

    def test_dropdown_keeps_the_personal_actions(self, layout: str) -> None:
        """Relocating admin pages must not take the account actions with them."""
        for command in ("settings", "password", "logout"):
            assert f'command="{command}"' in layout, f"下拉里的 {command} 不见了"

    def test_admin_flag_is_no_longer_bound(self, layout: str) -> None:
        """``isAdmin`` gated those items on a client-side flag.

        Leaving it destructured invites the next control to be gated the same
        way — advisory visibility that the server never checks.
        """
        assert "isAdmin" not in layout, "isAdmin 仍被解构, 但已无使用点"

    def test_stripping_comments_is_actually_working(self, layout: str) -> None:
        """Guards the guard.

        If the comment stripper silently stopped matching, every assertion above
        would go back to failing on prose — or worse, start passing for the
        wrong reason after a refactor moves the strings around.
        """
        raw = LAYOUT.read_text(encoding="utf-8")
        assert "<!--" in raw, "源文件里没有 HTML 注释, 剥离逻辑可能已失效"
        assert "<!--" not in layout
