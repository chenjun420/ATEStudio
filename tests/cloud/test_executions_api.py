"""Tests for executions API dispatch bridge (Todo 11).

Verifies that POST /api/v1/executions:
1. Dispatches the materialized YamlPlan to NATS JetStream after DB creation.
2. Returns 201 with status=PENDING (DB and HTTP response stay PENDING).
3. Returns 503 when dispatch fails — DB record stays PENDING.

The bridge pattern: create_execution() → ExecutionPlanMaterializer.materialize()
→ ExecutionDispatchService.dispatch() → JetStream publish on ate.tasks.{run_id}.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models import Sequence
from ate_cloud.models.execution import Execution

_VALID_YAML = """\
name: test-sequence
version: "1.0"
steps:
  - id: step-1
    script: tests/fixtures/pass.py
"""


def _make_mock_nc() -> tuple[MagicMock, MagicMock]:
    """Build a mock NATS client + JetStream context.

    ``jetstream()`` is sync (returns ``JetStreamContext`` without I/O) and
    ``publish`` is async — matching nats-py's real API surface.
    """
    mock_js = MagicMock()
    mock_js.publish = AsyncMock(return_value=MagicMock())
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    mock_nc.publish = AsyncMock()
    return mock_nc, mock_js


def _make_failing_mock_nc() -> MagicMock:
    """Build a mock NATS client whose JetStream publish raises."""
    mock_js = MagicMock()
    mock_js.publish = AsyncMock(side_effect=Exception("NATS unavailable"))
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc


def _insert_sequence(db_session: AsyncSession, seq_id: str) -> None:
    """Insert a Sequence with valid YAML so the materializer can load it."""
    sequence = Sequence(
        id=seq_id,
        name=f"test-{seq_id}",
        yaml_content=_VALID_YAML,
    )
    db_session.add(sequence)


class TestExecutionDispatchBridge:
    """Tests for the create_execution → materialize → dispatch bridge."""

    @pytest.mark.asyncio
    async def test_create_execution_dispatches_to_nats(self, db_session, client) -> None:
        """POST /executions dispatches the materialized plan to ate.tasks.{run_id}."""
        _insert_sequence(db_session, "seq-dispatch-test")
        await db_session.flush()

        mock_nc, mock_js = _make_mock_nc()

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions",
                json={"sequence_id": "seq-dispatch-test"},
            )

        assert response.status_code == 201
        run_id = response.json()["id"]

        # Dispatch published to ate.tasks.{run_id} via JetStream
        mock_js.publish.assert_awaited_once()
        subject = mock_js.publish.call_args.args[0]
        assert subject == f"ate.tasks.{run_id}"

        # Payload is valid JSON with the plan structure
        payload = mock_js.publish.call_args.args[1]
        plan = json.loads(payload)
        assert plan["name"] == "test-sequence"
        assert len(plan["steps"]) == 1
        assert plan["steps"][0]["id"] == "step-1"

    @pytest.mark.asyncio
    async def test_response_status_is_pending(self, db_session, client) -> None:
        """POST /executions returns 201 with status=PENDING after dispatch."""
        _insert_sequence(db_session, "seq-pending-test")
        await db_session.flush()

        mock_nc, _ = _make_mock_nc()

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions",
                json={"sequence_id": "seq-pending-test"},
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "PENDING"
        assert data["sequence_id"] == "seq-pending-test"

    @pytest.mark.asyncio
    async def test_dispatch_failure_returns_error(self, db_session, client) -> None:
        """POST /executions returns 503 when NATS dispatch fails; DB stays PENDING."""
        _insert_sequence(db_session, "seq-fail-test")
        await db_session.flush()

        mock_nc = _make_failing_mock_nc()

        with patch("ate_cloud.main.get_nats", return_value=mock_nc):
            response = await client.post(
                "/api/v1/executions",
                json={"sequence_id": "seq-fail-test"},
            )

        assert response.status_code == 503
        assert "dispatch failed" in response.json()["detail"].lower()

        # Verify DB record stays PENDING after dispatch failure
        result = await db_session.execute(
            select(Execution).where(Execution.sequence_id == "seq-fail-test")
        )
        execution = result.scalar_one()
        assert execution.status == "PENDING"
