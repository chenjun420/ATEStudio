"""种子菜单的 route_path 必须在 Vue 路由里真实存在。

为什么要查这个
------------
菜单能显示出来(上一次修复解决的)不等于点得进去。``app_menus.route_path`` 是
数据, Vue 路由是代码, 两者没有任何机制保持一致 —— 种子里写 ``/flow/scripts``
而路由表里没有这条, 表现就是"菜单在那里, 点进去一片空白", 而所有 API 检查
都是绿的。

和前两个缺陷同一类: 跨边界的约定没有断言。``contract_hash`` 钉住了 ATERag 与
ATEStudio 之间的 bundle 契约, 但没有任何东西钉住"数据库里的菜单路径"与"前端
路由表"。

做法与它的边界
--------------
不导入前端代码(那是 Node 工具链, 不在 pytest 里)。改为解析
``frontend/src/router/index.ts`` 的源码, 抽出全部 ``path:`` 字面量, 再与种子
数据比对。

只覆盖**显式字面量**。动态拼接出来的路径无法从源码静态抽出, 因此本测试不能证明
"所有菜单都能点开" —— 它证明的是"每个种子路径都能由已声明的字面量拼出来"。
这条边界写在这里, 而不是假装它覆盖了全部。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ate_cloud.api.v1.apps import default_apps

REPO = Path(__file__).resolve().parents[2]
ROUTER = REPO / "frontend" / "src" / "router" / "index.ts"

_PATH = re.compile(r"""path:\s*['"]([^'"]+)['"]""")


def _seeded_route_paths() -> set[str]:
    out: set[str] = set()
    for app in default_apps:
        for menu in app["menus"]:
            if menu.get("route_path"):
                out.add(menu["route_path"])
    return out


def _router_paths() -> set[str]:
    if not ROUTER.is_file():
        pytest.skip(f"找不到路由源文件: {ROUTER}")
    return set(_PATH.findall(ROUTER.read_text(encoding="utf-8")))


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _declared(path: str, literals: set[str]) -> bool:
    """Whether ``path`` is declared, treating a ``:param`` segment as a wildcard.

    A dynamic parent genuinely routes a concrete child, so ``/operator/:station_id``
    counts as declaring ``/operator/default``.
    """
    if path in literals:
        return True
    want = _segments(path)
    for lit in literals:
        have = _segments(lit)
        if len(want) != len(have):
            continue
        if all(
            a == b or a.startswith(":") or b.startswith(":")
            for a, b in zip(want, have, strict=True)
        ):
            return True
    return False


def _resolves(route: str) -> bool:
    """Whether ``route`` is reachable from the router's nested declarations.

    The router declares parents absolutely and children **relatively** — ``/node``
    with a child ``stations`` — so the literals in the file are ``/node`` and
    ``stations``, never ``/node/stations``. Collecting the literals and comparing
    them to the seeded paths therefore matches nothing at all, which is exactly
    what the first version of this test did: 19 failures, all spurious.

    So the segments are joined instead: for every split point, check that the
    parent prefix and the remaining tail are both declared.
    """
    literals = _router_paths()
    segments = _segments(route)
    for cut in range(len(segments)):
        parent = "/" + "/".join(segments[:cut])
        tail = "/".join(segments[cut:])
        if _declared(parent, literals) and _declared(tail, literals):
            return True
    return False


class TestSeededRoutesResolve:
    def test_router_file_is_where_we_think(self) -> None:
        """Guards the parser: a moved file would make every other check vacuous."""
        assert ROUTER.is_file(), f"路由源文件不在预期位置: {ROUTER}"
        assert _router_paths(), "从路由源文件里没解析出任何 path —— 解析方式变了?"

    def test_the_seed_declares_routes(self) -> None:
        assert len(_seeded_route_paths()) >= 15, "种子菜单的 route_path 数量异常"

    def test_no_seeded_route_is_a_duplicate(self) -> None:
        """Two menus on the same path is a routing bug waiting to happen."""
        seen = [
            m["route_path"]
            for app in default_apps
            for m in app["menus"]
            if m.get("route_path")
        ]
        dupes = {p for p in seen if seen.count(p) > 1}
        assert not dupes, f"重复的 route_path: {sorted(dupes)}"

    def test_every_seeded_route_resolves(self) -> None:
        """One summary assertion, so the failure names every bad path at once.

        Parametrising per route reports 19 separate failures for what may be one
        missing entry, which buries the single fact worth acting on.
        """
        unresolved = [r for r in sorted(_seeded_route_paths()) if not _resolves(r)]
        assert not unresolved, f"菜单指向这些路径, 但 Vue 路由里拼不出来: {unresolved}"

    def test_the_resolver_actually_resolves_the_common_shape(self) -> None:
        """Pin the fix for the spurious 19.

        If the join logic regressed into "matches nothing", the summary test
        above would go red for the wrong reason and look like a product break.
        """
        literals = _router_paths()
        assert "/node" in literals and "stations" in literals, (
            "路由表的形状变了(父路由不再绝对、子路由不再相对), "
            "本文件的拼接解析需要跟着改"
        )
        assert _resolves("/node/stations")
        assert _resolves("/node/stations/:id")
