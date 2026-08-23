"""T5: inject_fault control command handling in JetStreamWorker.

Covers the ``inject_fault`` action added to ``_on_control_message``:
- valid rule forwarded to the RUNNING execution's FaultInjector (observable)
- malformed rule payloads → structured error reply (no crash)
- unknown / idle / aborted executions reject new rules with error replies

All NATS messages are in-memory fakes — no broker required.
"""

import json
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ate_platform.scheduler.jetstream_worker import JetStreamWorker
from ate_platform.simulation.fault_injector import SchedulerFaultError
from shared.dsl import YamlPlan, YamlStep


class FakeControlMsg:
    """Fake core-NATS control message supporting request-reply."""

    def __init__(
        self,
        payload: dict[str, Any],
        subject: str = "ate.control.exec-123",
    ) -> None:
        self.data = json.dumps(payload).encode("utf-8")
        self.subject = subject
        self.replies: list[dict[str, Any]] = []

    async def respond(self, data: bytes) -> None:
        self.replies.append(json.loads(data.decode("utf-8")))


class FakeTaskMsg:
    """Fake JetStream task message (plan dispatch)."""

    def __init__(self, data: bytes, headers: dict[str, str] | None = None) -> None:
        self.data = data
        self.headers = headers
        self.acked = False

    async def ack(self) -> None:
        self.acked = True

    async def nak(self) -> None:
        pass


def _enum_to_value(o: Any) -> Any:
    if isinstance(o, Enum):
        return o.value
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _serialize_plan(plan: YamlPlan) -> bytes:
    return json.dumps(asdict(plan), default=_enum_to_value).encode("utf-8")


def _make_mock_nc() -> MagicMock:
    mock_nc = MagicMock()
    mock_nc.is_connected = True
    mock_nc.publish = AsyncMock()
    mock_nc.close = AsyncMock()
    mock_sub = MagicMock()
    mock_sub.unsubscribe = AsyncMock()
    mock_nc.subscribe = AsyncMock(return_value=mock_sub)
    mock_js = MagicMock()
    mock_kv = MagicMock()
    mock_kv.put = AsyncMock(return_value=1)
    mock_js.key_value = AsyncMock(return_value=mock_kv)
    mock_js.pull_subscribe = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc


async def _boot_worker(
    tmp_path: Path,
    execution_id: str = "exec-123",
) -> tuple[JetStreamWorker, MagicMock]:
    """Start a worker and pull one task so an execution is 'running'."""
    # Precondition references a nonexistent step → step stays PENDING and
    # never dispatches (keeps the test free of script-execution noise).
    plan = YamlPlan(
        name="t",
        version="1.0",
        steps=[YamlStep(id="step1", script="t.py", preconditions=["gate-step"])],
    )
    mock_nc = _make_mock_nc()
    psub = MagicMock()
    psub.fetch = AsyncMock(
        return_value=[FakeTaskMsg(_serialize_plan(plan), {"execution_id": execution_id})]
    )
    mock_nc.jetstream.return_value.pull_subscribe = AsyncMock(return_value=psub)

    worker = JetStreamWorker(worker_id_path=str(tmp_path / "worker_id"))
    await worker.start(nc=mock_nc)
    assert await worker.pull_and_process_one(timeout=1.0) is True
    return worker, mock_nc


_SCHED_RULE = {
    "id": "sched-fault-1",
    "layer": "scheduler",
    "target": "step1",
    "trigger": {"type": "count", "value": 2},
    "fault": {"type": "scheduler_error"},
}


class TestInjectFaultControlCommand:
    @pytest.mark.asyncio
    async def test_valid_rule_reaches_running_execution_injector(
        self, tmp_path: Path
    ) -> None:
        """Valid rule mid-run registers on the execution's injector and fires."""
        worker, _ = await _boot_worker(tmp_path)
        try:
            injector = worker._current_injector
            assert injector is not None
            assert injector.rules == []  # zero-overhead gate before injection

            msg = FakeControlMsg({"action": "inject_fault", "rule": _SCHED_RULE})
            await worker._on_control_message(msg)

            assert len(injector.rules) == 1
            assert injector.rules[0].fault_id == "sched-fault-1"
            assert msg.replies == [
                {
                    "status": "ok",
                    "action": "inject_fault",
                    "fault_id": "sched-fault-1",
                    "layer": "scheduler",
                }
            ]

            # Observable effect: count=2 trigger fires on second dispatch check.
            injector.check_scheduler_raise("step1")  # call_count=1 → no hit
            with pytest.raises(SchedulerFaultError, match="sched-fault-1"):
                injector.check_scheduler_raise("step1")  # call_count=2 → hit
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_malformed_rule_gets_structured_error_reply(
        self, tmp_path: Path
    ) -> None:
        """Invalid layer must produce an error reply, not crash the handler."""
        worker, _ = await _boot_worker(tmp_path)
        try:
            bad = dict(_SCHED_RULE, id="bad-1", layer="quantum")
            msg = FakeControlMsg({"action": "inject_fault", "rule": bad})
            await worker._on_control_message(msg)  # must not raise

            assert len(msg.replies) == 1
            reply = msg.replies[0]
            assert reply["status"] == "error"
            assert reply["error"] == "invalid_rule"
            assert "Invalid layer" in reply["detail"]
            assert worker._current_injector is not None
            assert worker._current_injector.rules == []
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_missing_or_non_dict_rule_gets_error_reply(
        self, tmp_path: Path
    ) -> None:
        """Missing 'rule' key or non-object rule → malformed_rule error."""
        worker, _ = await _boot_worker(tmp_path)
        try:
            for payload in (
                {"action": "inject_fault"},
                {"action": "inject_fault", "rule": 42},
                {"action": "inject_fault", "rule": "not-a-dict"},
            ):
                msg = FakeControlMsg(payload)
                await worker._on_control_message(msg)
                assert len(msg.replies) == 1
                assert msg.replies[0]["status"] == "error"
                assert msg.replies[0]["error"] == "malformed_rule"

            assert worker._current_injector is not None
            assert worker._current_injector.rules == []
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_rule_without_id_or_action_type_rejected(
        self, tmp_path: Path
    ) -> None:
        """Rule missing both id and action.type cannot register silently."""
        worker, _ = await _boot_worker(tmp_path)
        try:
            msg = FakeControlMsg(
                {"action": "inject_fault", "rule": {"layer": "instrument"}}
            )
            await worker._on_control_message(msg)

            assert msg.replies[0]["status"] == "error"
            assert msg.replies[0]["error"] == "malformed_rule"  # no id
            assert worker._current_injector is not None
            assert worker._current_injector.rules == []
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_inject_on_unknown_execution_gets_error_reply(
        self, tmp_path: Path
    ) -> None:
        """Subject naming a different/unknown run → structured error reply."""
        worker, _ = await _boot_worker(tmp_path)
        try:
            msg = FakeControlMsg(
                {"action": "inject_fault", "rule": _SCHED_RULE},
                subject="ate.control.some-other-run",
            )
            await worker._on_control_message(msg)

            assert msg.replies[0]["status"] == "error"
            assert msg.replies[0]["error"] == "no_active_execution"
            assert worker._current_injector is not None
            assert worker._current_injector.rules == []
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_idle_worker_rejects_inject_fault(self, tmp_path: Path) -> None:
        """No running execution at all → error reply, no crash."""
        worker = JetStreamWorker(worker_id_path=str(tmp_path / "worker_id"))
        try:
            msg = FakeControlMsg({"action": "inject_fault", "rule": _SCHED_RULE})
            await worker._on_control_message(msg)

            assert msg.replies[0]["status"] == "error"
            assert msg.replies[0]["error"] == "no_active_execution"
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_aborted_execution_rejects_new_rules(self, tmp_path: Path) -> None:
        """After abort clears the execution, new rules are rejected."""
        worker, _ = await _boot_worker(tmp_path)
        try:
            await worker._on_control_message(FakeControlMsg({"action": "abort"}))
            assert worker._current_scheduler is None
            assert worker._current_execution_id is None

            msg = FakeControlMsg({"action": "inject_fault", "rule": _SCHED_RULE})
            await worker._on_control_message(msg)

            assert msg.replies[0]["status"] == "error"
            assert msg.replies[0]["error"] == "no_active_execution"
        finally:
            await worker.stop()

    @pytest.mark.asyncio
    async def test_instrument_layer_rule_intercepts_after_registration(
        self, tmp_path: Path
    ) -> None:
        """Non-scheduler layers route through intercept() with full context."""
        worker, _ = await _boot_worker(tmp_path)
        try:
            rule = {
                "id": "inst-fault-1",
                "layer": "instrument",
                "target": "dmm1",
                "trigger": {"type": "count", "value": 1},
                "fault": {"type": "timeout"},
            }
            msg = FakeControlMsg({"action": "inject_fault", "rule": rule})
            await worker._on_control_message(msg)

            assert msg.replies[0]["status"] == "ok"
            injector = worker._current_injector
            assert injector is not None
            action = injector.intercept(
                "instrument", "dmm1", context={"call_count": 1},
            )
            assert action is not None
            assert action.fault_type == "timeout"
            assert action.fault_id == "inst-fault-1"
            # Non-matching target unaffected
            assert injector.intercept("instrument", "psu1") is None
        finally:
            await worker.stop()
