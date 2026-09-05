"""Persisted-breakpoint CRUD + enable/disable API tests (task 19, T39 §8.4).

The typed simulation breakpoints under ``/api/v1/executions/{run_id}/breakpoints``
are durable: they persist to the ``breakpoints`` table (surviving a new DB
session / restart) and are the source of truth. The in-memory registry stays a
live-hit cache. These tests pin:

- create → persisted to DB and visible via list/get;
- list returns persisted rows across requests (a fresh session still sees them);
- PUT /disable (and the enable/disable action) flip ``enabled``; a disabled
  breakpoint does NOT fire in the live registry;
- toggling / getting / updating a non-existent id → 404;
- delete removes the durable row (idempotent: second delete → removed=false);
- anonymous request → 401 when dev_mode is off (mount-level JWT).

Given/When/Then throughout; hermetic (in-memory SQLite, no external services).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ate_cloud.config import settings
from ate_cloud.models.breakpoint import Breakpoint
from ate_cloud.models.execution import Execution


async def _insert_execution(
    db_session: AsyncSession,
    run_id: str,
    status: str = "RUNNING",
) -> Execution:
    """Insert an Execution row a breakpoint can be armed on."""
    execution = Execution(id=run_id, sequence_id="seq-bp", status=status)
    db_session.add(execution)
    await db_session.flush()
    return execution


def _registry_for(client: Any) -> Any:
    """Fetch the in-memory breakpoint registry from app.state."""
    from ate_cloud.services.breakpoint_registry import BreakpointRegistry

    reg = getattr(client.app.state, "breakpoint_registry", None)
    if reg is None:
        reg = BreakpointRegistry()
        client.app.state.breakpoint_registry = reg
    return reg


class TestPersistedBreakpointCrud:
    """Durable CRUD behavior for typed breakpoints."""

    @pytest.mark.asyncio
    async def test_create_persists_to_db(self, db_session, client) -> None:
        """Given a running execution, When a breakpoint is created, Then a row
        with kind/target/session_id(run_id) is persisted to the breakpoints table."""
        await _insert_execution(db_session, "run-persist", status="RUNNING")

        resp = await client.post(
            "/api/v1/executions/run-persist/breakpoints",
            json={"kind": "step", "target": "step_a"},
        )

        assert resp.status_code == 200
        bp_id = resp.json()["id"]
        result = await db_session.execute(
            select(Breakpoint).where(Breakpoint.id == bp_id)
        )
        row = result.scalar_one()
        assert row.session_id == "run-persist"
        assert row.kind == "step"
        assert row.target == "step_a"
        assert row.enabled is True

    @pytest.mark.asyncio
    async def test_list_returns_persisted_rows_across_sessions(
        self, db_session, test_engine, client,
    ) -> None:
        """Given a persisted breakpoint, When listed through a brand-new DB
        session (simulating a restart), Then the row is still returned."""
        await _insert_execution(db_session, "run-survive", status="RUNNING")
        created = (
            await client.post(
                "/api/v1/executions/run-survive/breakpoints",
                json={"kind": "instrument_call", "target": "PSU.set_voltage"},
            )
        ).json()

        # New session factory over the SAME in-memory engine — the committed
        # row must be visible without any in-memory registry state.
        fresh_factory = async_sessionmaker(test_engine, class_=AsyncSession)
        async with fresh_factory() as fresh:
            result = await fresh.execute(
                select(Breakpoint).where(Breakpoint.session_id == "run-survive")
            )
            rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].id == created["id"]

        # The list endpoint reads the durable store, not the live registry.
        resp = await client.get("/api/v1/executions/run-survive/breakpoints")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert resp.json()["total"] == 1
        assert items[0]["id"] == created["id"]
        assert items[0]["kind"] == "instrument_call"
        assert items[0]["target"] == "PSU.set_voltage"

    @pytest.mark.asyncio
    async def test_get_single_breakpoint(self, db_session, client) -> None:
        """Given a persisted breakpoint, When fetched by id, Then it is returned."""
        await _insert_execution(db_session, "run-get", status="RUNNING")
        created = (
            await client.post(
                "/api/v1/executions/run-get/breakpoints",
                json={"kind": "variable_change", "target": "bench.voltage"},
            )
        ).json()

        resp = await client.get(
            f"/api/v1/executions/run-get/breakpoints/{created['id']}"
        )

        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]
        assert resp.json()["kind"] == "variable_change"

    @pytest.mark.asyncio
    async def test_get_unknown_id_404(self, db_session, client) -> None:
        """Given no such breakpoint, When fetched by id, Then 404."""
        await _insert_execution(db_session, "run-get404", status="RUNNING")

        resp = await client.get(
            "/api/v1/executions/run-get404/breakpoints/does-not-exist"
        )

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_put_update_enabled_and_condition(self, db_session, client) -> None:
        """Given a condition breakpoint, When PUT updates condition + enabled,
        Then the persisted row reflects both and the response echoes them."""
        await _insert_execution(db_session, "run-put", status="RUNNING")
        created = (
            await client.post(
                "/api/v1/executions/run-put/breakpoints",
                json={"kind": "condition", "target": "*", "condition": "v > 1"},
            )
        ).json()

        resp = await client.put(
            f"/api/v1/executions/run-put/breakpoints/{created['id']}",
            json={"condition": "v > 5", "enabled": False},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["condition"] == "v > 5"
        assert body["enabled"] is False

        result = await db_session.execute(
            select(Breakpoint).where(Breakpoint.id == created["id"])
        )
        row = result.scalar_one()
        assert row.condition == "v > 5"
        assert row.enabled is False

    @pytest.mark.asyncio
    async def test_put_unknown_id_404(self, db_session, client) -> None:
        """Given no such breakpoint, When PUT toggling it, Then 404."""
        await _insert_execution(db_session, "run-put404", status="RUNNING")

        resp = await client.put(
            "/api/v1/executions/run-put404/breakpoints/missing",
            json={"enabled": False},
        )

        assert resp.status_code == 404


class TestBreakpointEnableDisable:
    """The enable/disable action and its effect on live hit detection."""

    @pytest.mark.asyncio
    async def test_disable_action_persists_and_prevents_firing(
        self, db_session, client,
    ) -> None:
        """Given an armed step breakpoint, When it is disabled via the action,
        Then the row is enabled=False in the DB and the live registry does not
        fire on a matching event; re-enabling arms it again."""
        from ate_cloud.nats.sse_bridge import SSEBridge
        from ate_cloud.services.breakpoint_registry import handle_status_event

        await _insert_execution(db_session, "run-toggle", status="RUNNING")
        created = (
            await client.post(
                "/api/v1/executions/run-toggle/breakpoints",
                json={"kind": "step", "target": "dmm_read"},
            )
        ).json()
        bp_id = created["id"]

        # When: disable through the dedicated action.
        disabled = await client.post(
            f"/api/v1/executions/run-toggle/breakpoints/{bp_id}/disable"
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False

        # Then: persisted state is disabled.
        result = await db_session.execute(
            select(Breakpoint).where(Breakpoint.id == bp_id)
        )
        assert result.scalar_one().enabled is False

        # And: a matching live event does NOT fire (registry was updated).
        bridge = SSEBridge(nc=None)
        queue = bridge.get_or_create_queue("run-toggle")
        try:
            await handle_status_event(
                _registry_for(client), bridge, _make_mock_nc(),
                {"type": "STEP_STARTED", "run_id": "run-toggle", "step_id": "dmm_read"},
            )
            assert all(e["type"] != "BREAKPOINT_HIT" for e in _drain(queue))
        finally:
            bridge.remove_queue("run-toggle")

        # When: re-enable.
        enabled = await client.post(
            f"/api/v1/executions/run-toggle/breakpoints/{bp_id}/enable"
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True

    @pytest.mark.asyncio
    async def test_toggle_unknown_id_404(self, db_session, client) -> None:
        """Given no such breakpoint, When enabling/disabling it, Then 404."""
        await _insert_execution(db_session, "run-toggle404", status="RUNNING")

        resp = await client.post(
            "/api/v1/executions/run-toggle404/breakpoints/missing/disable"
        )

        assert resp.status_code == 404


class TestBreakpointDelete:
    """Deletion removes the durable row."""

    @pytest.mark.asyncio
    async def test_delete_removes_persisted_row(self, db_session, client) -> None:
        """Given a persisted breakpoint, When deleted, Then the row is gone from
        the DB and a follow-up GET returns 404; a second delete is idempotent."""
        await _insert_execution(db_session, "run-del", status="RUNNING")
        created = (
            await client.post(
                "/api/v1/executions/run-del/breakpoints",
                json={"kind": "step", "target": "step_x"},
            )
        ).json()
        bp_id = created["id"]

        first = await client.delete(
            f"/api/v1/executions/run-del/breakpoints/{bp_id}"
        )
        assert first.status_code == 200
        assert first.json()["removed"] is True

        result = await db_session.execute(
            select(Breakpoint).where(Breakpoint.id == bp_id)
        )
        assert result.scalar_one_or_none() is None

        assert (
            await client.get(f"/api/v1/executions/run-del/breakpoints/{bp_id}")
        ).status_code == 404

        second = await client.delete(
            f"/api/v1/executions/run-del/breakpoints/{bp_id}"
        )
        assert second.status_code == 200
        assert second.json()["removed"] is False


class TestBreakpointAuth:
    """Mount-level JWT enforcement."""

    @pytest.mark.asyncio
    async def test_anonymous_401_when_dev_mode_off(
        self, db_session, client, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Given dev_mode off (auth enforced), When an anonymous client lists
        breakpoints, Then the request is rejected with 401."""
        monkeypatch.setattr(settings, "dev_mode", False)

        resp = await client.get("/api/v1/executions/any-run/breakpoints")

        assert resp.status_code == 401


def _drain(queue: Any) -> list[dict[str, Any]]:
    """Drain an asyncio-backed SSE queue into a list of events."""
    events: list[dict[str, Any]] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def _make_mock_nc() -> Any:
    """Build a mock NATS client with an async publish."""
    from unittest.mock import AsyncMock, MagicMock

    mock_nc = MagicMock()
    mock_nc.publish = AsyncMock()
    return mock_nc
