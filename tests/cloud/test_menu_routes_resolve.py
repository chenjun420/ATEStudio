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


def _walk(menus: list[dict[str, object]]) -> list[dict[str, object]]:
    """Every seed row, groups and pages alike, in tree order."""
    out: list[dict[str, object]] = []
    for menu in menus:
        out.append(menu)
        children = menu.get("children")
        if children:
            out.extend(_walk(children))  # type: ignore[arg-type]
    return out


def _seeded_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for app in default_apps:
        rows.extend(_walk(app["menus"]))
    return rows


def _seeded_route_paths() -> set[str]:
    return {str(m["route_path"]) for m in _seeded_rows() if m.get("route_path")}


def _router_paths() -> set[str]:
    """Declared paths, minus the 404 catch-all.

    The catch-all is ``/:pathMatch(.*)*``. Its ``:`` prefix makes it a wildcard
    for the segment matcher below, so leaving it in made **every** single-segment
    tail "resolve" -- including ones that do not exist:

        /ops  +  'faultcase'  ->  'faultcase' matches ':pathMatch(.*)*'  ->  True

    So a menu repointed from ``/ops/fault-cases`` to ``/ops/faultcase`` passed
    this file's central check. Found by mutation: the check that exists to
    prevent "the menu points at nothing" was passing a menu that pointed at
    nothing. The router's own version is declared by the trailing ``*`` on the
    repeat, so dropping literals containing ``*`` removes it without
    special-casing the route name.
    """
    if not ROUTER.is_file():
        pytest.skip(f"找不到路由源文件: {ROUTER}")
    return {p for p in _PATH.findall(ROUTER.read_text(encoding="utf-8")) if "*" not in p}


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

    def test_every_seed_row_has_a_code(self) -> None:
        """Codes are the idempotency key for seeding and the i18n key for labels.

        A row without one cannot be re-seeded and cannot be translated, so it
        would silently fall back to its Chinese database name in every locale.
        """
        missing = [
            f"{app['code']}/{m.get('name', '?')}"
            for app in default_apps
            for m in _walk(app["menus"])
            if not m.get("code")
        ]
        assert not missing, f"种子行缺少 code: {missing}"

    def test_groups_are_exactly_the_rows_without_a_route(self) -> None:
        """The group/page split is defined by ``route_path``, so it needs a guard.

        Making ``route_path`` nullable (alembic f2b3c4d5e6a7) opened a way for the
        distinction to rot: a group that grew a route becomes a menu entry that
        navigates to a page that does not exist, and a page that lost its route
        becomes a header above nothing. Both are silent.

        So: a group has children and no route; a page has a route. Nesting deeper
        than group → page is what the spec's "depth 3" forbids, and the sidebar
        renders exactly two levels.
        """
        offenders: list[str] = []
        for app in default_apps:
            for row in _walk(app["menus"]):
                has_children = bool(row.get("children"))
                has_route = bool(row.get("route_path"))
                if has_children and not has_route:
                    continue  # a group, as intended
                if has_route and not has_children:
                    continue  # a page, as intended
                offenders.append(
                    f"{app['code']}/{row.get('code')}: "
                    f"route={row.get('route_path')!r} children={len(row.get('children') or [])}"
                )
        assert not offenders, (
            "种子行的分组/页面结构不对 —— 分组必须有子项且无路由, 页面必须有路由且无子项:\n"
            + "\n".join(f"  {o}" for o in offenders)
        )

    def test_no_seeded_route_is_a_duplicate(self) -> None:
        """Two menus on the same path is a routing bug waiting to happen."""
        seen = [str(m["route_path"]) for m in _seeded_rows() if m.get("route_path")]
        dupes = {p for p in seen if seen.count(p) > 1}
        assert not dupes, f"重复的 route_path: {sorted(dupes)}"

    def test_every_seeded_route_resolves(self) -> None:
        """One summary assertion, so the failure names every bad path at once.

        Parametrising per route reports 19 separate failures for what may be one
        missing entry, which buries the single fact worth acting on.
        """
        unresolved = [r for r in sorted(_seeded_route_paths()) if not _resolves(r)]
        assert not unresolved, f"菜单指向这些路径, 但 Vue 路由里拼不出来: {unresolved}"

    def test_a_route_that_does_not_exist_does_not_resolve(self) -> None:
        """The negative case, which the summary test above cannot express.

        Every other check here asks "does a real path resolve?". Nothing asked
        the opposite, so the resolver could grow a false positive and stay green
        — which is exactly what happened: the 404 catch-all
        ``/:pathMatch(.*)*`` counted as declaring any single-segment tail, so
        ``/ops/faultcase`` resolved and a menu repointed from
        ``/ops/fault-cases`` passed unnoticed.

        Each path below is one segment off a parent that *does* exist, which is
        the shape that matched the catch-all.
        """
        for bogus in (
            "/ops/faultcase",       # real: /ops/fault-cases
            "/ops/station",         # real: /ops/stations
            "/dev/traceabilty",     # real: /dev/traceability
            "/ops/deeply/nested/menu",
        ):
            assert not _resolves(bogus), (
                f"{bogus} 在路由表里不存在, 但解析器说它能解析 —— "
                "解析器对不存在的路径返回了 True"
            )

    def test_the_catch_all_route_is_excluded_from_the_literal_set(self) -> None:
        """Guards the guard.

        If the router ever stops using ``*`` for the 404 catch-all, this filter
        silently stops excluding it and the negative test above starts failing
        for the wrong reason. Asserting the exclusion is what makes the failure
        legible.
        """
        literals = _router_paths()
        assert "/:pathMatch(.*)*" not in literals, (
            "404 兜底路由又回到字面量集合里了 —— 任何单段子路径都会被它匹配上"
        )
        assert _PATH.findall(ROUTER.read_text(encoding="utf-8")), "路由表里一个字面量都没有了"

    def test_the_resolver_actually_resolves_the_common_shape(self) -> None:
        """Pin the fix for the spurious 19.

        If the join logic regressed into "matches nothing", the summary test
        above would go red for the wrong reason and look like a product break.

        The router declares parents absolutely (``/ops``) and children relatively
        (``stations``), so no literal ever reads ``/ops/stations`` and a naive
        comparison matches nothing.
        """
        literals = _router_paths()
        assert "/ops" in literals and "stations" in literals, (
            "路由表的形状变了(父路由不再绝对、子路由不再相对), "
            "本文件的拼接解析需要跟着改"
        )
        assert "/ops/stations" not in literals, (
            "出现了拼接形式的字面量 —— 解析逻辑的前提变了"
        )
        assert _resolves("/ops/stations")
        assert _resolves("/dev/sequences")
