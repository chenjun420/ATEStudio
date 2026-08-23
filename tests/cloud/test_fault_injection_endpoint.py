"""Tests for POST /api/v1/executions/{run_id}/fault-injection (T44 v41-gap-analysis).

Verifies:
- Valid injection -> 200 {ok: true} + NATS control message (T5 inject_fault semantics)
- Published rule_cfg passes FaultInjector DSL validation (§7.7.2)
- Topology SSE fault event emitted with link_id + fault_type (§8.3.7 paint)
- 404 unknown run_id; 409 terminal state (no active execution)
- 422 invalid fault_type / missing link_id
- 401 anonymous request (router stays JWT-protected via _PROTECTED_ROUTERS)
- NATS outage is non-fatal (200, consistent with pause/resume/force_next)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

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


class TestFaultInjectionHappyPath:
    """Tests for successful fault injection."""

    @pytest.mark.asyncio
    async def test_inject_returns_200_ok(self, db_session, client) -> None:
        """Valid injection on a running execution returns 200 {ok: true}."""
        await _insert_execution(db_session, "run-fi-ok", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions/run-fi-ok/fault-injection",
                json={"link_id": "link-1", "fault_type": "open_circuit"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["run_id"] == "run-fi-ok"
        assert data["link_id"] == "link-1"
        assert data["fault_type"] == "open_circuit"

    @pytest.mark.asyncio
    async def test_publishes_inject_fault_control_message(self, db_session, client) -> None:
        """Injection forwards a T5-style inject_fault control message."""
        await _insert_execution(db_session, "run-fi-cmd", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            await client.post(
                "/api/v1/executions/run-fi-cmd/fault-injection",
                json={"link_id": "link-2", "fault_type": "short_circuit"},
            )

        mock_nc.publish.assert_awaited_once()
        subject = mock_nc.publish.call_args.args[0]
        assert subject == "ate.control.run-fi-cmd"

        payload = json.loads(mock_nc.publish.call_args.args[1])
        assert payload["action"] == "inject_fault"
        assert payload["run_id"] == "run-fi-cmd"
        rule = payload["rule"]
        assert rule["target"] == "link-2"
        assert rule["action"]["type"] == "short_circuit"
        assert rule.get("fault_id") or rule.get("id")

    @pytest.mark.asyncio
    async def test_published_rule_passes_fault_injector_validation(
        self, db_session, client,
    ) -> None:
        """The forwarded rule_cfg must load through FaultInjector.load (T5 path)."""
        from ate_platform.simulation.fault_injector import FaultInjector

        await _insert_execution(db_session, "run-fi-dsl", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions/run-fi-dsl/fault-injection",
                json={"link_id": "link-3", "fault_type": "contact_resistance"},
            )
        assert response.status_code == 200

        payload = json.loads(mock_nc.publish.call_args.args[1])
        injector = FaultInjector()
        injector.load([payload["rule"]])  # raises ValueError on invalid DSL
        assert injector.rules[-1].layer == "network"

    @pytest.mark.asyncio
    async def test_all_four_fault_types_accepted(self, db_session, client) -> None:
        """All §8.3 fault kinds are accepted (open_circuit|short_circuit|contact_resistance|noise)."""
        await _insert_execution(db_session, "run-fi-kinds", status="RUNNING")

        mock_nc = _make_mock_nc()
        for fault_type in (
            "open_circuit",
            "short_circuit",
            "contact_resistance",
            "noise",
        ):
            with patch("ate_cloud.main.get_nats", return_value=mock_nc):
                response = await client.post(
                    "/api/v1/executions/run-fi-kinds/fault-injection",
                    json={"link_id": "link-x", "fault_type": fault_type},
                )
            assert response.status_code == 200, fault_type
            assert response.json()["fault_type"] == fault_type

    @pytest.mark.asyncio
    async def test_publishes_topology_sse_fault_event(self, db_session, client) -> None:
        """Injection emits a topology-stream fault event carrying link_id+fault_type."""
        await _insert_execution(db_session, "run-fi-sse", status="RUNNING")

        # Subscribe to the isolated topology stream BEFORE injecting so the
        # published event lands in this client's queue.
        bridge = client.app.state.sse_bridge
        queue = bridge.get_stream_queue("run-fi-sse", "topology")

        mock_nc = _make_mock_nc()
        try:
            with patch("ate_cloud.main.get_nats", return_value=mock_nc):
                response = await client.post(
                    "/api/v1/executions/run-fi-sse/fault-injection",
                    json={"link_id": "link-9", "fault_type": "noise"},
                )
            assert response.status_code == 200

            event = queue.get_nowait()
            assert event["type"] == "fault"
            assert event["data"]["link_id"] == "link-9"
            assert event["data"]["fault_type"] == "noise"
        finally:
            bridge.remove_stream_queue("run-fi-sse", "topology")


class TestFaultInjectionErrors:
    """Error-path tests for the fault-injection endpoint."""

    @pytest.mark.asyncio
    async def test_unknown_run_404(self, db_session, client) -> None:
        """Unknown run_id returns 404."""
        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions/nonexistent/fault-injection",
                json={"link_id": "link-1", "fault_type": "open_circuit"},
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal_status", ["COMPLETED", "FAILED", "ABORTED"])
    async def test_terminal_state_409(self, db_session, client, terminal_status: str) -> None:
        """Terminal execution (no active execution) returns 409."""
        run_id = f"run-fi-terminal-{terminal_status}"
        await _insert_execution(db_session, run_id, status=terminal_status)

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                f"/api/v1/executions/{run_id}/fault-injection",
                json={"link_id": "link-1", "fault_type": "open_circuit"},
            )

        assert response.status_code == 409
        assert "active" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_invalid_fault_type_422(self, db_session, client) -> None:
        """fault_type outside the §8.3 set returns 422."""
        await _insert_execution(db_session, "run-fi-bad-type", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions/run-fi-bad-type/fault-injection",
                json={"link_id": "link-1", "fault_type": "meteor_strike"},
            )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_link_id_422(self, db_session, client) -> None:
        """Missing link_id in body returns 422 (structured FastAPI error)."""
        await _insert_execution(db_session, "run-fi-no-link", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions/run-fi-no-link/fault-injection",
                json={"fault_type": "open_circuit"},
            )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_malformed_body_422(self, db_session, client) -> None:
        """Non-object body returns structured 422 error."""
        await _insert_execution(db_session, "run-fi-malformed", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions/run-fi-malformed/fault-injection",
                json="not-an-object",
            )

        assert response.status_code == 422
        assert response.json()["detail"]

    @pytest.mark.asyncio
    async def test_without_nats_still_returns_200(self, db_session, client) -> None:
        """NATS outage is non-fatal — API still returns 200 (worker-independent)."""
        await _insert_execution(db_session, "run-fi-no-nats", status="RUNNING")

        with patch("ate_cloud.main.get_nats", side_effect=RuntimeError("no nats")):
            response = await client.post(
                "/api/v1/executions/run-fi-no-nats/fault-injection",
                json={"link_id": "link-1", "fault_type": "open_circuit"},
            )

        assert response.status_code == 200
        assert response.json()["ok"] is True


class TestFaultInjectionAuth:
    """Auth-enforcement regression guard for the new endpoint."""

    @pytest.mark.asyncio
    async def test_anonymous_401(
        self, db_session, client, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Anonymous request (dev_mode off) must be rejected with 401."""
        monkeypatch.setattr(settings, "dev_mode", False)

        response = await client.post(
            "/api/v1/executions/some-run/fault-injection",
            json={"link_id": "link-1", "fault_type": "open_circuit"},
        )

        assert response.status_code == 401
