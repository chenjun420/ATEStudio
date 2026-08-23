"""Tests for POST /api/v1/executions/{run_id}/step-control (T40 v41-gap-analysis).

Verifies:
- Valid step mode -> 200 {ok: true} + NATS control message (step_control semantics)
- run_to_cursor forwards target_step_id
- 404 unknown run_id; 409 terminal state (no active execution)
- 422 invalid mode / run_to_cursor without target_step_id
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


class TestStepControlHappyPath:
    """Tests for successful step-control forwarding."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["over", "into", "out"])
    async def test_targetless_modes_return_200_and_forward(
        self, db_session, client, mode: str,
    ) -> None:
        """over/into/out are accepted and forwarded as step_control actions."""
        await _insert_execution(db_session, f"run-sc-{mode}", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                f"/api/v1/executions/run-sc-{mode}/step-control",
                json={"mode": mode},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["mode"] == mode
        assert data["target_step_id"] is None

        mock_nc.publish.assert_awaited_once()
        subject = mock_nc.publish.call_args.args[0]
        assert subject == f"ate.control.run-sc-{mode}"
        payload = json.loads(mock_nc.publish.call_args.args[1])
        assert payload["action"] == "step_control"
        assert payload["run_id"] == f"run-sc-{mode}"
        assert payload["mode"] == mode

    @pytest.mark.asyncio
    async def test_run_to_cursor_forwards_target(self, db_session, client) -> None:
        """run_to_cursor requires + forwards the target step id."""
        await _insert_execution(db_session, "run-sc-r2c", status="PAUSING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions/run-sc-r2c/step-control",
                json={"mode": "run_to_cursor", "target_step_id": "step-42"},
            )

        assert response.status_code == 200
        assert response.json()["target_step_id"] == "step-42"

        payload = json.loads(mock_nc.publish.call_args.args[1])
        assert payload["mode"] == "run_to_cursor"
        assert payload["target_step_id"] == "step-42"

    @pytest.mark.asyncio
    async def test_without_nats_still_returns_200(self, db_session, client) -> None:
        """NATS outage is non-fatal — API still returns 200 (worker-independent)."""
        await _insert_execution(db_session, "run-sc-no-nats", status="RUNNING")

        with patch("ate_cloud.main.get_nats", side_effect=RuntimeError("no nats")):
            response = await client.post(
                "/api/v1/executions/run-sc-no-nats/step-control",
                json={"mode": "into"},
            )

        assert response.status_code == 200
        assert response.json()["ok"] is True


class TestStepControlErrors:
    """Error-path tests for the step-control endpoint."""

    @pytest.mark.asyncio
    async def test_unknown_run_404(self, db_session, client) -> None:
        """Unknown run_id returns 404."""
        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions/nonexistent/step-control",
                json={"mode": "over"},
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal_status", ["COMPLETED", "FAILED", "ABORTED"])
    async def test_terminal_state_409(
        self, db_session, client, terminal_status: str,
    ) -> None:
        """Terminal execution (no active execution) returns 409."""
        run_id = f"run-sc-terminal-{terminal_status}"
        await _insert_execution(db_session, run_id, status=terminal_status)

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                f"/api/v1/executions/{run_id}/step-control",
                json={"mode": "over"},
            )

        assert response.status_code == 409
        assert "active" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_invalid_mode_422(self, db_session, client) -> None:
        """mode outside the §8.4 StepMode set returns 422."""
        await _insert_execution(db_session, "run-sc-bad-mode", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions/run-sc-bad-mode/step-control",
                json={"mode": "sideways"},
            )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_run_to_cursor_without_target_422(self, db_session, client) -> None:
        """run_to_cursor without target_step_id returns 422 (model validator)."""
        await _insert_execution(db_session, "run-sc-no-target", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            for body in (
                {"mode": "run_to_cursor"},
                {"mode": "run_to_cursor", "target_step_id": ""},
            ):
                response = await client.post(
                    "/api/v1/executions/run-sc-no-target/step-control",
                    json=body,
                )
                assert response.status_code == 422, body

        mock_nc.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_malformed_body_422(self, db_session, client) -> None:
        """Non-object body returns structured 422 error."""
        await _insert_execution(db_session, "run-sc-malformed", status="RUNNING")

        mock_nc = _make_mock_nc()
        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions/run-sc-malformed/step-control",
                json="not-an-object",
            )

        assert response.status_code == 422
        assert response.json()["detail"]


class TestStepControlAuth:
    """Auth-enforcement regression guard for the new endpoint."""

    @pytest.mark.asyncio
    async def test_anonymous_401(
        self, db_session, client, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Anonymous request (dev_mode off) must be rejected with 401."""
        monkeypatch.setattr(settings, "dev_mode", False)

        response = await client.post(
            "/api/v1/executions/some-run/step-control",
            json={"mode": "over"},
        )

        assert response.status_code == 401
