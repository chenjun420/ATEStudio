"""The main screen must not come up empty after a successful login.

What shipped
------------
Login worked, the SPA was served, the health endpoint answered, and the home
screen had **no menus at all**. Cause: menus are permission-filtered against
``app_menus.required_permissions`` — ``system:read``, ``node:read``,
``exec:read``, ``flow:read`` — a namespaced vocabulary that exists only in the
seed data. ``ROLE_SCOPES`` grants the flat ``admin``/``read``/``write``/
``execute`` set and never names any of them, so the intersection was empty for
every account, every app was dropped for having no visible menu, and
``GET /api/v1/apps`` returned ``{"items": [], "total": 0}`` even for an admin.

Why the existing tests missed it
--------------------------------
``test_scope_reachability.py`` derives required scopes from ``require_scopes(...)``
calls in the endpoint sources. That covers one of the two kinds of permission
check in this codebase. A menu's requirement is not written in code at all — it is
a column value — so the scan had nothing to read. This module closes that half by
reading the seed data itself.

The property asserted
---------------------
Every permission the seeded menus require must be satisfiable by *some* account,
and specifically by an admin. A permission that nothing can ever hold is a menu
that can never be seen, which is indistinguishable from a broken UI.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from ate_cloud.api.v1.apps import (
    _build_menu_tree,
    _filter_menus_by_permissions,
    default_apps,
    seed_apps,
)
from ate_cloud.auth.rbac import (
    ADMIN_ONLY_SCOPES,
    PRODUCT_READ_SCOPES,
    PRODUCT_WRITE_SCOPES,
    ROLE_SCOPES,
    SCOPE_EXEC_READ,
    is_superuser,
)
from ate_cloud.models import Base
from ate_cloud.models.app_menu import App, AppMenu


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _menu(code: str, required: list[str] | None, *, active: bool = True) -> AppMenu:
    return AppMenu(
        id=str(uuid.uuid4()),
        app_id="app",
        parent_id=None,
        code=code,
        name=code,
        route_path=f"/{code}",
        route_name=code,
        icon=None,
        sort_order=0,
        is_active=active,
        required_permissions=required,
    )


# ── the vocabulary in the seed data ────────────────────────────────────────


def _seeded_permissions() -> set[str]:
    """Every permission string the shipped menus require."""
    out: set[str] = set()
    for app in default_apps:
        for menu in app["menus"]:
            out.update(menu.get("required_permissions") or [])
    return out


class TestSeededMenusAreReachable:
    def test_the_seed_actually_declares_permissions(self) -> None:
        """Guards the guard: an empty scan would make the test below vacuous."""
        perms = _seeded_permissions()
        assert perms, "default_apps 里没有 required_permissions —— 解析方式变了?"
        assert any(":" in p for p in perms), f"意外的取值: {sorted(perms)}"

    def test_every_seeded_permission_is_granted_by_some_role(self) -> None:
        """The gap this file was written to make visible is now closed.

        It used to assert the opposite — that no seeded permission appears in any
        role's scope set. That assertion is what kept the product admin-only: the
        menu vocabulary (``knowledge:read`` and friends) and the role vocabulary
        (``read``/``write``) did not intersect, so every menu filtered out for
        every account and only the ``is_superuser`` bypass let admin through.

        Now asserted forwards instead. A permission that *stops* being granted
        shows up here as a failure rather than as a silently empty sidebar.
        """
        grantable = {s for scopes in ROLE_SCOPES.values() for s in scopes}
        unreachable = _seeded_permissions() - grantable
        assert not unreachable, (
            f"这些菜单权限没有任何角色能拿到, 侧栏对谁都是空的: {sorted(unreachable)}"
        )

    def test_admin_satisfies_every_seeded_permission(self) -> None:
        """The property that matters: an admin can see the whole product.

        Checked two ways. ``is_superuser`` is the bypass that rescued the product
        while the vocabularies were disjoint, and it stays as the safety net. The
        explicit grants matter too: a data-driven guard that does not consult
        ``is_superuser`` would otherwise 403 an admin.
        """
        admin_scopes = set(ROLE_SCOPES["admin"])
        assert is_superuser(admin_scopes)
        unmatched = {p for p in _seeded_permissions() if not (admin_scopes & {p})}
        assert not unmatched, (
            f"admin 的 scope 未直接覆盖这些菜单权限: {sorted(unmatched)} —— "
            "通配旁路能过，但不做 is_superuser 检查的守卫会 403"
        )


class TestRoleToMenuPolicy:
    """Which role gets which domain — the decision left open, now made.

    ``auth/rbac.py`` documents the reasoning; these tests exist so that changing
    the policy is a deliberate edit to a named constant rather than a drift that
    only shows up as a confusing sidebar.
    """

    def test_administration_scopes_are_admin_only(self) -> None:
        """系统设置 / 用户管理 / 角色与权限 must not ride along with `read`.

        This is the one grant that cannot be narrowed later without the user
        having already seen the pages, so it is asserted for every non-admin role
        rather than left implicit in a list.
        """
        for role, scopes in ROLE_SCOPES.items():
            if role == "admin":
                assert ADMIN_ONLY_SCOPES <= set(scopes), "admin 必须持有全部管理面 scope"
                continue
            leaked = ADMIN_ONLY_SCOPES & set(scopes)
            assert not leaked, f"角色 {role} 不该持有管理面权限: {sorted(leaked)}"

    def test_read_role_sees_product_data_but_administration(self) -> None:
        """`read` is "read-only access", so it reads product data."""
        read = set(ROLE_SCOPES["read"])
        assert PRODUCT_READ_SCOPES and set(PRODUCT_READ_SCOPES) <= read
        # No write scopes, or it is not a read-only account.
        assert not (set(PRODUCT_WRITE_SCOPES) & read)

    def test_execute_role_gets_the_execution_domain_only(self) -> None:
        """`execute` is "execution access only (no read/write on resources)".

        The one addition is ``exec:read``: an operator who cannot see what the
        line is doing cannot act on it. That is a judgement call and it is the
        narrowest one that makes the role usable — it grants no product read.
        """
        execute = set(ROLE_SCOPES["execute"])
        assert SCOPE_EXEC_READ in execute
        product_reads = set(PRODUCT_READ_SCOPES) - {SCOPE_EXEC_READ}
        assert not (product_reads & execute), (
            f"execute 不该持有资源读权限: {sorted(product_reads & execute)}"
        )

    def test_write_role_is_read_plus_write(self) -> None:
        write = set(ROLE_SCOPES["write"])
        read = set(ROLE_SCOPES["read"])
        assert read <= write, "write 必须是 read 的超集"
        assert set(PRODUCT_WRITE_SCOPES) <= write

    def test_non_admin_roles_are_now_distinguishable(self) -> None:
        """The point of the mapping: the roles no longer behave identically.

        Before the mapping all three non-admin roles saw an identical empty
        screen, so the interface could not tell them apart at all.
        """
        menus = [
            _menu(f"m{i}", [perm])
            for i, perm in enumerate(sorted(_seeded_permissions()))
        ]
        visibility = {
            role: len(_filter_menus_by_permissions(menus, set(ROLE_SCOPES[role])))
            for role in ("read", "write", "execute")
        }
        assert len(set(visibility.values())) > 1, (
            f"三个非 admin 角色看到的菜单数完全相同: {visibility} —— "
            "角色到菜单的映射没有区分度"
        )
        assert visibility["execute"] > 0, (
            "execute 角色一个菜单都看不到 —— 它无法知道自己该执行什么"
        )
        assert visibility["read"] > 0, "read 角色仍然看不到任何菜单"


class TestFilterBehaviour:
    def test_admin_sees_a_menu_it_holds_no_named_permission_for(self) -> None:
        """The exact case that emptied the screen."""
        m = _menu("stations", ["node:read"])
        assert _filter_menus_by_permissions([m], set(ROLE_SCOPES["admin"])) == [m]

    def test_admin_sees_menus_with_no_requirement(self) -> None:
        m = _menu("plain", None)
        assert _filter_menus_by_permissions([m], set(ROLE_SCOPES["admin"])) == [m]

    def test_admin_seeing_everything_does_not_depend_on_menu_count(self) -> None:
        menus = [_menu(f"m{i}", [f"domain{i}:read"]) for i in range(19)]
        assert len(_filter_menus_by_permissions(menus, set(ROLE_SCOPES["admin"]))) == 19

    def test_no_requirement_is_visible_to_anyone(self) -> None:
        m = _menu("plain", None)
        assert _filter_menus_by_permissions([m], {"read"}) == [m]

    def test_unmet_requirement_is_hidden_from_a_non_admin(self) -> None:
        """The filter still bites where it should — the bypass is not a no-op."""
        m = _menu("stations", ["node:read"])
        assert _filter_menus_by_permissions([m], {"read", "write"}) == []

    def test_met_requirement_is_shown_to_a_non_admin(self) -> None:
        m = _menu("stations", ["node:read"])
        assert _filter_menus_by_permissions([m], {"node:read"}) == [m]

    def test_empty_scope_set_is_not_a_superuser(self) -> None:
        """``bool(scopes) and`` matters: ``{None}``-style falsy input must not pass."""
        assert not is_superuser(set())
        assert not is_superuser(None)


class TestSeededDataIsVisible:
    @pytest.mark.asyncio
    async def test_every_seeded_app_reports_at_least_one_visible_menu(self, db: AsyncSession) -> None:
        """End-to-end on the data: seed, then filter as an admin would.

        ``list_apps`` drops any app with no visible menu, so "every app visible"
        is the condition for a non-empty main screen. Asserting it against the
        real seed is what makes this a regression test rather than a unit test
        of the filter.
        """
        await seed_apps(db)
        admin_scopes = set(ROLE_SCOPES["admin"])

        apps = (
            await db.execute(
                select(App).options(selectinload(App.menus)).where(App.is_active.is_(True))
            )
        ).scalars().all()
        assert apps, "seed 没有写入任何 app"

        empty: list[str] = []
        for app in apps:
            active = [m for m in app.menus if m.is_active]
            if not _filter_menus_by_permissions(active, admin_scopes):
                empty.append(app.code)
        assert not empty, f"这些 app 对 admin 一个菜单都不可见: {empty}"


class TestKnownRemainingGap:
    """The admin-only gap is closed. What remains open, stated.

    This class used to assert that ``read``/``write``/``execute`` could see
    *nothing*, on the grounds that the role→menu mapping is a policy decision and
    guessing it would be worse than leaving it open. The policy has now been made
    (see :class:`TestRoleToMenuPolicy`), so the assertion is inverted: those roles
    must reach the menus their scopes entitle them to.

    What is still deliberately *not* enforced is the finest granularity — a
    ``read`` account sees every product page, including ones whose only
    requirement is a ``:read`` in a domain it has no particular stake in. Per-page
    custom roles would need the Role/Permission tables to be on the login path
    (``get_db_role_scopes`` is not), so that is separate work.
    """

    @pytest.mark.asyncio
    async def test_non_admin_roles_now_reach_their_menus(self, db: AsyncSession) -> None:
        await seed_apps(db)
        menus = (
            await db.execute(select(AppMenu).where(AppMenu.is_active.is_(True)))
        ).scalars().all()
        empty: dict[str, int] = {}
        for role in ("read", "write", "execute"):
            scopes = set(ROLE_SCOPES[role])
            visible = _filter_menus_by_permissions(list(menus), scopes)
            if not visible:
                empty[role] = 0
        assert not empty, (
            f"这些角色登录后侧栏仍然是空的: {empty} —— "
            "角色到菜单权限的映射回退了"
        )

    @pytest.mark.asyncio
    async def test_read_does_not_reach_administration_pages(
        self, db: AsyncSession
    ) -> None:
        """The narrowing that makes the widening acceptable.

        Without this the mapping would be "give read everything", which fixes the
        empty screen by handing out user administration.

        The assertion is on **pages**, not on any row that mentions an admin scope.
        A group's ``required_permissions`` is the union of its children's, and the
        filter is any-of, so a group holding both ``node:read`` and ``system:read``
        survives for a ``node:read`` holder — correctly, because its
        administration children are removed individually. Asserting on group rows
        would demand that a union be stricter than its most permissive member,
        which would contradict the design and hide legitimate containers.
        """
        await seed_apps(db)
        rows = (
            await db.execute(select(AppMenu).where(AppMenu.is_active.is_(True)))
        ).scalars().all()
        admin_pages = [
            m for m in rows
            # A page has a route; a group does not. Only pages are the thing that
            # must not be handed out.
            if m.route_path
            and m.required_permissions
            and set(m.required_permissions) & ADMIN_ONLY_SCOPES
        ]
        assert admin_pages, (
            "种子里没有任何管理面页面 —— 断言变空洞, 说明权限词汇变了"
        )
        visible = _filter_menus_by_permissions(
            admin_pages, set(ROLE_SCOPES["read"])
        )
        assert not visible, (
            f"read 角色看到了管理面页面: {[m.code for m in visible]}"
        )

    @pytest.mark.asyncio
    async def test_admin_pages_reach_admin_only(
        self, db: AsyncSession
    ) -> None:
        """The other half: widening the roles must not have cost admin anything."""
        await seed_apps(db)
        rows = (
            await db.execute(select(AppMenu).where(AppMenu.is_active.is_(True)))
        ).scalars().all()
        admin_pages = [
            m for m in rows
            if m.route_path
            and m.required_permissions
            and set(m.required_permissions) & ADMIN_ONLY_SCOPES
        ]
        visible = _filter_menus_by_permissions(admin_pages, set(ROLE_SCOPES["admin"]))
        assert len(visible) == len(admin_pages), (
            "admin 看不到部分管理面页面: "
            f"{sorted(m.code for m in admin_pages if m not in visible)}"
        )

    @pytest.mark.asyncio
    async def test_a_group_holding_an_admin_child_loses_that_child_not_itself(
        self, db: AsyncSession
    ) -> None:
        """Documents the union-plus-any-of behaviour at a leaf.

        工位运维 requires ``[node:read, system:read]`` because 校准管理 and 产品切换
        need ``system:read`` while 工位列表 and 工位执行器 need ``node:read``. A
        ``read`` account holds the latter only. The group stays (it still has
        pages) and the administration pages go — which is the whole point of
        filtering the flat list before nesting, rather than after.
        """
        await seed_apps(db)
        rows = (
            await db.execute(select(AppMenu).where(AppMenu.is_active.is_(True)))
        ).scalars().all()
        flat = _filter_menus_by_permissions(rows, set(ROLE_SCOPES["read"]))
        tree = _build_menu_tree(flat)
        by_code = {n.code: n for n in tree}

        assert "station-ops" in by_code, "工位运维 应保留 —— 它还有 read 可见的子页"
        kids = {k.code for k in by_code["station-ops"].children}
        assert {"stations", "workers"} <= kids
        assert not ({"calibration", "changeover"} & kids), (
            f"校准/产品切换 不该出现在 read 的菜单里: {sorted(kids)}"
        )
