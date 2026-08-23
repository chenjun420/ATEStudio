"""Tests for POST /api/v1/executions/{run_id}/manual-fault (T38 v41-gap-analysis).

Manual fault injection panel backend: accepts {scope, target_id, fault_type,
params?} and forwards a T5-style ``inject_fault`` control message to the
worker with the layer mapped per scope:

    link -> network | instrument -> instrument | step -> scheduler
    scheduler -> scheduler | protocol -> protocol

Verifies:
- Each scope maps to its §7.7.1 layer in the published rule_cfg
- Published rule passes FaultInjector DSL validation for every scope
- Topology SSE fault event emitted with target_id + fault_type
- 404 unknown run_id; 409 terminal state; 422 invalid scope / fault-type
  not allowed for scope / missing target_id
- 401 anonymous request (router stays JWT-protected)
- NATS outage is non-fatal (200, consistent with T44 link-fault endpoint)
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


async def _post(client, run_id: str, body: dict):
    """POST the manual-fault endpoint with get_nats patched."""
    mock_nc = _make_mock_nc()
    with patch("ate_cloud.main.get_nats", return_value=mock_nc):
        response = await client.post(
            f"/api/v1/executions/{run_id}/manual-fault", json=body
        )
    return response, mock_nc


class TestManualFaultScopeMapping:
    """Each scope must map to its §7.7.1 injection layer."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("scope", "target", "fault_type", "expected_layer"),
        [
            ("link", "link-1", "open_circuit", "network"),
            ("instrument", "dmm-1", "measurement_out_of_range", "instrument"),
            ("step", "step-read-voltage", "timeout", "scheduler"),
            ("scheduler", "*", "resource_deadlock", "scheduler"),
            ("protocol", "dmm-1", "scpi_error", "protocol"),
        ],
    )
    async def test_scope_maps_to_layer(
        self,
        db_session,
        client,
        scope: str,
        target: str,
        fault_type: str,
        expected_layer: str,
    ) -> None:
        """Published rule_cfg carries the layer mapped from scope."""
        await _insert_execution(db_session, f"run-mf-{scope}", status="RUNNING")

        response, mock_nc = await _post(
            client,
            f"run-mf-{scope}",
            {"scope": scope, "target_id": target, "fault_type": fault_type},
        )

        assert response.status_code == 200, scope
        data = response.json()
        assert data["ok"] is True
        assert data["layer"] == expected_layer

        payload = json.loads(mock_nc.publish.call_args.args[1])
        assert payload["action"] == "inject_fault"
        assert payload["rule"]["layer"] == expected_layer
        assert payload["rule"]["target"] == target
        assert payload["rule"]["action"]["type"] == fault_type

    @pytest.mark.asyncio
    async def test_all_scopes_pass_fault_injector_validation(
        self, db_session, client,
    ) -> None:
        """Every scope's forwarded rule loads through FaultInjector.load (T5 path)."""
        from ate_platform.simulation.fault_injector import FaultInjector

        cases = [
            ("link", "link-9", "short_circuit"),
            ("instrument", "psu-1", "over_current"),
            ("step", "step-42", "force_fail"),
            ("scheduler", "*", "resource_deadlock"),
            ("protocol", "gpib-1", "truncated_data"),
        ]
        for i, (scope, target, fault_type) in enumerate(cases):
            run_id = f"run-mf-dsl-{i}"
            await _insert_execution(db_session, run_id, status="RUNNING")
            response, mock_nc = await _post(
                client,
                run_id,
                {"scope": scope, "target_id": target, "fault_type": fault_type},
            )
            assert response.status_code == 200, scope

            injector = FaultInjector()
            injector.load([json.loads(mock_nc.publish.call_args.args[1])["rule"]])
            assert injector.rules[-1].layer  # loaded fine

    @pytest.mark.asyncio
    async def test_params_forwarded_into_action(self, db_session, client) -> None:
        """params merge into rule.action (e.g. value_override value)."""
        await _insert_execution(db_session, "run-mf-params", status="RUNNING")

        response, mock_nc = await _post(
            client,
            "run-mf-params",
            {
                "scope": "instrument",
                "target_id": "dmm-1",
                "fault_type": "value_override",
                "params": {"value": 4.2},
            },
        )

        assert response.status_code == 200
        payload = json.loads(mock_nc.publish.call_args.args[1])
        assert payload["rule"]["action"]["type"] == "value_override"
        assert payload["rule"]["action"]["value"] == 4.2

    @pytest.mark.asyncio
    async def test_publishes_topology_sse_fault_event(
        self, db_session, client,
    ) -> None:
        """Injection emits a topology-stream fault event carrying target+type."""
        await _insert_execution(db_session, "run-mf-sse", status="RUNNING")

        bridge = client.app.state.sse_bridge
        queue = bridge.get_stream_queue("run-mf-sse", "topology")
        try:
            response, _ = await _post(
                client,
                "run-mf-sse",
                {
                    "scope": "instrument",
                    "target_id": "dmm-7",
                    "fault_type": "noise",
                },
            )
            assert response.status_code == 200

            event = queue.get_nowait()
            assert event["type"] == "fault"
            assert event["data"]["target_id"] == "dmm-7"
            assert event["data"]["fault_type"] == "noise"
            assert event["data"]["scope"] == "instrument"
        finally:
            bridge.remove_stream_queue("run-mf-sse", "topology")


class TestManualFaultErrors:
    """Error-path tests for the manual-fault endpoint."""

    @pytest.mark.asyncio
    async def test_unknown_run_404(self, db_session, client) -> None:
        """Unknown run_id returns 404."""
        response, _ = await _post(
            client,
            "nonexistent-manual-fault",
            {"scope": "link", "target_id": "link-1", "fault_type": "open_circuit"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal_status", ["COMPLETED", "FAILED", "ABORTED"])
    async def test_terminal_state_409(
        self, db_session, client, terminal_status: str,
    ) -> None:
        """Terminal execution (no active execution) returns 409."""
        run_id = f"run-mf-terminal-{terminal_status}"
        await _insert_execution(db_session, run_id, status=terminal_status)

        response, _ = await _post(
            client,
            run_id,
            {"scope": "link", "target_id": "link-1", "fault_type": "open_circuit"},
        )
        assert response.status_code == 409
        assert "active" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_invalid_scope_422(self, db_session, client) -> None:
        """scope outside the allowed set returns 422."""
        await _insert_execution(db_session, "run-mf-bad-scope", status="RUNNING")
        response, _ = await _post(
            client,
            "run-mf-bad-scope",
            {"scope": "galaxy", "target_id": "x", "fault_type": "open_circuit"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_fault_type_not_allowed_for_scope_422(
        self, db_session, client,
    ) -> None:
        """A valid scope with a foreign fault_type returns 422."""
        await _insert_execution(db_session, "run-mf-cross", status="RUNNING")
        # scpi_error belongs to protocol, not link.
        response, _ = await _post(
            client,
            "run-mf-cross",
            {"scope": "link", "target_id": "link-1", "fault_type": "scpi_error"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_target_id_422(self, db_session, client) -> None:
        """Missing target_id returns 422."""
        await _insert_execution(db_session, "run-mf-no-target", status="RUNNING")
        response, _ = await _post(
            client,
            "run-mf-no-target",
            {"scope": "link", "fault_type": "open_circuit"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_without_nats_still_returns_200(self, db_session, client) -> None:
        """NATS outage is non-fatal — API still returns 200."""
        await _insert_execution(db_session, "run-mf-no-nats", status="RUNNING")
        with patch("ate_cloud.main.get_nats", side_effect=RuntimeError("no nats")):
            response = await client.post(
                "/api/v1/executions/run-mf-no-nats/manual-fault",
                json={"scope": "step", "target_id": "s1", "fault_type": "timeout"},
            )
        assert response.status_code == 200
        assert response.json()["ok"] is True


class TestManualFaultAuth:
    """Auth-enforcement regression guard for the new endpoint."""

    @pytest.mark.asyncio
    async def test_anonymous_401(
        self, db_session, client, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Anonymous request (dev_mode off) must be rejected with 401."""
        monkeypatch.setattr(settings, "dev_mode", False)

        response = await client.post(
            "/api/v1/executions/some-run/manual-fault",
            json={"scope": "link", "target_id": "link-1", "fault_type": "open_circuit"},
        )
        assert response.status_code == 401
