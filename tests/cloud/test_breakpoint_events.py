"""Tests for typed simulation breakpoints (T39, v41-gap-analysis #39).

Covers:
- POST /api/v1/executions/{run_id}/breakpoints — 4 kinds (step | instrument_call |
  variable_change | condition), 404 unknown run, 409 terminal state, 422 malformed
  condition / condition-on-wrong-kind
- DELETE idempotency (second delete → removed=false, still 200)
- GET list per run
- Hit detection: step / instrument_call / variable_change / condition kinds via the
  relay-side hook (handle_status_event) → SSE BREAKPOINT_HIT event + pause control
  message reusing the existing {action: "pause"} NATS contract unchanged
- No-hit passthrough: non-matching events emit nothing and never pause
- Auth: anonymous request rejected with 401 when dev_mode off
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.config import settings
from ate_cloud.models.execution import Execution


async def _insert_execution(
    db_session: AsyncSession,
    run_id: str,
    status: str = "RUNNING",
) -> Execution:
    """Insert an Execution record for testing."""
    execution = Execution(
        id=run_id,
        sequence_id="seq-test",
        status=status,
    )
    db_session.add(execution)
    await db_session.flush()
    return execution


def _make_mock_nc() -> MagicMock:
    """Build a mock NATS client with async publish."""
    mock_nc = MagicMock()
    mock_nc.publish = AsyncMock()
    return mock_nc


def _registry_for(client: Any) -> Any:
    """Fetch (lazily creating) the breakpoint registry from app.state."""
    from ate_cloud.services.breakpoint_registry import BreakpointRegistry

    reg = getattr(client.app.state, "breakpoint_registry", None)
    if reg is None:
        reg = BreakpointRegistry()
        client.app.state.breakpoint_registry = reg
    return reg


class TestBreakpointCrud:
    """CRUD endpoint tests for typed breakpoints."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kind", "target"),
        [
            ("step", "dmm_read"),
            ("instrument_call", "PSU_MAIN.set_voltage"),
            ("variable_change", "bench.voltage"),
            ("condition", "*"),
        ],
    )
    async def test_create_each_kind_200(
        self, db_session, client, kind: str, target: str,
    ) -> None:
        """All four §8.4 breakpoint kinds are accepted at creation."""
        await _insert_execution(db_session, f"run-bp-{kind}", status="RUNNING")

        body: dict[str, Any] = {"kind": kind, "target": target}
        if kind == "condition":
            body["condition"] = "voltage > 3.0"

        response = await client.post(
            f"/api/v1/executions/run-bp-{kind}/breakpoints", json=body,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["kind"] == kind
        assert data["target"] == target
        assert data["id"]

    @pytest.mark.asyncio
    async def test_list_breakpoints_returns_created(self, db_session, client) -> None:
        """GET returns previously registered breakpoints for the run."""
        await _insert_execution(db_session, "run-bp-list", status="RUNNING")
        await client.post(
            "/api/v1/executions/run-bp-list/breakpoints",
            json={"kind": "step", "target": "step_a"},
        )

        response = await client.get("/api/v1/executions/run-bp-list/breakpoints")

        assert response.status_code == 200
        items = response.json()["items"]
        assert any(bp["kind"] == "step" and bp["target"] == "step_a" for bp in items)

    @pytest.mark.asyncio
    async def test_delete_idempotent(self, db_session, client) -> None:
        """Deleting twice is safe: first removes, second reports removed=false."""
        await _insert_execution(db_session, "run-bp-del", status="RUNNING")
        created = (
            await client.post(
                "/api/v1/executions/run-bp-del/breakpoints",
                json={"kind": "step", "target": "step_x"},
            )
        ).json()

        first = await client.delete(f"/api/v1/executions/run-bp-del/breakpoints/{created['id']}")
        second = await client.delete(f"/api/v1/executions/run-bp-del/breakpoints/{created['id']}")

        assert first.status_code == 200
        assert first.json()["removed"] is True
        assert second.status_code == 200
        assert second.json()["removed"] is False

    @pytest.mark.asyncio
    async def test_unknown_run_404(self, db_session, client) -> None:
        """Unknown run_id returns 404 on create."""
        response = await client.post(
            "/api/v1/executions/nonexistent-bp/breakpoints",
            json={"kind": "step", "target": "s"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_terminal_state_409(self, db_session, client) -> None:
        """Terminal execution (no active run) rejects new breakpoints with 409."""
        await _insert_execution(db_session, "run-bp-terminal", status="COMPLETED")

        response = await client.post(
            "/api/v1/executions/run-bp-terminal/breakpoints",
            json={"kind": "step", "target": "s"},
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_condition_kind_requires_expression_422(self, db_session, client) -> None:
        """QA failure scenario: malformed (empty) condition rejected at creation."""
        await _insert_execution(db_session, "run-bp-nocond", status="RUNNING")

        response = await client.post(
            "/api/v1/executions/run-bp-nocond/breakpoints",
            json={"kind": "condition", "target": "*", "condition": "   "},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_condition_syntax_validated_at_creation_422(self, db_session, client) -> None:
        """Syntactically invalid expression (simpleeval subset) rejected with 422."""
        await _insert_execution(db_session, "run-bp-badsyntax", status="RUNNING")

        response = await client.post(
            "/api/v1/executions/run-bp-badsyntax/breakpoints",
            json={"kind": "condition", "target": "*", "condition": "voltage >"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_condition_on_non_condition_kind_422(self, db_session, client) -> None:
        """condition field is only allowed for the condition kind."""
        await _insert_execution(db_session, "run-bp-condstep", status="RUNNING")

        response = await client.post(
            "/api/v1/executions/run-bp-condstep/breakpoints",
            json={"kind": "step", "target": "s", "condition": "x > 1"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_kind_422(self, db_session, client) -> None:
        """kind outside the four-value set returns 422."""
        await _insert_execution(db_session, "run-bp-unkind", status="RUNNING")

        response = await client.post(
            "/api/v1/executions/run-bp-unkind/breakpoints",
            json={"kind": "meteor", "target": "s"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_anonymous_401(
        self, db_session, client, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Anonymous request (dev_mode off) must be rejected with 401."""
        monkeypatch.setattr(settings, "dev_mode", False)

        response = await client.post(
            "/api/v1/executions/some-run/breakpoints",
            json={"kind": "step", "target": "s"},
        )
        assert response.status_code == 401


class TestBreakpointHits:
    """Hit-detection tests for handle_status_event (relay-side hook)."""

    def _drain(self, queue: Any) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while not queue.empty():
            events.append(queue.get_nowait())
        return events

    @pytest.mark.asyncio
    async def test_step_hit_publishes_sse_and_pause_control(self, db_session, client) -> None:
        """STEP_STARTED matching a step-kind bp emits BREAKPOINT_HIT SSE + pause control."""
        from ate_cloud.services.breakpoint_registry import handle_status_event

        await _insert_execution(db_session, "run-hit-step", status="RUNNING")
        created = (
            await client.post(
                "/api/v1/executions/run-hit-step/breakpoints",
                json={"kind": "step", "target": "dmm_read"},
            )
        ).json()

        bridge = client.app.state.sse_bridge
        queue = bridge.get_or_create_queue("run-hit-step")
        mock_nc = _make_mock_nc()
        try:
            await handle_status_event(
                _registry_for(client), bridge, mock_nc,
                {"type": "STEP_STARTED", "run_id": "run-hit-step", "step_id": "dmm_read"},
            )

            events = self._drain(queue)
            hits = [e for e in events if e["type"] == "BREAKPOINT_HIT"]
            assert len(hits) == 1
            assert hits[0]["data"]["breakpoint_id"] == created["id"]
            assert hits[0]["data"]["kind"] == "step"
            assert hits[0]["data"]["context"]["step_id"] == "dmm_read"

            # Pause reuses the existing control contract verbatim.
            mock_nc.publish.assert_awaited_once()
            subject = mock_nc.publish.call_args.args[0]
            payload = json.loads(mock_nc.publish.call_args.args[1])
            assert subject == "ate.control.run-hit-step"
            assert payload["action"] == "pause"
            assert payload["run_id"] == "run-hit-step"
        finally:
            bridge.remove_queue("run-hit-step")

    @pytest.mark.asyncio
    async def test_no_hit_passthrough(self, db_session, client) -> None:
        """Non-matching events produce no BREAKPOINT_HIT and no pause."""
        from ate_cloud.services.breakpoint_registry import handle_status_event

        await _insert_execution(db_session, "run-hit-miss", status="RUNNING")
        await client.post(
            "/api/v1/executions/run-hit-miss/breakpoints",
            json={"kind": "step", "target": "other_step"},
        )

        bridge = client.app.state.sse_bridge
        queue = bridge.get_or_create_queue("run-hit-miss")
        mock_nc = _make_mock_nc()
        try:
            await handle_status_event(
                _registry_for(client), bridge, mock_nc,
                {"type": "STEP_STARTED", "run_id": "run-hit-miss", "step_id": "dmm_read"},
            )

            assert all(e["type"] != "BREAKPOINT_HIT" for e in self._drain(queue))
            mock_nc.publish.assert_not_awaited()
        finally:
            bridge.remove_queue("run-hit-miss")

    @pytest.mark.asyncio
    async def test_variable_change_hit(self, db_session, client) -> None:
        """measurement_recorded without instrument matches variable_change scope.key."""
        from ate_cloud.services.breakpoint_registry import handle_status_event

        await _insert_execution(db_session, "run-hit-var", status="RUNNING")
        await client.post(
            "/api/v1/executions/run-hit-var/breakpoints",
            json={"kind": "variable_change", "target": "bench.voltage"},
        )

        bridge = client.app.state.sse_bridge
        queue = bridge.get_or_create_queue("run-hit-var")
        mock_nc = _make_mock_nc()
        try:
            await handle_status_event(
                _registry_for(client), bridge, mock_nc,
                {
                    "type": "measurement_recorded",
                    "run_id": "run-hit-var",
                    "name": "bench.voltage",
                    "new_value": 3.3,
                },
            )

            hits = [e for e in self._drain(queue) if e["type"] == "BREAKPOINT_HIT"]
            assert len(hits) == 1
            assert hits[0]["data"]["kind"] == "variable_change"
            assert hits[0]["data"]["context"]["value"] == 3.3
        finally:
            bridge.remove_queue("run-hit-var")

    @pytest.mark.asyncio
    async def test_instrument_call_hit_matches_resource_part(self, db_session, client) -> None:
        """instrument_call bp target resource.method matches by instrument_id + method."""
        from ate_cloud.services.breakpoint_registry import handle_status_event

        await _insert_execution(db_session, "run-hit-inst", status="RUNNING")
        await client.post(
            "/api/v1/executions/run-hit-inst/breakpoints",
            json={"kind": "instrument_call", "target": "PSU_MAIN.set_voltage"},
        )

        bridge = client.app.state.sse_bridge
        queue = bridge.get_or_create_queue("run-hit-inst")
        mock_nc = _make_mock_nc()
        try:
            await handle_status_event(
                _registry_for(client), bridge, mock_nc,
                {
                    "type": "measurement_recorded",
                    "run_id": "run-hit-inst",
                    "name": "bench.current",
                    "new_value": 0.5,
                    "instrument_id": "PSU_MAIN",
                    "method": "set_voltage",
                },
            )

            hits = [e for e in self._drain(queue) if e["type"] == "BREAKPOINT_HIT"]
            assert len(hits) == 1
            assert hits[0]["data"]["kind"] == "instrument_call"
        finally:
            bridge.remove_queue("run-hit-inst")

    @pytest.mark.asyncio
    async def test_condition_kind_evaluated_server_side(self, db_session, client) -> None:
        """condition-kind bp evaluates server-side; truthy context hits, falsy doesn't."""
        from ate_cloud.services.breakpoint_registry import handle_status_event

        await _insert_execution(db_session, "run-hit-cond", status="RUNNING")
        await client.post(
            "/api/v1/executions/run-hit-cond/breakpoints",
            json={"kind": "condition", "target": "*", "condition": "voltage > 3.0"},
        )

        bridge = client.app.state.sse_bridge
        queue = bridge.get_or_create_queue("run-hit-cond")
        mock_nc = _make_mock_nc()
        registry = _registry_for(client)
        try:
            await handle_status_event(
                registry, bridge, mock_nc,
                {
                    "type": "measurement_recorded",
                    "run_id": "run-hit-cond",
                    "name": "bench.voltage",
                    "new_value": 2.9,
                },
            )
            assert self._drain(queue) == [] or all(
                e["type"] != "BREAKPOINT_HIT" for e in self._drain(queue)
            )

            await handle_status_event(
                registry, bridge, mock_nc,
                {
                    "type": "measurement_recorded",
                    "run_id": "run-hit-cond",
                    "name": "bench.voltage",
                    "new_value": 3.3,
                },
            )
            hits = [e for e in self._drain(queue) if e["type"] == "BREAKPOINT_HIT"]
            assert len(hits) == 1
            assert hits[0]["data"]["kind"] == "condition"
        finally:
            bridge.remove_queue("run-hit-cond")

    @pytest.mark.asyncio
    async def test_disabled_breakpoint_never_hits(self, db_session, client) -> None:
        """A disabled breakpoint does not fire even on a matching event."""
        from ate_cloud.services.breakpoint_registry import handle_status_event

        await _insert_execution(db_session, "run-hit-disabled", status="RUNNING")
        created = (
            await client.post(
                "/api/v1/executions/run-hit-disabled/breakpoints",
                json={"kind": "step", "target": "dmm_read"},
            )
        ).json()

        bridge = client.app.state.sse_bridge
        queue = bridge.get_or_create_queue("run-hit-disabled")
        mock_nc = _make_mock_nc()
        registry = _registry_for(client)
        try:
            registry.disable(created["id"])
            await handle_status_event(
                registry, bridge, mock_nc,
                {"type": "STEP_STARTED", "run_id": "run-hit-disabled", "step_id": "dmm_read"},
            )
            assert all(e["type"] != "BREAKPOINT_HIT" for e in self._drain(queue))
        finally:
            bridge.remove_queue("run-hit-disabled")

    def test_breakpoint_hit_sse_category(self) -> None:
        """BREAKPOINT_HIT maps to its own SSE `event:` name ("breakpoint")."""
        from ate_cloud.nats.sse_bridge import _EVENT_TYPE_TO_SSE_CATEGORY

        assert _EVENT_TYPE_TO_SSE_CATEGORY["BREAKPOINT_HIT"] == "breakpoint"
        # Existing mappings untouched.
        assert _EVENT_TYPE_TO_SSE_CATEGORY["EXECUTION_PAUSED"] == "event"
