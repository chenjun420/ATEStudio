"""Pydantic schemas for App and AppMenu API responses."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict


class AppMenuResponse(BaseModel):
    """Menu item response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    app_id: str
    parent_id: Optional[str] = None
    code: str
    name: str
    route_path: str
    route_name: Optional[str] = None
    icon: Optional[str] = None
    sort_order: int
    is_active: bool


class AppMenuTree(AppMenuResponse):
    """Menu item with nested children for tree rendering."""

    children: list["AppMenuTree"] = []


class AppResponse(BaseModel):
    """App response without menus."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    sort_order: int
    is_active: bool


class AppWithMenusResponse(AppResponse):
    """App response including its menu tree."""

    menus: list[AppMenuTree] = []


class AppListResponse(BaseModel):
    """Paginated app list response."""

    items: list[AppResponse]
    total: int
