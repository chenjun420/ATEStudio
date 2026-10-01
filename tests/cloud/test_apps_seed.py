"""操作员面板不再作为菜单项, 以及分组菜单的种子结构。

这个文件以前叫 ``test_apps_seed.py``, 内容只有一个: 断言「操作员面板」被种进
``exec-monitor`` 的菜单里。现在那个断言反过来了 —— 见下面为什么。

为什么移除这条菜单项
--------------------
它指向 ``/operator/default``。菜单项需要一条具体路径, 而 ``OperatorView`` 的
路径是 ``/operator/:station_id``, 没有具体值。于是种子里出现了一个叫 ``default``
的工位。

结果是: 菜单对**任何**工位都声称存在一个操作员面板, 点进去打开的却是一个叫
``default``、并不存在的工位。这正是本项目反复拒绝的那类东西 —— 界面呈现了一个
不成立的能力。

操作员面板是**某一个工位的屏幕**, 给站在工位上的人看。所以它留在
``/operator/:station_id``, 由「工位列表」那一行按工位 id 链过去 —— 那是唯一真正
知道工位 id 的地方。要把它放回菜单, 前面得先有一个工位选择器。

分组菜单的结构
--------------
``route_path`` 在 f2b3c4d5e6a7 里被放宽为可空, 好让分组不被迫编一个路径。代价是
「分组」和「页面」的区别从类型上消失了, 只剩约定, 所以这里把它钉住。
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ate_cloud.api.v1.apps import default_apps
from ate_cloud.main import app
from ate_cloud.models import Base


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    from ate_cloud.db import get_db

    async def _get_db() -> Any:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    # The seed endpoint is open (no auth) by design; other routes would need a
    # token, which is not what this file is about.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


def _walk(menus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for menu in menus:
        out.append(menu)
        out.extend(_walk(menu.get("children") or []))
    return out


def _all_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for app_def in default_apps:
        rows.extend(_walk(app_def["menus"]))
    return rows


class TestNoFabricatedStationRoute:
    """No menu entry may name a station that does not exist."""

    def test_no_menu_points_at_a_placeholder_station(self) -> None:
        offenders = [
            str(m["route_path"])
            for m in _all_rows()
            if m.get("route_path") and str(m["route_path"]).startswith("/operator/")
        ]
        assert not offenders, (
            "菜单里又出现了指向具体工位 id 的操作员面板项 —— 那必须是一个真实工位, "
            f"否则界面声称的页面并不存在: {offenders}"
        )

    def test_the_operator_route_still_exists_without_being_a_menu(self) -> None:
        """Removing the menu entry must not remove the capability.

        The route stays: the station screen links to it per row. Only the
        fabricated ``default`` path is gone.
        """
        from pathlib import Path

        router = Path(__file__).resolve().parents[2] / "frontend" / "src" / "router" / "index.ts"
        source = router.read_text(encoding="utf-8")
        assert "path: '/operator/:station_id'" in source, "操作员面板的路由不见了"
        assert "'/operator/default'" not in source, (
            "/operator/default 还在 —— 那是一个不存在的工位 id"
        )


class TestTwoModeNavigation:
    def test_top_level_is_exactly_two_modes_plus_system(self) -> None:
        codes = [a["code"] for a in default_apps]
        assert codes == ["test-dev", "runtime", "system"], (
            f"顶层应只有 产测开发 / 运行监控 两个模式, 外加从下拉进入的 system: {codes}"
        )

    def test_system_is_not_a_tab_but_is_still_seeded(self) -> None:
        """`system` must exist without being a top-bar tab.

        The spec's top bar has two items, so system administration is reached
        from the account dropdown. But its pages stay menu rows, because
        test_admin_pages_in_menu.py is right about why: a page reachable only by
        remembering a dropdown is a page nobody finds, and a client-side
        ``isAdmin`` flag is advisory where ``required_permissions`` is enforced.
        """
        system = next(a for a in default_apps if a["code"] == "system")
        codes = {m["code"] for m in system["menus"]}
        assert {"settings", "users", "roles"} <= codes


class TestGroupedMenuStructure:
    def test_groups_have_children_and_no_route(self) -> None:
        for app_def in default_apps:
            for group in app_def["menus"]:
                children = group.get("children") or []
                if not children:
                    continue  # a page sitting at the top level (the system app)
                assert not group.get("route_path"), (
                    f"{app_def['code']}/{group['code']} 是分组, 却带了 route_path="
                    f"{group['route_path']!r} —— 一个编出来的路径就是一个点得进去的白屏"
                )

    def test_pages_are_at_most_two_levels_below_the_app(self) -> None:
        """Depth 3 is the spec's cap: app → group → page.

        Rendered as two sidebar levels. A third level would mean the sidebar has
        to scroll three deep to reach a page, and every menu in the seed is
        currently exactly two.
        """
        for app_def in default_apps:
            for group in app_def["menus"]:
                for page in group.get("children") or []:
                    assert not page.get("children"), (
                        f"{app_def['code']}/{group['code']}/{page['code']} 之下还有子项 —— "
                        "侧栏深度超过两层"
                    )

    def test_every_group_carries_a_label_code(self) -> None:
        """A group has no page to fall back on, so its code must be translatable.

        Groups resolve through the same i18n table as pages. The frontend mirror
        in menuLabels.test.ts asserts the same list, so a group added to the seed
        without a key there renders its Chinese database name in every locale.
        """
        from pathlib import Path

        labels = (
            Path(__file__).resolve().parents[2]
            / "frontend"
            / "src"
            / "composables"
            / "menuLabels.ts"
        ).read_text(encoding="utf-8")

        def mapped(code: str) -> bool:
            # Both spellings the table uses: bare identifiers (`debug: '...'`)
            # and quoted ones (`station-binding: '...'`).
            return f"'{code}':" in labels or f"\n  {code}:" in labels

        missing = [
            str(group["code"])
            for app_def in default_apps
            for group in app_def["menus"]
            if group.get("children") and not mapped(str(group["code"]))
        ]
        assert not missing, f"分组缺少 i18n 映射: {missing}"


class TestSeedIdempotency:
    async def test_rerunning_the_seed_does_not_duplicate_rows(self, client: Any) -> None:
        """Twice is the same as once.

        The tree now has parent/child links, and a child's parent_id depends on
        the parent row having an id. Re-seeding has to be as safe as it was
        before the nesting, and "safe" here means provable rather than assumed.
        """
        first = await client.post("/api/v1/apps/seed")
        assert first.status_code == 200, first.text
        first_counts = first.json()
        assert first_counts["created_menus"] > 0, (
            "第一次 seed 一个菜单行都没建 —— 夹具或种子数据坏了, 后面几条断言会假绿"
        )

        second = await client.post("/api/v1/apps/seed")
        assert second.status_code == 200, second.text
        after_second = second.json()

        assert after_second["created_apps"] == 0
        assert after_second["created_menus"] == 0

        listed = await client.get("/api/v1/apps")
        assert listed.status_code == 200
        items = listed.json()["items"]

        def count_groups(app_item: dict[str, Any]) -> int:
            return sum(
                1 + count_groups(g) for g in app_item.get("menus") or []  # type: ignore[arg-type]
            )

        for app_item in items:
            assert count_groups(app_item) == len(app_item.get("menus") or []), (
                f"{app_item['code']} 的菜单树结构异常"
            )

    async def test_deactivated_apps_disappear_from_the_list(self, client: Any) -> None:
        """The four-app navigation this seed replaced must not linger.

        Seeding only ever adds, so without an explicit step the old
        ``node-mgmt`` / ``flow-mgmt`` / ``exec-monitor`` rows would stay visible
        forever and the sidebar would show both navigations.
        """
        await client.post("/api/v1/apps/seed")
        listed = await client.get("/api/v1/apps")
        codes = {a["code"] for a in listed.json()["items"]}
        assert codes <= {"test-dev", "runtime", "system"}, f"出现了不该在的 app: {codes}"
        assert not (codes & {"node-mgmt", "flow-mgmt", "exec-monitor"})


@pytest.mark.parametrize("required", [["exec:read"], ["node:read"], ["flow:read"]])
def test_seed_data_declares_the_runtime_pages(required: list[str]) -> None:
    """The pages a scope is supposed to grant are actually declared with it.

          A menu requiring a permission nothing holds is a menu nobody sees, which is
      indistinguishable from a broken UI. The pairing matters in both directions:
      a scope with no page, and a page with no scope, are each half of a grant that
      does not happen. The role side lives in ``auth/rbac.py``.
      """
    rows = [
        m
        for m in _all_rows()
        if m.get("route_path")
        and m.get("required_permissions")
        and set(required) & set(m["required_permissions"])
    ]
    assert rows, f"没有任何页面声明 {required}"
    for row in rows:
        assert row.get("route_path"), f"{row['code']} 要求 {required} 却没有可导航的页面"


@pytest.mark.parametrize("app_code", ["test-dev", "runtime"])
def test_a_group_requires_what_its_pages_require(app_code: str) -> None:
    """A group must not be more permissive than the pages under it.

    This is the bug this assertion was written for. Groups were first seeded
    with ``required_permissions: None``, which
    :func:`_filter_menus_by_permissions` treats as *visible to everyone* — so
    every sidebar header rendered for a ``read``-only account, while the pages
    under them were correctly hidden. Eight headers and nothing in them.

    Requiring the union of the children's permissions fixes it for two reasons:
    the group's visibility is derived from its contents rather than restated,
    and the test below can then check the relationship instead of trusting a
    hand-maintained list. Combined with the empty-group pruning in
    ``_build_menu_tree``, a user holding one of several permissions sees the
    header and only the pages they may see.
    """
    app_def = next(a for a in default_apps if a["code"] == app_code)
    offenders: list[str] = []
    for group in app_def["menus"]:
        children = group.get("children") or []
        if not children:
            continue
        union: set[str] = set()
        for page in children:
            union |= set(page.get("required_permissions") or [])
        declared = set(group.get("required_permissions") or [])
        if declared != union:
            offenders.append(
                f"{app_def['code']}/{group['code']}: 声明 {sorted(declared)}, "
                f"子页并集 {sorted(union)}"
            )
    assert not offenders, (
        "分组的权限与其子页不一致 —— 分组声明 None 等于对所有人可见:\n"
        + "\n".join(f"  {o}" for o in offenders)
    )
