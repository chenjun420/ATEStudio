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


class TestDropdownIsNavigationNotASecondGate:
    """The dropdown may point at these pages, but it must not decide access.

    What changed, and why it is not a regression
    ---------------------------------------------
    The spec's top bar has exactly two items (产测开发 / 运行监控), so system
    administration is reached from the account dropdown (§4.4). That put these
    two pages back in the dropdown as navigation shortcuts.

    What did **not** change is where access is decided, which is the whole point
    of this file:

    * they remain seeded menu rows under ``system`` with
      ``required_permissions: ["admin:read"]``, which the server evaluates;
    * their routes carry ``meta.requiresAdmin``;
    * the endpoints behind them are scope-checked.

    So the dropdown item is a shortcut to a page the server already protects.
    Before, the dropdown item *was* the entry point and its visibility came from
    a token-derived flag the server never read — which is what made it advisory.

    The distinction this class pins: a dropdown command must navigate to the same
    path the menu uses. If it ever grows its own gate, or its own fetches, or
    diverges from the menu's route, the two gates are back.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def layout(cls) -> str:
        """The layout source with comments removed.

        Comments have to go: the code that made this change explains *why* in
        prose, and that prose necessarily names ``command="users"`` and
        ``isAdmin`` — the very strings being asserted on. Matching raw text
        therefore fails on the explanation of the change.
        """
        assert LAYOUT.is_file(), f"找不到 {LAYOUT}"
        src = LAYOUT.read_text(encoding="utf-8")
        src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
        src = re.sub(r"^\s*(?://|\*|/\*).*$", "", src, flags=re.M)
        return src

    @pytest.mark.parametrize("code", ADMIN_ONLY)
    def test_dropdown_command_navigates_to_the_menu_route(
        self, layout: str, code: str
    ) -> None:
        """A shortcut, not a second implementation.

        The command has to push the *same* path the seeded menu row carries. If
        the two ever differ, one of them is showing a page the other does not
        offer — and whichever is wrong, the user cannot tell which.
        """
        assert f'command="{code}"' in layout, f"下拉里的 {code} 入口不见了"
        branch = re.search(rf"case\s+'{code}'\s*:(.*?)break;?", layout, re.S)
        assert branch, f"handleCommand 里没有 '{code}' 分支 —— 下拉项会静默无反应"
        expected = _menu(code)["route_path"]
        assert expected in branch.group(1), (
            f"下拉的 {code} 跳向的路径与菜单不一致: 菜单是 {expected}"
        )

    @pytest.mark.parametrize("code", ADMIN_ONLY)
    def test_dropdown_does_not_fetch_or_decide(self, layout: str, code: str) -> None:
        """No data call, no permission check — just a route.

        Anything more in this branch is a second gate wearing a shortcut's
        clothes: it would have its own notion of who may see the page, and the two
        notions would eventually disagree.
        """
        branch = re.search(rf"case\s+'{code}'\s*:(.*?)break;?", layout, re.S)
        assert branch, f"没有 '{code}' 分支"
        body = branch.group(1)
        for forbidden in ("http", "fetch(", "api.", "hasScope", "permissions"):
            assert forbidden not in body, (
                f"下拉的 {code} 分支里出现了 {forbidden!r} —— "
                "下拉应当只负责跳转, 访问权由菜单与服务端判定"
            )

    def test_dropdown_keeps_the_personal_actions(self, layout: str) -> None:
        """Restructuring the dropdown must not take the account actions with it."""
        for command in ("settings", "password", "logout"):
            assert f'command="{command}"' in layout, f"下拉里的 {command} 不见了"

    def test_the_personal_group_is_not_gated_by_is_admin(self, layout: str) -> None:
        """``isAdmin`` may hide a shortcut, but not a personal action.

        修改密码 / 语言 / 主题 belong to whoever is signed in. Gating them on an
        admin flag would lock a normal user out of changing their own password —
        and it is the kind of copy-paste that brought the admin items here in the
        first place.
        """
        for command in ("password", "logout"):
            pattern = re.compile(
                rf'v-if="[^"]*isAdmin[^"]*"\s*>\s*<el-dropdown-item\s+command="{command}"'
            )
            assert not pattern.search(layout), f"{command} 被 isAdmin 挡住了"

        """Guards the guard.

        If the comment stripper silently stopped matching, every assertion above
        would go back to failing on prose — or worse, start passing for the
        wrong reason after a refactor moves the strings around.
        """
        raw = LAYOUT.read_text(encoding="utf-8")
        assert "<!--" in raw, "源文件里没有 HTML 注释, 剥离逻辑可能已失效"
        assert "<!--" not in layout
