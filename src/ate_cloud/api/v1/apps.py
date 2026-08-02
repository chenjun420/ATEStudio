"""Apps and Menus API endpoints.

Provides DB-driven application and menu routing for the frontend Portal.
- GET /api/v1/apps          - List all active apps
- GET /api/v1/apps/{id}     - Get app with menu tree
- POST /api/v1/apps/seed    - Seed default apps and menus (idempotent)
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ate_cloud.db import get_db
from ate_cloud.models.app_menu import App, AppMenu
from ate_cloud.schemas.app_menu import (
    AppListResponse,
    AppMenuResponse,
    AppMenuTree,
    AppResponse,
    AppWithMenusResponse,
)

router = APIRouter(prefix="/apps", tags=["apps"])


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
async def list_apps(db: AsyncSession = Depends(get_db)) -> AppListResponse:
    """List all active apps ordered by sort_order."""
    result = await db.execute(
        select(App)
        .where(App.is_active == True)  # noqa: E712
        .order_by(App.sort_order)
    )
    apps = result.scalars().all()
    return AppListResponse(items=[AppResponse.model_validate(a) for a in apps], total=len(apps))


@router.get("/{app_id}", response_model=AppWithMenusResponse)
async def get_app(app_id: str, db: AsyncSession = Depends(get_db)) -> AppWithMenusResponse:
    """Get a single app with its menu tree."""
    result = await db.execute(
        select(App)
        .options(selectinload(App.menus))
        .where(App.id == app_id)
    )
    app = result.scalar_one_or_none()
    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")
    menu_tree = _build_menu_tree([m for m in app.menus if m.is_active])
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


@router.post("/seed", response_model=dict)
async def seed_apps(db: AsyncSession = Depends(get_db)) -> dict:
    """Seed default apps and menus. Idempotent — uses code as unique key."""
    # Default apps with their menus
    default_apps = [
        {
            "code": "node-mgmt",
            "name": "节点管理",
            "description": "管理测试节点注册、状态监控及配置下发",
            "icon": "Monitor",
            "sort_order": 1,
            "menus": [
                {"code": "stations", "name": "节点列表", "route_path": "/node/stations", "route_name": "StationManagement", "icon": "List", "sort_order": 1},
                {"code": "node-detail", "name": "节点详情", "route_path": "/node/stations/:id", "route_name": "NodeDetail", "icon": "View", "sort_order": 2},
                {"code": "node-templates", "name": "节点模板", "route_path": "/node/templates", "route_name": "NodeTemplates", "icon": "CopyDocument", "sort_order": 3},
            ],
        },
        {
            "code": "flow-mgmt",
            "name": "流程管理",
            "description": "可视化流程编排、版本化管理及节点流程绑定",
            "icon": "Connection",
            "sort_order": 2,
            "menus": [
                {"code": "sequences", "name": "流程列表", "route_path": "/flow/sequences", "route_name": "SequenceList", "icon": "List", "sort_order": 1},
                {"code": "sequence-editor", "name": "流程编排", "route_path": "/flow/editor", "route_name": "SequenceEditor", "icon": "Edit", "sort_order": 2},
                {"code": "scripts", "name": "脚本管理", "route_path": "/flow/scripts", "route_name": "ScriptManagement", "icon": "Document", "sort_order": 3},
                {"code": "node-binding", "name": "节点流程绑定", "route_path": "/flow/binding", "route_name": "NodeFlowBinding", "icon": "Link", "sort_order": 4},
            ],
        },
        {
            "code": "exec-monitor",
            "name": "执行监控",
            "description": "实时监控测试执行状态、历史记录及报告导出",
            "icon": "DataLine",
            "sort_order": 3,
            "menus": [
                {"code": "dashboard", "name": "实时看板", "route_path": "/monitor/dashboard", "route_name": "Dashboard", "icon": "Odometer", "sort_order": 1},
                {"code": "history", "name": "执行历史", "route_path": "/monitor/history", "route_name": "ExecutionHistory", "icon": "Clock", "sort_order": 2},
                {"code": "measurements", "name": "测量数据", "route_path": "/monitor/measurements", "route_name": "MeasurementExplorer", "icon": "TrendCharts", "sort_order": 3},
                {"code": "reports", "name": "测试报告", "route_path": "/monitor/reports", "route_name": "Reports", "icon": "Tickets", "sort_order": 4},
            ],
        },
        {
            "code": "system",
            "name": "系统管理",
            "description": "系统配置、产品切换及校准管理",
            "icon": "Setting",
            "sort_order": 4,
            "menus": [
                {"code": "settings", "name": "系统设置", "route_path": "/system/settings", "route_name": "Settings", "icon": "Tools", "sort_order": 1},
                {"code": "changeover", "name": "产品切换", "route_path": "/system/changeover", "route_name": "ProductChangeover", "icon": "Switch", "sort_order": 2},
                {"code": "calibration", "name": "校准管理", "route_path": "/system/calibration", "route_name": "CalibrationPanel", "icon": "Aim", "sort_order": 3},
            ],
        },
    ]

    created_apps = 0
    created_menus = 0

    for app_data in default_apps:
        menus_data = app_data.pop("menus")
        # Check if app exists by code
        result = await db.execute(select(App).where(App.code == app_data["code"]))
        app = result.scalar_one_or_none()
        if app is None:
            app = App(id=str(uuid.uuid4()), **app_data)
            db.add(app)
            created_apps += 1

        for m_data in menus_data:
            result = await db.execute(
                select(AppMenu).where(AppMenu.app_id == app.id, AppMenu.code == m_data["code"])
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                menu = AppMenu(
                    id=str(uuid.uuid4()),
                    app_id=app.id,
                    **m_data,
                )
                db.add(menu)
                created_menus += 1

    await db.commit()
    return {"created_apps": created_apps, "created_menus": created_menus, "status": "ok"}
