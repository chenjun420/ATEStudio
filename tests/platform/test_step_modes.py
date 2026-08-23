"""T40 (v41-gap-analysis #40): debugger step modes (over/into/out/run_to_cursor).

Covers the scheduler single-step state machine and the worker
``step_control`` command:

- step_over pauses before the next sibling of the origin dispatches
- step_into descends: container boundary first, then first child
- step_out runs until a step above the origin's depth is reached
- run_to_cursor pauses when the target step is about to start;
  unknown/past-end targets complete normally (never deadlock)
- into at top level == over (spec §8.4 no-deadlock requirement)
- plain resume() cancels an armed mode
- worker handler: ok reply arms the mode; invalid mode / unknown target /
  no active execution produce structured error replies

All NATS messages are in-memory fakes — no broker required.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.scheduler.event_bus import EventBus, EventType, get_event_category
from ate_platform.scheduler.jetstream_worker import JetStreamWorker
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.scanner_scheduler import ScannerScheduler, StepPosition
from ate_platform.scheduler.step_registry import StepRegistry
from ate_platform.scheduler.variable_space import VariableSpace
from shared.dsl import LoopType, YamlLoop, YamlPlan, YamlStep

# ---------------------------------------------------------------------------
# Harness — recording bus + scheduler with a nested-plan hierarchy
# ---------------------------------------------------------------------------


class _RecordingEventBus(EventBus):
    """Records published events without fan-out (deterministic tests)."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[EventType, dict[str, Any]]] = []

    async def publish(self, event_type: EventType, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))

    def publish_sync(self, event_type: EventType, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))
        from ate_platform.scheduler.event_bus import Event

        event = Event(
            type=event_type, data=data, category=get_event_category(event_type)
        )
        for callback in list(self._subscribers.get(event_type, [])):
            callback(event)


def _started(bus: _RecordingEventBus) -> list[str]:
    """STEP_STARTED step_ids in publish order."""
    return [d["step_id"] for t, d in bus.events if t == EventType.STEP_STARTED]


# Nested plan shape:  [s1, loop[c1, c2], s2]
_POSITIONS: dict[str, StepPosition] = {
    "s1": StepPosition(parent=None, order=0, depth=0),
    "loop": StepPosition(parent=None, order=1, depth=0),
    "c1": StepPosition(parent="loop", order=0, depth=1),
    "c2": StepPosition(parent="loop", order=1, depth=1),
    "s2": StepPosition(parent=None, order=2, depth=0),
}


def _make_scheduler(
    positions: dict[str, StepPosition] | None = None,
) -> tuple[_RecordingEventBus, ScannerScheduler]:
    bus = _RecordingEventBus()
    rm = ResourceManager()
    scheduler = ScannerScheduler(
        event_bus=bus,
        registry=StepRegistry(event_bus=bus),
        evaluator=ConditionEvaluator({}, rm, None),
        variable_space=VariableSpace(),
        resource_manager=rm,
    )
    for step_id in (positions or _POSITIONS):
        scheduler._registry.register(step_id)
    scheduler.register_step_hierarchy(positions or _POSITIONS)
    return bus, scheduler


async def _drive(scheduler: ScannerScheduler, ids: list[str]) -> None:
    """Attempt to dispatch each step id in plan order.

    Stops early once the scheduler re-pauses (a step-mode stop blocks
    further dispatch attempts on the pause gate — same as production,
    where queued dispatch tasks wait for the operator to resume).
    """
    for step_id in ids:
        if scheduler.is_paused:
            return
        await scheduler._dispatch_step(step_id)


# ---------------------------------------------------------------------------
# Scheduler semantics — each mode on the nested plan [s1, loop[c1, c2], s2]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_over_pauses_at_next_sibling() -> None:
    """over from s1 must pause BEFORE the next sibling (loop) starts."""
    bus, scheduler = _make_scheduler()
    scheduler._last_dispatched_step = "s1"
    scheduler.pause()
    scheduler.arm_step_mode("over")

    await _drive(scheduler, ["loop", "c1", "c2", "s2"])

    assert _started(bus) == []  # loop never started — paused at its gate
    assert scheduler.is_paused
    assert scheduler._step_mode is None  # one-shot consumed


@pytest.mark.asyncio
async def test_step_over_then_plain_resume_continues_plan() -> None:
    """After the mode-consumed pause, plain resume lets the plan finish."""
    bus, scheduler = _make_scheduler()
    scheduler._last_dispatched_step = "s1"
    scheduler.pause()
    scheduler.arm_step_mode("over")
    await _drive(scheduler, ["loop"])

    scheduler.resume()
    await _drive(scheduler, ["loop", "c1", "c2", "s2"])

    assert _started(bus) == ["loop", "c1", "c2", "s2"]


@pytest.mark.asyncio
async def test_step_into_descends_container_then_first_child() -> None:
    """into stops at the container boundary; pressing again reaches its child."""
    bus, scheduler = _make_scheduler()
    scheduler._last_dispatched_step = "s1"
    scheduler.pause()
    scheduler.arm_step_mode("into")
    await _drive(scheduler, ["loop"])
    assert _started(bus) == []  # paused at loop boundary

    # Resume: loop starts; pause again and press into once more.
    scheduler.resume()
    await _drive(scheduler, ["loop"])
    assert _started(bus) == ["loop"]
    scheduler.pause()
    scheduler.arm_step_mode("into")  # origin = loop (last dispatched)
    await _drive(scheduler, ["c1", "c2", "s2"])

    assert _started(bus) == ["loop"]  # c1 gated — descended into container
    assert scheduler.is_paused


@pytest.mark.asyncio
async def test_step_into_at_top_level_equals_over_no_deadlock() -> None:
    """Flat plan: into must behave like over (pause at next sibling), not hang."""
    flat = {
        "a": StepPosition(parent=None, order=0, depth=0),
        "b": StepPosition(parent=None, order=1, depth=0),
    }
    bus, scheduler = _make_scheduler(flat)
    scheduler._last_dispatched_step = "a"
    scheduler.pause()
    scheduler.arm_step_mode("into")

    await _drive(scheduler, ["b"])

    assert _started(bus) == []
    assert scheduler.is_paused  # stopped at b — no deadlock


@pytest.mark.asyncio
async def test_step_out_runs_until_parent_level_reached() -> None:
    """out from c1 finishes remaining container steps, pauses at s2 (depth 0)."""
    bus, scheduler = _make_scheduler()
    scheduler._last_dispatched_step = "c1"
    scheduler.pause()
    scheduler.arm_step_mode("out")

    await _drive(scheduler, ["c2", "s2"])

    assert _started(bus) == ["c2"]
    assert scheduler.is_paused


@pytest.mark.asyncio
async def test_step_out_at_top_level_completes_without_deadlock() -> None:
    """out from a top-level step has no shallower level → plan just completes."""
    bus, scheduler = _make_scheduler()
    scheduler._last_dispatched_step = "s1"
    scheduler.pause()
    scheduler.arm_step_mode("out")

    await _drive(scheduler, ["loop", "c1", "c2", "s2"])

    assert _started(bus) == ["loop", "c1", "c2", "s2"]
    assert not scheduler.is_paused


@pytest.mark.asyncio
async def test_run_to_cursor_pauses_when_target_about_to_start() -> None:
    """run_to_cursor(s2) lets intermediate steps start, gates s2 itself."""
    bus, scheduler = _make_scheduler()
    scheduler.pause()
    scheduler.arm_step_mode("run_to_cursor", target_step_id="s2")

    await _drive(scheduler, ["loop", "c1", "c2", "s2"])

    assert _started(bus) == ["loop", "c1", "c2"]
    assert scheduler.is_paused


@pytest.mark.asyncio
async def test_run_to_cursor_past_end_completes_normally() -> None:
    """Target that never appears (past end / unknown id) → normal completion."""
    bus, scheduler = _make_scheduler()
    scheduler.pause()
    scheduler.arm_step_mode("run_to_cursor", target_step_id="ghost-step")

    await _drive(scheduler, ["loop", "c1", "c2", "s2"])

    assert _started(bus) == ["loop", "c1", "c2", "s2"]
    assert not scheduler.is_paused  # no deadlock


@pytest.mark.asyncio
async def test_arm_step_mode_rejects_unknown_mode() -> None:
    """Unknown mode names fail fast at the scheduler boundary."""
    _, scheduler = _make_scheduler()
    with pytest.raises(ValueError, match="unknown"):
        scheduler.arm_step_mode("teleport")


@pytest.mark.asyncio
async def test_plain_resume_cancels_armed_mode() -> None:
    """Operator pressing plain resume overrides any pending step mode."""
    bus, scheduler = _make_scheduler()
    scheduler._last_dispatched_step = "s1"
    scheduler.pause()
    scheduler.arm_step_mode("over")
    scheduler.resume()  # explicit continue wins

    await _drive(scheduler, ["loop", "c1", "c2", "s2"])

    assert _started(bus) == ["loop", "c1", "c2", "s2"]
    assert scheduler._step_mode is None


# ---------------------------------------------------------------------------
# Worker handler — step_control control command (structured replies)
# ---------------------------------------------------------------------------


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


def _gated_flat_plan() -> YamlPlan:
    """Flat plan whose steps never dispatch (precondition on missing gate).

    Keeps booted-worker tests free of script-execution noise while still
    registering every step id in the registry (run_to_cursor target checks).
    """
    return YamlPlan(
        name="t",
        version="1.0",
        steps=[
            YamlStep(id="s1", script="s1.py", preconditions=["gate-step"]),
            YamlStep(id="s2", script="s2.py", preconditions=["gate-step"]),
        ],
    )


def _nested_plan() -> YamlPlan:
    """[s1, loop[c1, c2], s2] — used to verify hierarchy extraction only."""
    return YamlPlan(
        name="t",
        version="1.0",
        steps=[
            YamlStep(id="s1", script="s1.py"),
            YamlLoop(
                id="loop",
                loop_type=LoopType.FOR,
                count=2,
                steps=[
                    YamlStep(id="c1", script="c1.py"),
                    YamlStep(id="c2", script="c2.py"),
                ],
            ),
            YamlStep(id="s2", script="s2.py"),
        ],
    )


async def _boot_worker(
    tmp_path: Path,
    execution_id: str = "exec-123",
) -> tuple[JetStreamWorker, MagicMock]:
    """Start a worker and pull one task so an execution is 'running'."""
    plan = _gated_flat_plan()
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


def test_bootstrapper_registers_hierarchy_for_nested_plan() -> None:
    """PlanBootstrapper flattens [s1, loop[c1,c2], s2] into StepPosition map."""
    from ate_platform.scheduler.jetstream_worker import PlanBootstrapper

    scheduler = PlanBootstrapper(_nested_plan()).bootstrap()

    assert scheduler._step_positions == {
        "s1": StepPosition(parent=None, order=0, depth=0),
        "loop": StepPosition(parent=None, order=1, depth=0),
        "c1": StepPosition(parent="loop", order=0, depth=1),
        "c2": StepPosition(parent="loop", order=1, depth=1),
        "s2": StepPosition(parent=None, order=2, depth=0),
    }


@pytest.mark.asyncio
async def test_worker_step_control_arms_mode_and_replies_ok(
    tmp_path: Path,
) -> None:
    """step_control forwards to scheduler.arm_step_mode and acks structurally."""
    worker, _ = await _boot_worker(tmp_path)
    try:
        scheduler = worker._current_scheduler
        assert scheduler is not None
        msg = FakeControlMsg({"action": "step_control", "mode": "over"})
        await worker._on_control_message(msg)

        assert msg.replies == [
            {"status": "ok", "action": "step_control", "mode": "over",
             "target_step_id": None}
        ]
        assert scheduler._step_mode == "over"
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_run_to_cursor_unknown_target_error_reply(
    tmp_path: Path,
) -> None:
    """run_to_cursor targeting a step outside the registry → unknown_target."""
    worker, _ = await _boot_worker(tmp_path)
    try:
        msg = FakeControlMsg({
            "action": "step_control",
            "mode": "run_to_cursor",
            "target_step_id": "ghost-step",
        })
        await worker._on_control_message(msg)

        assert len(msg.replies) == 1
        assert msg.replies[0]["status"] == "error"
        assert msg.replies[0]["error"] == "unknown_target"
        assert worker._current_scheduler is not None
        assert worker._current_scheduler._step_mode is None
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_invalid_mode_error_reply(tmp_path: Path) -> None:
    """Unknown mode strings get a structured invalid_mode error."""
    worker, _ = await _boot_worker(tmp_path)
    try:
        msg = FakeControlMsg({"action": "step_control", "mode": "sideways"})
        await worker._on_control_message(msg)

        assert msg.replies[0]["status"] == "error"
        assert msg.replies[0]["error"] == "invalid_mode"
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_no_active_execution_error_reply(tmp_path: Path) -> None:
    """step_control for an idle/unknown execution → no_active_execution."""
    worker, _ = await _boot_worker(tmp_path)
    try:
        msg = FakeControlMsg(
            {"action": "step_control", "mode": "over"},
            subject="ate.control.other-run",
        )
        await worker._on_control_message(msg)

        assert msg.replies[0]["status"] == "error"
        assert msg.replies[0]["error"] == "no_active_execution"
    finally:
        await worker.stop()
