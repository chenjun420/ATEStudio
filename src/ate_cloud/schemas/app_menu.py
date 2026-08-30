"""Pydantic schemas for App and AppMenu API responses."""

from pydantic import BaseModel, ConfigDict


class AppMenuResponse(BaseModel):
    """Menu item response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    app_id: str
    parent_id: str | None = None
    code: str
    name: str
    route_path: str
    route_name: str | None = None
    icon: str | None = None
    sort_order: int
    is_active: bool
    required_permissions: list[str] | None = None


class AppMenuTree(AppMenuResponse):
    """Menu item with nested children for tree rendering."""

    children: list["AppMenuTree"] = []


class AppResponse(BaseModel):
    """App response without menus."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    name: str
    description: str | None = None
    icon: str | None = None
    sort_order: int
    is_active: bool


class AppWithMenusResponse(AppResponse):
    """App response including its menu tree."""

    menus: list[AppMenuTree] = []


class AppListResponse(BaseModel):
    """Paginated app list response."""

    items: list[AppResponse]
    total: int


class MenuCreateRequest(BaseModel):
    """Request body for creating a new menu item."""

    code: str
    name: str
    route_path: str
    route_name: str | None = None
    icon: str | None = None
    sort_order: int = 0
    is_active: bool | None = None
    parent_id: str | None = None
    required_permissions: list[str] | None = None


class MenuUpdateRequest(BaseModel):
    """Request body for updating a menu item. All fields optional."""

    code: str | None = None
    name: str | None = None
    route_path: str | None = None
    route_name: str | None = None
    icon: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    parent_id: str | None = None
    required_permissions: list[str] | None = None
