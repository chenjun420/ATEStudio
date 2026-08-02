"""Tests for ExecutionDispatchService (Todo 7).

Verifies that dispatch():
1. Publishes the serialized YamlPlan to ``ate.tasks.{execution_id}`` via JetStream.
2. Includes ``execution_id`` in the message headers.
3. Is fire-and-forget — publishes only, never subscribes for consumer acks.

The ATE_TASKS JetStream stream has ``subjects=["ate.tasks.*"]``, so
``ate.tasks.{execution_id}`` is captured and durably stored for the
``ate-worker`` pull consumer to fetch.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from enum import Enum
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ate_cloud.services.execution_dispatch import (
    ExecutionDispatchService,
)
from shared.dsl import (
    ExecutionMode,
    LoopType,
    YamlLoop,
    YamlPlan,
    YamlStep,
)


def _enum_to_value(o: Any) -> Any:
    """JSON default hook matching the service's — Enum → .value."""
    if isinstance(o, Enum):
        return o.value
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _make_plan() -> YamlPlan:
    """Build a YamlPlan with a step and a loop (exercises enum serialization)."""
    return YamlPlan(
        name="test-plan",
        version="1.0",
        scope={"dut": "DUT-001"},
        max_concurrency=2,
        steps=[
            YamlStep(id="step-1", script="tests/fixtures/pass.py", timeout=30),
            YamlLoop(
                id="loop-1",
                loop_type=LoopType.FOR,
                count=3,
                execution_mode=ExecutionMode.SERIAL,
                steps=[
                    YamlStep(id="step-2", script="tests/fixtures/check.py"),
                ],
            ),
        ],
    )


def _make_mock_nc() -> tuple[MagicMock, MagicMock]:
    """Build a mock NATS client + JetStream context.

    ``jetstream()`` is sync (returns ``JetStreamContext`` without I/O) and
    ``publish`` is async — matching nats-py's real API surface.
    """
    mock_js = MagicMock()
    mock_js.publish = AsyncMock(return_value=MagicMock())
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc, mock_js


class TestExecutionDispatch:
    """Tests for ExecutionDispatchService.dispatch()."""

    @pytest.mark.asyncio
    async def test_dispatch_publishes_to_jetstream(self) -> None:
        """dispatch() publishes the serialized plan to ate.tasks.{execution_id}."""
        mock_nc, mock_js = _make_mock_nc()
        service = ExecutionDispatchService(mock_nc)

        plan = _make_plan()
        await service.dispatch("exec-123", plan)

        mock_js.publish.assert_awaited_once()
        call = mock_js.publish.call_args
        subject = call.args[0]
        payload = call.args[1]

        assert subject == "ate.tasks.exec-123"
        # Payload is JSON of the plan — enums converted to their .value strings.
        decoded = json.loads(payload)
        expected = json.loads(json.dumps(asdict(plan), default=_enum_to_value))
        assert decoded == expected
        # Enum values survived serialization as uppercase strings (matching .value).
        assert decoded["steps"][1]["loop_type"] == "FOR"
        assert decoded["steps"][1]["execution_mode"] == "SERIAL"

    @pytest.mark.asyncio
    async def test_dispatch_includes_execution_id_headers(self) -> None:
        """dispatch() passes execution_id in the message headers."""
        mock_nc, mock_js = _make_mock_nc()
        service = ExecutionDispatchService(mock_nc)

        await service.dispatch("exec-456", _make_plan())

        headers = mock_js.publish.call_args.kwargs["headers"]
        assert headers == {"execution_id": "exec-456"}

    @pytest.mark.asyncio
    async def test_dispatch_non_blocking(self) -> None:
        """dispatch() is fire-and-forget — publishes only, never subscribes.

        JetStream durability guarantees delivery; the service does not set up
        a consumer subscription or wait for worker acknowledgment.
        """
        mock_nc, mock_js = _make_mock_nc()
        service = ExecutionDispatchService(mock_nc)

        await service.dispatch("exec-789", _make_plan())

        # Publish is the ONLY JetStream interaction — no consumer ack wait.
        mock_js.publish.assert_awaited_once()
        mock_js.subscribe.assert_not_called()
        mock_js.pull_subscribe.assert_not_called()
