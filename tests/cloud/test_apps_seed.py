"""Tests for the operator panel menu seed (RH-5, /monitor/operator).

Verifies that POST /api/v1/apps/seed creates the 操作员面板 menu entry under
the 执行监控 (exec-monitor) app, pointing at the OperatorView frontend route:

- Seed creates the entry with sibling-consistent shape.
- Re-running the seed is idempotent (no duplicate rows).
- Entry shape (keys / field types) matches sibling menu entries.
- Permission filtering behaves identically to siblings (exec:read required).

The seed mechanism is the ``default_apps`` list in
``src/ate_cloud/api/v1/apps.py::seed_apps`` — the single source of menu
seed data (alembic migration f1a2b3c4d5e6 is schema-only).

Route-path note: the OperatorView route is ``/operator/:station_id``
(standalone, no AppLayout). AppLayout's ``handleMenuSelect`` strips
``/:param`` segments before navigation, so seeding the raw pattern would
navigate to ``/operator`` → NotFound. Per plan v41 task 5, the entry points
directly at ``/operator/default``.
"""

from __future__ import annotations

from typing import Any

import pytest

from ate_cloud.auth.dependencies import get_current_user
from ate_cloud.models.user import User

OPERATOR_PANEL_CODE = "operator-panel"
EXEC_MONITOR_APP_CODE = "exec-monitor"
SIBLING_CODE = "dashboard"


async def _seed(client: Any) -> dict[str, Any]:
    """Run the RBAC + apps/menus seed endpoints; return apps-seed response.

    RBAC seed is idempotent and gives the dev-mode admin role the DB-backed
    permissions (e.g. ``exec:read``) that menu visibility filtering checks.
    """
    rbac = await client.post("/api/v1/rbac/roles/seed")
    assert rbac.status_code == 200, rbac.text
    response = await client.post("/api/v1/apps/seed")
    assert response.status_code == 200, response.text
    return response.json()


async def _get_exec_monitor_menus(client: Any) -> list[dict[str, Any]]:
    """Fetch the exec-monitor app's flat menu list via the API."""
    response = await client.get("/api/v1/apps")
    assert response.status_code == 200, response.text
    apps = response.json()["items"]
    target = next(a for a in apps if a["code"] == EXEC_MONITOR_APP_CODE)
    detail = await client.get(f"/api/v1/apps/{target['id']}")
    assert detail.status_code == 200, detail.text

    menus: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = list(detail.json()["menus"])
    while stack:
        node = stack.pop()
        stack.extend(node.pop("children"))
        menus.append(node)
    return menus


async def _list_app_codes(client: Any) -> list[str]:
    """Return the codes of apps visible to the current authenticated user."""
    response = await client.get("/api/v1/apps")
    assert response.status_code == 200, response.text
    return [a["code"] for a in response.json()["items"]]


def _find(menus: list[dict[str, Any]], code: str) -> dict[str, Any] | None:
    return next((m for m in menus if m["code"] == code), None)


class TestOperatorPanelSeed:
    """RH-5: operator panel menu entry seeded under exec-monitor."""

    async def test_seed_creates_operator_panel_entry(self, client: Any) -> None:
        """Seeding creates the 操作员面板 entry resolving to OperatorView."""
        await _seed(client)
        menus = await _get_exec_monitor_menus(client)
        entry = _find(menus, OPERATOR_PANEL_CODE)
        assert entry is not None, (
            f"{OPERATOR_PANEL_CODE} missing from {EXEC_MONITOR_APP_CODE} menus"
        )
        assert entry["name"] == "操作员面板"
        # Resolves to the OperatorView route (/operator/:station_id) with a
        # default station so the menu click lands on a real page.
        assert entry["route_path"] == "/operator/default"
        assert entry["route_name"] == "OperatorView"
        assert entry["is_active"] is True

    async def test_seed_idempotent_no_duplicate(self, client: Any) -> None:
        """Re-running the seed does not duplicate the operator panel row."""
        first = await _seed(client)
        second = await _seed(client)
        assert second["created_apps"] == 0
        assert second["created_menus"] == 0
        assert second["updated_menus"] == 0
        assert first["status"] == "ok"

        menus = await _get_exec_monitor_menus(client)
        duplicates = [m for m in menus if m["code"] == OPERATOR_PANEL_CODE]
        assert len(duplicates) == 1

    async def test_operator_panel_shape_matches_siblings(self, client: Any) -> None:
        """Entry carries exactly the same fields as sibling menu entries."""
        await _seed(client)
        menus = await _get_exec_monitor_menus(client)
        entry = _find(menus, OPERATOR_PANEL_CODE)
        sibling = _find(menus, SIBLING_CODE)
        assert entry is not None and sibling is not None
        assert set(entry.keys()) == set(sibling.keys())

        # Field-type parity with siblings.
        assert isinstance(entry["sort_order"], int)
        assert isinstance(entry["required_permissions"], list)
        assert all(isinstance(p, str) for p in entry["required_permissions"])
        assert isinstance(entry["icon"], str) and entry["icon"]
        # sort_order continues the sibling sequence (unique within the app).
        sort_orders = [m["sort_order"] for m in menus]
        assert len(sort_orders) == len(set(sort_orders))

    async def test_operator_panel_auth_matches_siblings(self, client: Any) -> None:
        """Permission filtering for the new entry mirrors its siblings.

        A user without ``exec:read`` sees neither the operator panel nor the
        dashboard sibling; a user holding ``exec:read`` sees both.
        """

        def _user_with(role: str, scopes: list[str]) -> User:
            return User(
                id="u1",
                username="u1",
                password_hash="",
                role=role,
                scopes=scopes,
                is_active=True,
                theme_mode="auto",
                language="en",
            )

        await _seed(client)

        # User lacking exec:read → operator panel hidden like the sibling.
        # (Custom unknown role + every other module's read scope: the other
        # apps stay visible while exec-monitor drops out entirely.)
        limited = _user_with(
            "custom-no-scopes", ["node:read", "flow:read", "system:read"]
        )

        async def _limited_user() -> User:
            return limited

        client.app.dependency_overrides[get_current_user] = _limited_user
        try:
            codes = await _list_app_codes(client)
            assert EXEC_MONITOR_APP_CODE not in codes
        finally:
            client.app.dependency_overrides.pop(get_current_user, None)

        # User holding exec:read → both visible.
        permitted = _user_with(
            "custom-no-scopes",
            ["node:read", "flow:read", "system:read", "exec:read"],
        )

        async def _permitted_user() -> User:
            return permitted

        client.app.dependency_overrides[get_current_user] = _permitted_user
        try:
            menus = await _get_exec_monitor_menus(client)
            assert _find(menus, OPERATOR_PANEL_CODE) is not None
            assert _find(menus, SIBLING_CODE) is not None
        finally:
            client.app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize(
    "required",
    [["exec:read"]],
    ids=["exec-read"],
)
def test_seed_data_declares_operator_panel(required: list[str]) -> None:
    """The seed source itself declares the operator panel entry.

    Directly inspects the ``default_apps`` structure so a regression in the
    seed data (not just the DB round-trip) fails loudly.
    """
    from ate_cloud.api.v1.apps import default_apps  # noqa: PLC0415

    exec_monitor = next(a for a in default_apps if a["code"] == EXEC_MONITOR_APP_CODE)
    entry = next(
        (m for m in exec_monitor["menus"] if m["code"] == OPERATOR_PANEL_CODE), None
    )
    assert entry is not None
    assert entry["required_permissions"] == required
    assert entry["route_name"] == "OperatorView"
