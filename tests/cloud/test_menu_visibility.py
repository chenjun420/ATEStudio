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

from ate_cloud.api.v1.apps import _filter_menus_by_permissions, default_apps, seed_apps
from ate_cloud.auth.rbac import ROLE_SCOPES, is_superuser
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

    def test_no_seeded_permission_is_in_a_role_scope_set(self) -> None:
        """The trap, stated as a fact.

        Every seeded permission is namespaced (``domain:verb``) and no role
        grants any of them. This is why the intersection was empty, and it is
        asserted rather than assumed so that a future grant shows up here as a
        deliberate change instead of silently making the admin bypass redundant.
        """
        grantable = {s for scopes in ROLE_SCOPES.values() for s in scopes}
        leaked = _seeded_permissions() & grantable
        assert not leaked, f"这些菜单权限已由角色直接授予: {sorted(leaked)}"

    def test_admin_satisfies_every_seeded_permission(self) -> None:
        """The property that matters: an admin can see the whole product."""
        admin_scopes = set(ROLE_SCOPES["admin"])
        assert is_superuser(admin_scopes)
        # is_superuser short-circuits, so assert the pre-bypass view too — it is
        # what makes the bypass necessary rather than decorative.
        unmatched = {
            p for p in _seeded_permissions() if not (admin_scopes & {p})
        }
        assert unmatched, "admin 的 scope 已直接覆盖全部菜单权限, 通配可考虑移除"
        assert is_superuser(admin_scopes) or not unmatched


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
    """Non-admin roles still see nothing — stated, not hidden.

    No non-admin role holds a namespaced menu permission either, so a ``read``
    account logs in successfully and lands on the same empty screen. Which roles
    get which domain's ``:read`` is a policy decision (it is not derivable from
    the data), so it is left open rather than guessed. This test exists so that
    gap is visible to whoever reads the suite, and so that closing it turns this
    red.
    """

    @pytest.mark.asyncio
    async def test_non_admin_roles_cannot_see_seeded_menus(self, db: AsyncSession) -> None:
        await seed_apps(db)
        menus = (
            await db.execute(select(AppMenu).where(AppMenu.is_active.is_(True)))
        ).scalars().all()
        for role in ("read", "write", "execute"):
            scopes = set(ROLE_SCOPES[role])
            visible = _filter_menus_by_permissions(list(menus), scopes)
            assert not visible, (
                f"角色 {role} 现在能看到 {len(visible)} 个菜单 —— "
                "如果这是有意的, 请把角色到菜单权限的映射写进 ROLE_SCOPES 并更新本测试"
            )
