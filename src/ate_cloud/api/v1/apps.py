"""Apps and Menus API endpoints.

Provides DB-driven application and menu routing for the frontend Portal.
- GET /api/v1/apps                   - List all active apps (filtered by user permissions)
- GET /api/v1/apps/{app_id}          - Get app with menu tree (filtered by user permissions)
- POST /api/v1/apps/seed             - Seed default apps and menus (idempotent, open)
- POST /api/v1/apps/{app_id}/menus   - Create menu (admin only)
- PUT /api/v1/apps/{app_id}/menus/{menu_id}  - Update menu (admin only)
- DELETE /api/v1/apps/{app_id}/menus/{menu_id} - Delete menu (admin only)
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ate_cloud.auth.dependencies import get_current_user, require_scopes
from ate_cloud.auth.rbac import get_db_effective_scopes, is_superuser
from ate_cloud.db import get_db
from ate_cloud.models.app_menu import App, AppMenu
from ate_cloud.models.user import User
from ate_cloud.schemas.app_menu import (
    AppListResponse,
    AppMenuResponse,
    AppMenuTree,
    AppResponse,
    AppWithMenusResponse,
    MenuCreateRequest,
    MenuUpdateRequest,
)

router = APIRouter(prefix="/apps", tags=["apps"])

# Default apps with their menus (seed data — code is the idempotency key).
# Menu route_path values mirror resolvable frontend routes; for routes with a
# dynamic segment (e.g. /operator/:station_id) the entry points at a concrete
# default path because AppLayout strips /:param segments on menu click.
default_apps: list[dict[str, Any]] = [
    # ── 产测开发 ────────────────────────────────────────────────────────────
    # Top-level mode 1. Engineering state: author the test, do not run it.
    # NI TestStand's architecture card puts the split at "depending on mode,
    # edit, execute, and debug test sequences" — mode is what separates the
    # engineering surface from the operating surface, so it is the top level.
    {
        "code": "test-dev",
        "name": "产测开发",
        "description": "规格书驱动的需求、用例与产测流程编排",
        "icon": "Edit",
        "sort_order": 1,
        "menus": [
            {
                "code": "requirements",
                "name": "需求与用例",
                "route_path": None,
                "route_name": None,
                "icon": "Document",
                "sort_order": 1,
                # Union of the pages below: a group with no requirement of its own
                # would be visible to every account, because the filter reads an
                # empty list as 'no restriction'. See test_a_group_requires_what_its_
                # pages_require.
                "required_permissions": ["knowledge:read", "knowledge:write"],
                "children": [
                    # Lands the mode. 型号总览 is the spec's intended landing page
                    # but belongs to P5 (import wizard), so the page that already
                    # answers "what does this model have to be tested against"
                    # takes the position: every requirement, its cases and its
                    # conditions, with the approval state visible per row.
                    {
                        "code": "traceability",
                        "name": "需求追溯矩阵",
                        "route_path": "/dev/traceability",
                        "route_name": "TraceabilityMatrix",
                        "icon": "Share",
                        "sort_order": 1,
                        "required_permissions": ["knowledge:read"],
                    },
                    {
                        "code": "condition-review",
                        "name": "条件评审向导",
                        "route_path": "/dev/condition-review",
                        "route_name": "AteragReviewWizard",
                        "icon": "Checked",
                        "sort_order": 2,
                        "required_permissions": ["knowledge:read", "knowledge:write"],
                    },
                ],
            },
            {
                "code": "process",
                "name": "流程与脚本",
                "route_path": None,
                "route_name": None,
                "icon": "Connection",
                "sort_order": 2,
                # Union of the pages below: a group with no requirement of its own
                # would be visible to every account, because the filter reads an
                # empty list as 'no restriction'. See test_a_group_requires_what_its_
                # pages_require.
                "required_permissions": ["flow:read"],
                "children": [
                    {
                        "code": "sequences",
                        "name": "流程列表",
                        "route_path": "/dev/sequences",
                        "route_name": "SequenceList",
                        "icon": "List",
                        "sort_order": 1,
                        "required_permissions": ["flow:read"],
                    },
                    # Points at the parameterless editor, not at ``sequences/:id``.
                    # Clicking a menu strips ``:param`` segments, so a dynamic path
                    # here would land on the list page while the menu claimed to
                    # open the editor.
                    {
                        "code": "sequence-editor",
                        "name": "流程编排",
                        "route_path": "/dev/editor",
                        "route_name": "SequenceEditor",
                        "icon": "Edit",
                        "sort_order": 2,
                        "required_permissions": ["flow:read"],
                    },
                    # 流程节点模板 keeps 节点. A flow node is a step in the test
                    # sequence graph; a 工位 is a physical position on the line.
                    # The spec renames the station concept and keeps this one
                    # (§4.2 lists it as 流程节点模板), and conflating them would
                    # put "where do I stand" in the sequence editor.
                    {
                        "code": "flow-templates",
                        "name": "流程节点模板",
                        "route_path": "/dev/templates",
                        "route_name": "NodeTemplates",
                        "icon": "CopyDocument",
                        "sort_order": 3,
                        "required_permissions": ["flow:read"],
                    },
                    {
                        "code": "scripts",
                        "name": "脚本管理",
                        "route_path": "/dev/scripts",
                        "route_name": "ScriptManagement",
                        "icon": "Document",
                        "sort_order": 4,
                        "required_permissions": ["flow:read"],
                    },
                    {
                        "code": "fixture-designer",
                        "name": "工装设计调试器",
                        "route_path": "/dev/fixture-designer",
                        "route_name": "FixtureDesigner",
                        "icon": "SetUp",
                        "sort_order": 5,
                        "required_permissions": ["flow:read"],
                    },
                ],
            },
            {
                "code": "station-binding",
                "name": "工位绑定",
                "route_path": None,
                "route_name": None,
                "icon": "Link",
                "sort_order": 3,
                # Union of the pages below: a group with no requirement of its own
                # would be visible to every account, because the filter reads an
                # empty list as 'no restriction'. See test_a_group_requires_what_its_
                # pages_require.
                "required_permissions": ["flow:read", "node:read"],
                "children": [
                    # 节点流程绑定 renamed to 工位管理 (§4.5): what it binds is a
                    # 工位 to a flow, and both halves now say so.
                    {
                        "code": "station-management",
                        "name": "工位管理",
                        "route_path": "/dev/station-management",
                        "route_name": "StationManagement",
                        "icon": "Link",
                        "sort_order": 1,
                        "required_permissions": ["flow:read", "node:read"],
                    },
                ],
            },
        ],
    },
    # ── 运行监控 ────────────────────────────────────────────────────────────
    # Top-level mode 2. Operating state: run the test, watch it, debug it.
    {
        "code": "runtime",
        "name": "运行监控",
        "description": "产线实时运行、执行数据与故障诊断",
        "icon": "DataLine",
        "sort_order": 2,
        "menus": [
            {
                "code": "line",
                "name": "产线运行",
                "route_path": None,
                "route_name": None,
                "icon": "Odometer",
                "sort_order": 1,
                # Union of the pages below: a group with no requirement of its own
                # would be visible to every account, because the filter reads an
                # empty list as 'no restriction'. See test_a_group_requires_what_its_
                # pages_require.
                "required_permissions": ["exec:read"],
                "children": [
                    {
                        "code": "dashboard",
                        "name": "实时看板",
                        "route_path": "/ops/dashboard",
                        "route_name": "Dashboard",
                        "icon": "Odometer",
                        "sort_order": 1,
                        "required_permissions": ["exec:read"],
                    },
                    # 操作员面板 is deliberately absent.
                    #
                    # It used to be seeded pointing at /operator/default, because
                    # a menu entry needs a concrete path and ``:station_id`` has
                    # none. That made the menu claim a page existed for every
                    # station while actually opening one for a station called
                    # "default", which does not exist.
                    #
                    # It is a per-station screen: it is for the operator standing
                    # at a station. The route stays at /operator/:station_id, and
                    # the 工位列表 page links into it per row — the only place a
                    # station id is actually known. Re-adding it as a menu entry
                    # would need a station picker in front of it first.
                ],
            },
            {
                "code": "execution",
                "name": "执行与数据",
                "route_path": None,
                "route_name": None,
                "icon": "DataLine",
                "sort_order": 2,
                # Union of the pages below: a group with no requirement of its own
                # would be visible to every account, because the filter reads an
                # empty list as 'no restriction'. See test_a_group_requires_what_its_
                # pages_require.
                "required_permissions": ["exec:read"],
                "children": [
                    {
                        "code": "history",
                        "name": "执行历史",
                        "route_path": "/ops/history",
                        "route_name": "ExecutionHistory",
                        "icon": "Clock",
                        "sort_order": 1,
                        "required_permissions": ["exec:read"],
                    },
                    {
                        "code": "measurements",
                        "name": "测量数据",
                        "route_path": "/ops/measurements",
                        "route_name": "MeasurementExplorer",
                        "icon": "TrendCharts",
                        "sort_order": 2,
                        "required_permissions": ["exec:read"],
                    },
                    {
                        "code": "tracing",
                        "name": "追溯查询",
                        "route_path": "/ops/tracing",
                        "route_name": "TracingViewer",
                        "icon": "Search",
                        "sort_order": 3,
                        "required_permissions": ["exec:read"],
                    },
                    {
                        "code": "reports",
                        "name": "测试报告",
                        "route_path": "/ops/reports",
                        "route_name": "Reports",
                        "icon": "Tickets",
                        "sort_order": 4,
                        "required_permissions": ["exec:read"],
                    },
                ],
            },
            {
                "code": "debug",
                "name": "调试",
                "route_path": None,
                "route_name": None,
                "icon": "VideoPlay",
                "sort_order": 3,
                "required_permissions": ["exec:read"],
                "children": [
                    {
                        "code": "simulation-console",
                        "name": "仿真调试控制台",
                        "route_path": "/ops/simulation",
                        "route_name": "SimulationConsole",
                        "icon": "VideoPlay",
                        "sort_order": 1,
                        "required_permissions": ["exec:read"],
                    },
                ],
            },
            {
                "code": "station-ops",
                "name": "工位运维",
                "route_path": None,
                "route_name": None,
                "icon": "Setting",
                "sort_order": 4,
                # Union of the pages below: a group with no requirement of its own
                # would be visible to every account, because the filter reads an
                # empty list as 'no restriction'. See test_a_group_requires_what_its_
                # pages_require.
                "required_permissions": ["node:read", "system:read"],
                "children": [
                    # Reads the Station table built in P0 (plants -> stations),
                    # which is what the plant/station context selector is scoped
                    # to. Distinct from 工位执行器 below.
                    {
                        "code": "stations",
                        "name": "工位列表",
                        "route_path": "/ops/stations",
                        "route_name": "StationList",
                        "icon": "List",
                        "sort_order": 1,
                        "required_permissions": ["node:read"],
                    },
                    # The worker registry: executor processes that report
                    # heartbeats and run flows. It used to be titled 节点列表,
                    # which claimed it was the station list while reading a
                    # different table entirely.
                    {
                        "code": "workers",
                        "name": "工位执行器",
                        "route_path": "/ops/workers",
                        "route_name": "WorkerRegistry",
                        "icon": "Cpu",
                        "sort_order": 2,
                        "required_permissions": ["node:read"],
                    },
                    {
                        "code": "calibration",
                        "name": "校准管理",
                        "route_path": "/ops/calibration",
                        "route_name": "CalibrationPanel",
                        "icon": "Aim",
                        "sort_order": 3,
                        "required_permissions": ["system:read"],
                    },
                    {
                        "code": "changeover",
                        "name": "产品切换",
                        "route_path": "/ops/changeover",
                        "route_name": "ProductChangeover",
                        "icon": "Switch",
                        "sort_order": 4,
                        "required_permissions": ["system:read"],
                    },
                ],
            },
            {
                "code": "fault",
                "name": "故障智能",
                "route_path": None,
                "route_name": None,
                "icon": "Warning",
                "sort_order": 5,
                # Union of the pages below: a group with no requirement of its own
                # would be visible to every account, because the filter reads an
                # empty list as 'no restriction'. See test_a_group_requires_what_its_
                # pages_require.
                "required_permissions": ["node:read", "node:write", "system:read"],
                "children": [
                    {
                        "code": "fault-cases",
                        "name": "工位故障案例库",
                        "route_path": "/ops/fault-cases",
                        "route_name": "FaultCaseLibrary",
                        "icon": "Collection",
                        "sort_order": 1,
                        "required_permissions": ["node:read", "node:write"],
                    },
                    {
                        "code": "fmea",
                        "name": "FMEA管理",
                        "route_path": "/ops/fmea",
                        "route_name": "FmeaManagement",
                        "icon": "Tickets",
                        "sort_order": 2,
                        "required_permissions": ["system:read"],
                    },
                ],
            },
        ],
    },
    # ── 系统 ────────────────────────────────────────────────────────────────
    # Not a top-level mode. The spec's top bar has exactly two items, so system
    # administration is reached from the account dropdown (§4.4) — but the
    # entries stay menu rows rather than becoming dropdown items, because
    # test_admin_pages_in_menu.py documents why: a page reachable only by
    # remembering a dropdown is a page nobody finds, and a `v-if="isAdmin"`
    # display flag is advisory where `required_permissions` is enforced.
    {
        "code": "system",
        "name": "系统管理",
        "description": "系统配置、用户与角色权限",
        "icon": "Setting",
        "sort_order": 3,
        "menus": [
            {
                "code": "settings",
                "name": "系统设置",
                "route_path": "/system/settings",
                "route_name": "Settings",
                "icon": "Tools",
                "sort_order": 1,
                "required_permissions": ["system:read"],
            },
            {
                "code": "users",
                "name": "用户管理",
                "route_path": "/system/users",
                "route_name": "UserManagement",
                "icon": "User",
                "sort_order": 2,
                "required_permissions": ["admin:read"],
            },
            {
                "code": "roles",
                "name": "角色与权限",
                "route_path": "/system/roles",
                "route_name": "RoleManagement",
                "icon": "Lock",
                "sort_order": 3,
                "required_permissions": ["admin:read"],
            },
        ],
    },
]


def _iter_seed_menus(
    menus: list[dict[str, Any]], parent_code: str | None = None
) -> list[dict[str, Any]]:
    """Flatten the seed tree into parent-before-child order.

    Each row carries ``_parent_code`` — the parent's seed code — because
    ``seed_apps`` cannot fill in ``parent_id`` during a single pass: a parent's
    id is a fresh uuid that does not exist until the parent row is written, and
    the parent may not have been created yet when the child is visited. So the
    flat list keeps the code and ``seed_apps`` resolves it in a second pass.

    Returns copies. ``default_apps`` is a module-level constant shared across
    every seed invocation, so popping ``children`` off the caller's dicts would
    silently turn the second seed call into a flat one — and the symptom would
    be a menu that loses its groups the second time the seed runs.
    """
    flat: list[dict[str, Any]] = []
    for menu in menus:
        children = menu.get("children") or []
        row = {k: v for k, v in menu.items() if k != "children"}
        row["_parent_code"] = parent_code
        flat.append(row)
        flat.extend(_iter_seed_menus(children, menu["code"]))
    return flat


def _filter_menus_by_permissions(
    menus: list[AppMenu], user_permissions: set[str]
) -> list[AppMenu]:
    """Filter menus based on the user's permissions.

    A menu is visible if:
    - the user is an admin (see :func:`is_superuser`), or
    - required_permissions is None or empty (visible to all authenticated users)
    - The intersection of user_permissions and required_permissions is non-empty
      (user has at least one of the required permissions)

    The admin bypass is load-bearing, not a convenience: it keeps admin reachable
    even when a guard names a scope no role carries. It used to be the *only* thing
    keeping the product reachable. :data:`ROLE_SCOPES` granted just the flat
    ``admin``/``read``/``write``/``execute`` set, so its intersection with the seed's
    namespaced vocabulary was empty for *every* account, each app was dropped for
    having no visible menu, and a correct login landed on an empty main screen.
    ``ROLE_SCOPES`` now carries those scopes per role — the policy is documented in
    ``auth/rbac.py`` — so the bypass is a safety net rather than the mechanism.

    Any-of, not all-of: a group's ``required_permissions`` is the union of its
    children's, so requiring every member would make the group stricter than the
    pages it contains. Children are filtered individually out of the same flat list
    before the tree is built, so a page an account cannot reach disappears even when
    its group survives.

    That gap was not hypothetical: it is what shipped, and it is why the roles were
    widened. ``tests/cloud/test_menu_visibility.py`` now asserts the mapping in both
    directions — every seeded permission is reachable by some role, and no
    administration page is reachable by ``read`` — so the empty screen cannot come
    back unnoticed.

    Args:
        menus: Flat list of AppMenu ORM objects.
        user_permissions: Set of permission/scope strings the user holds.

    Returns:
        Filtered list of AppMenu objects visible to the user.
    """
    if is_superuser(user_permissions):
        return list(menus)

    result: list[AppMenu] = []
    for m in menus:
        if not m.required_permissions:
            # No required permissions → visible to all authenticated users
            result.append(m)
        elif user_permissions & set(m.required_permissions):
            # User has at least one of the required permissions
            result.append(m)
    return result


def _build_menu_tree(menus: list[AppMenu]) -> list[AppMenuTree]:
    """Build a nested menu tree from a flat menu list.

    Groups are pruned when every page under them has been filtered out. A group
    row carries the union of its children's requirements rather than one of its
    own — a group with no requirement of its own reads as "no restriction" and
    becomes visible to every account (see :func:`seed_apps`). So a group can
    outlive the permissions that made it interesting: it stays while any child
    survives the permission filter, and is dropped only once all of them are
    filtered out.
    """
    by_id: dict[str, AppMenuTree] = {}
    for m in menus:
        by_id[m.id] = AppMenuTree(
            id=m.id,
            app_id=m.app_id,
            parent_id=m.parent_id,
            code=m.code,
            name=m.name,
            route_path=m.route_path,
            route_name=m.route_name,
            icon=m.icon,
            sort_order=m.sort_order,
            is_active=m.is_active,
            required_permissions=m.required_permissions,
            children=[],
        )
    roots: list[AppMenuTree] = []
    for node in by_id.values():
        if node.parent_id and node.parent_id in by_id:
            by_id[node.parent_id].children.append(node)
        else:
            roots.append(node)

    def prune(nodes: list[AppMenuTree]) -> list[AppMenuTree]:
        kept: list[AppMenuTree] = []
        for node in nodes:
            node.children = prune(node.children)
            # A routeless node is a group: keep it only if something survived
            # underneath. A node with a route is a page and is always kept.
            if node.route_path or node.children:
                kept.append(node)
        kept.sort(key=lambda n: (n.sort_order, n.code))
        return kept

    return prune(roots)


@router.get("", response_model=AppListResponse)
async def list_apps(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AppListResponse:
    """List all active apps ordered by sort_order.

    Only apps that have at least one menu visible to the authenticated user
    are returned.
    """
    user_permissions = set(await get_db_effective_scopes(user.role, user.scopes, db))

    result = await db.execute(
        select(App)
        .options(selectinload(App.menus))
        .where(App.is_active == True)  # noqa: E712
        .order_by(App.sort_order)
    )
    apps = result.scalars().all()

    visible_apps: list[AppResponse] = []
    for app in apps:
        active_menus = [m for m in app.menus if m.is_active]
        visible_menus = _filter_menus_by_permissions(active_menus, user_permissions)
        if visible_menus:
            visible_apps.append(AppResponse.model_validate(app))

    return AppListResponse(items=visible_apps, total=len(visible_apps))


@router.get("/{app_id}", response_model=AppWithMenusResponse)
async def get_app(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AppWithMenusResponse:
    """Get a single app with its menu tree.

    Menus are filtered based on the authenticated user's permissions.
    """
    user_permissions = set(await get_db_effective_scopes(user.role, user.scopes, db))

    result = await db.execute(
        select(App)
        .options(selectinload(App.menus))
        .where(App.id == app_id)
    )
    app = result.scalar_one_or_none()
    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

    active_menus = [m for m in app.menus if m.is_active]
    visible_menus = _filter_menus_by_permissions(active_menus, user_permissions)
    menu_tree = _build_menu_tree(visible_menus)
    return AppWithMenusResponse(
        id=app.id,
        code=app.code,
        name=app.name,
        description=app.description,
        icon=app.icon,
        sort_order=app.sort_order,
        is_active=app.is_active,
        menus=menu_tree,
    )


@router.post("/{app_id}/menus", response_model=AppMenuResponse, status_code=status.HTTP_201_CREATED)
async def create_menu(
    app_id: str,
    body: MenuCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_scopes("admin")),
) -> AppMenuResponse:
    """Create a new menu item under the specified app. Admin only."""
    # Verify app exists
    result = await db.execute(select(App).where(App.id == app_id))
    app = result.scalar_one_or_none()
    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

    # Check for duplicate menu code within the same app
    result = await db.execute(
        select(AppMenu).where(AppMenu.app_id == app_id, AppMenu.code == body.code)
    )
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Menu with code '{body.code}' already exists in this app",
        )

    # Validate parent_id if provided
    if body.parent_id is not None:
        result = await db.execute(
            select(AppMenu).where(AppMenu.id == body.parent_id, AppMenu.app_id == app_id)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Parent menu not found or does not belong to this app",
            )

    menu = AppMenu(
        id=str(uuid.uuid4()),
        app_id=app_id,
        code=body.code,
        name=body.name,
        route_path=body.route_path,
        route_name=body.route_name,
        icon=body.icon,
        sort_order=body.sort_order,
        is_active=body.is_active if body.is_active is not None else True,
        parent_id=body.parent_id,
        required_permissions=body.required_permissions,
    )
    db.add(menu)
    await db.commit()
    await db.refresh(menu)
    return AppMenuResponse.model_validate(menu)


@router.put("/{app_id}/menus/{menu_id}", response_model=AppMenuResponse)
async def update_menu(
    app_id: str,
    menu_id: str,
    body: MenuUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_scopes("admin")),
) -> AppMenuResponse:
    """Update an existing menu item. Admin only."""
    result = await db.execute(
        select(AppMenu).where(AppMenu.id == menu_id, AppMenu.app_id == app_id)
    )
    menu = result.scalar_one_or_none()
    if menu is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Menu not found")

    update_data = body.model_dump(exclude_unset=True)

    # Validate parent_id if being updated
    if "parent_id" in update_data and update_data["parent_id"] is not None:
        if update_data["parent_id"] == menu_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Menu cannot be its own parent",
            )
        result = await db.execute(
            select(AppMenu).where(
                AppMenu.id == update_data["parent_id"], AppMenu.app_id == app_id
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Parent menu not found or does not belong to this app",
            )

    # Check for code conflict if code is being updated
    if "code" in update_data and update_data["code"] != menu.code:
        result = await db.execute(
            select(AppMenu).where(
                AppMenu.app_id == app_id,
                AppMenu.code == update_data["code"],
                AppMenu.id != menu_id,
            )
        )
        if result.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Menu with code '{update_data['code']}' already exists in this app",
            )

    for field, value in update_data.items():
        setattr(menu, field, value)

    await db.commit()
    await db.refresh(menu)
    return AppMenuResponse.model_validate(menu)


@router.delete("/{app_id}/menus/{menu_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_menu(
    app_id: str,
    menu_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_scopes("admin")),
) -> None:
    """Delete a menu item. Admin only."""
    result = await db.execute(
        select(AppMenu).where(AppMenu.id == menu_id, AppMenu.app_id == app_id)
    )
    menu = result.scalar_one_or_none()
    if menu is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Menu not found")

    await db.delete(menu)
    await db.commit()


@router.post("/seed", response_model=dict)
async def seed_apps(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Seed default apps and menus. Idempotent — code is the key.

    Existing rows are brought up to date rather than left alone. Three fields
    have to converge on the seed, because the seed is the thing that changed:

    ``route_path``
        Reorganising the navigation renames routes. A row that kept its old
        path would keep navigating to a route that may no longer exist — a menu
        entry pointing at nothing, which is the failure this whole phase is
        about. Updating ``required_permissions`` alone is not enough.
    ``parent_id``
        A page that moved under a group needs the link, or it renders as a
        top-level entry with no header above it.
    ``required_permissions``
        Unchanged in spirit, kept for the same reason as before.

    Rows whose code is no longer in the seed are deactivated, not deleted. The
    four-app navigation this seed replaced (``node-mgmt``, ``flow-mgmt``,
    ``exec-monitor`` and their menus) would otherwise stay visible forever,
    because seeding only ever adds. Deactivation is reversible from the
    database if the change turns out to be wrong; deletion is not, and the
    rollback of a menu restructure is exactly the case where you want it.
    """
    created_apps = 0
    created_menus = 0
    updated_menus = 0
    deactivated_apps = 0
    deactivated_menus = 0
    reactivated_apps = 0

    seed_app_codes = {a["code"] for a in default_apps}
    seed_menu_codes: dict[str, set[str]] = {
        a["code"]: {m["code"] for m in _iter_seed_menus(a["menus"])} for a in default_apps
    }

    for app_data in default_apps:
        menus_data = _iter_seed_menus(app_data["menus"])
        # Non-mutating copy without the "menus" key (default_apps is a
        # module-level constant shared across seed invocations).
        app_fields = {k: v for k, v in app_data.items() if k != "menus"}
        # Check if app exists by code
        result = await db.execute(select(App).where(App.code == app_fields["code"]))
        app = result.scalar_one_or_none()
        if app is None:
            app = App(id=str(uuid.uuid4()), **app_fields)
            db.add(app)
            await db.flush()  # app.id must exist before menus reference it
            created_apps += 1
        elif not app.is_active:
            app.is_active = True
            reactivated_apps += 1

        # seed code -> the ORM row, so a child can name its parent without a
        # second query.
        rows: dict[str, AppMenu] = {}

        for m_data in menus_data:
            menu_result = await db.execute(
                select(AppMenu).where(AppMenu.app_id == app.id, AppMenu.code == m_data["code"])
            )
            existing = menu_result.scalar_one_or_none()
            if existing is None:
                # ``_parent_code`` is bookkeeping for the second pass, not a column.
                fields = {k: v for k, v in m_data.items() if k != "_parent_code"}
                existing = AppMenu(
                    id=str(uuid.uuid4()),
                    app_id=app.id,
                    **fields,
                )
                db.add(existing)
                await db.flush()
                created_menus += 1
            else:
                changed = False
                for field in ("name", "route_path", "route_name", "icon", "sort_order"):
                    if getattr(existing, field) != m_data.get(field):
                        setattr(existing, field, m_data.get(field))
                        changed = True
                if existing.required_permissions != m_data.get("required_permissions"):
                    existing.required_permissions = m_data.get("required_permissions")
                    changed = True
                if not existing.is_active:
                    existing.is_active = True
                    changed = True
                if changed:
                    updated_menus += 1
            rows[m_data["code"]] = existing

        # Second pass for parent links: a parent may have been created after the
        # child was visited, so the id is only known now.
        for m_data in menus_data:
            parent_code = m_data.get("_parent_code")
            if parent_code is None:
                continue
            parent = rows.get(parent_code)
            if parent is None:
                # The seed is inconsistent: a page names a group that is not
                # there. Fail loudly rather than leaving the page top-level,
                # which would render it with no header above it.
                raise RuntimeError(
                    f"seed app {app.code!r}: menu {m_data['code']!r} references "
                    f"unknown parent {parent_code!r}"
                )
            child = rows[m_data["code"]]
            if child.parent_id != parent.id:
                child.parent_id = parent.id
                updated_menus += 1

        # Anything under this app that the seed no longer lists is stale.
        stale = await db.execute(
            select(AppMenu).where(
                AppMenu.app_id == app.id, AppMenu.code.not_in(seed_menu_codes[app.code])
            )
        )
        for menu in stale.scalars().all():
            if menu.is_active:
                menu.is_active = False
                deactivated_menus += 1

    # Apps the seed no longer lists at all.
    orphans = await db.execute(select(App).where(App.code.not_in(seed_app_codes)))
    for app in orphans.scalars().all():
        if app.is_active:
            app.is_active = False
            deactivated_apps += 1

    await db.commit()
    return {
        "created_apps": created_apps,
        "created_menus": created_menus,
        "updated_menus": updated_menus,
        "deactivated_apps": deactivated_apps,
        "deactivated_menus": deactivated_menus,
        "reactivated_apps": reactivated_apps,
        "status": "ok",
    }
