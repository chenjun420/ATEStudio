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
from ate_cloud.auth.rbac import get_db_effective_scopes
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
    {
        "code": "node-mgmt",
        "name": "节点管理",
        "description": "管理测试节点注册、状态监控及配置下发",
        "icon": "Monitor",
        "sort_order": 1,
        "menus": [
            {"code": "stations", "name": "节点列表", "route_path": "/node/stations", "route_name": "StationManagement", "icon": "List", "sort_order": 1, "required_permissions": ["node:read"]},
            {"code": "node-detail", "name": "节点详情", "route_path": "/node/stations/:id", "route_name": "NodeDetail", "icon": "View", "sort_order": 2, "required_permissions": ["node:read"]},
        ],
    },
    {
        "code": "flow-mgmt",
        "name": "流程管理",
        "description": "可视化流程编排、版本化管理及节点流程绑定",
        "icon": "Connection",
        "sort_order": 2,
        "menus": [
            {"code": "sequences", "name": "流程列表", "route_path": "/flow/sequences", "route_name": "SequenceList", "icon": "List", "sort_order": 1, "required_permissions": ["flow:read"]},
            {"code": "sequence-editor", "name": "流程编排", "route_path": "/flow/editor", "route_name": "SequenceEditor", "icon": "Edit", "sort_order": 2, "required_permissions": ["flow:read"]},
            {"code": "flow-templates", "name": "流程节点模板", "route_path": "/flow/templates", "route_name": "NodeTemplates", "icon": "CopyDocument", "sort_order": 3, "required_permissions": ["flow:read"]},
            {"code": "scripts", "name": "脚本管理", "route_path": "/flow/scripts", "route_name": "ScriptManagement", "icon": "Document", "sort_order": 4, "required_permissions": ["flow:read"]},
            {"code": "node-binding", "name": "节点流程绑定", "route_path": "/flow/binding", "route_name": "NodeFlowBinding", "icon": "Link", "sort_order": 5, "required_permissions": ["flow:read", "node:read"]},
            {"code": "fixture-designer", "name": "工装设计调试器", "route_path": "/flow/fixture-designer", "route_name": "FixtureDesigner", "icon": "SetUp", "sort_order": 6, "required_permissions": ["flow:read"]},
        ],
    },
    {
        "code": "exec-monitor",
        "name": "执行监控",
        "description": "实时监控测试执行状态、历史记录及报告导出",
        "icon": "DataLine",
        "sort_order": 3,
        "menus": [
            {"code": "dashboard", "name": "实时看板", "route_path": "/monitor/dashboard", "route_name": "Dashboard", "icon": "Odometer", "sort_order": 1, "required_permissions": ["exec:read"]},
            {"code": "history", "name": "执行历史", "route_path": "/monitor/history", "route_name": "ExecutionHistory", "icon": "Clock", "sort_order": 2, "required_permissions": ["exec:read"]},
            {"code": "measurements", "name": "测量数据", "route_path": "/monitor/measurements", "route_name": "MeasurementExplorer", "icon": "TrendCharts", "sort_order": 3, "required_permissions": ["exec:read"]},
            {"code": "reports", "name": "测试报告", "route_path": "/monitor/reports", "route_name": "Reports", "icon": "Tickets", "sort_order": 4, "required_permissions": ["exec:read"]},
            {"code": "tracing", "name": "追溯查询", "route_path": "/monitor/tracing", "route_name": "TracingViewer", "icon": "Search", "sort_order": 5, "required_permissions": ["exec:read"]},
            {"code": "simulation-console", "name": "仿真调试控制台", "route_path": "/monitor/simulation", "route_name": "SimulationConsole", "icon": "VideoPlay", "sort_order": 6, "required_permissions": ["exec:read"]},
            # OperatorView route is /operator/:station_id; AppLayout strips
            # /:param segments on menu click, so point at a concrete default.
            {"code": "operator-panel", "name": "操作员面板", "route_path": "/operator/default", "route_name": "OperatorView", "icon": "User", "sort_order": 7, "required_permissions": ["exec:read"]},  # noqa: E501
        ],
    },
    {
        "code": "system",
        "name": "系统管理",
        "description": "系统配置、产品切换及校准管理",
        "icon": "Setting",
        "sort_order": 4,
        "menus": [
            {"code": "settings", "name": "系统设置", "route_path": "/system/settings", "route_name": "Settings", "icon": "Tools", "sort_order": 1, "required_permissions": ["system:read"]},
            {"code": "changeover", "name": "产品切换", "route_path": "/system/changeover", "route_name": "ProductChangeover", "icon": "Switch", "sort_order": 2, "required_permissions": ["system:read"]},
            {"code": "calibration", "name": "校准管理", "route_path": "/system/calibration", "route_name": "CalibrationPanel", "icon": "Aim", "sort_order": 3, "required_permissions": ["system:read"]},
        ],
    },
]


def _filter_menus_by_permissions(
    menus: list[AppMenu], user_permissions: set[str]
) -> list[AppMenu]:
    """Filter menus based on the user's permissions.

    A menu is visible if:
    - required_permissions is None or empty (visible to all authenticated users)
    - The intersection of user_permissions and required_permissions is non-empty
      (user has at least one of the required permissions)

    Args:
        menus: Flat list of AppMenu ORM objects.
        user_permissions: Set of permission/scope strings the user holds.

    Returns:
        Filtered list of AppMenu objects visible to the user.
    """
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
    """Build a nested menu tree from flat menu list."""
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
    return roots


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
    """Seed default apps and menus. Idempotent — uses code as unique key.

    If a menu already exists, its required_permissions are updated to match
    the seed data (other fields are left unchanged).
    """
    created_apps = 0
    created_menus = 0
    updated_menus = 0

    for app_data in default_apps:
        menus_data = app_data["menus"]
        # Non-mutating copy without the "menus" key (default_apps is a
        # module-level constant shared across seed invocations).
        app_fields = {k: v for k, v in app_data.items() if k != "menus"}
        # Check if app exists by code
        result = await db.execute(select(App).where(App.code == app_fields["code"]))
        app = result.scalar_one_or_none()
        if app is None:
            app = App(id=str(uuid.uuid4()), **app_fields)
            db.add(app)
            created_apps += 1

        for m_data in menus_data:
            menu_result = await db.execute(
                select(AppMenu).where(AppMenu.app_id == app.id, AppMenu.code == m_data["code"])
            )
            existing = menu_result.scalar_one_or_none()
            if existing is None:
                menu = AppMenu(
                    id=str(uuid.uuid4()),
                    app_id=app.id,
                    **m_data,
                )
                db.add(menu)
                created_menus += 1
            else:
                # Update required_permissions if they differ from current DB values
                seed_perms = m_data.get("required_permissions")
                if existing.required_permissions != seed_perms:
                    existing.required_permissions = seed_perms
                    updated_menus += 1

    await db.commit()
    return {
        "created_apps": created_apps,
        "created_menus": created_menus,
        "updated_menus": updated_menus,
        "status": "ok",
    }
