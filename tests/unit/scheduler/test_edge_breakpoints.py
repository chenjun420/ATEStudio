"""Edge-evaluated breakpoints (T39/T40, task 20).

Covers the edge side of pushing persisted breakpoint definitions to the
worker and evaluating them on the edge:

- tolerant parsing of breakpoint defs (malformed entries dropped, never hang)
- the edge suspends at a defined step WITHOUT a cloud pause command
- a BREAKPOINT_HIT event is emitted carrying the current variable snapshot
- resume on the existing pause gate (same gate T40 step-mode uses) lets the
  run continue — the cloud does NOT drive each step
- condition-kind breakpoints evaluate against the variable snapshot
- disabled / already-fired breakpoints do not re-suspend
- the worker ``sync_breakpoints`` control action arms the engine

All NATS/executor collaborators are in-memory fakes — no broker, no hardware.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.scheduler.edge_breakpoints import (
    EdgeBreakpointEngine,
    parse_breakpoint_defs,
)
from ate_platform.scheduler.event_bus import EventBus, EventType, get_event_category
from ate_platform.scheduler.jetstream_worker import JetStreamWorker
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
from ate_platform.scheduler.step_registry import StepRegistry
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import Condition
from shared.dsl import YamlPlan, YamlStep
from shared.events import Event
from shared.types import StepStatus

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _RecordingEventBus(EventBus):
    """Records published events without a fan-out loop (deterministic)."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[EventType, dict[str, Any]]] = []

    async def publish(self, event_type: EventType, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))

    def publish_sync(self, event_type: EventType, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))
        event = Event(
            type=event_type, data=data, category=get_event_category(event_type)
        )
        for callback in list(self._subscribers.get(event_type, [])):
            callback(event)


def _started(bus: _RecordingEventBus) -> list[str]:
    return [d["step_id"] for t, d in bus.events if t == EventType.STEP_STARTED]


def _hits(bus: _RecordingEventBus) -> list[dict[str, Any]]:
    return [d for t, d in bus.events if t == EventType.BREAKPOINT_HIT]


def _make_scheduler(
    engine: EdgeBreakpointEngine | None,
    step_ids: tuple[str, ...] = ("s1", "s2"),
) -> tuple[_RecordingEventBus, ScannerScheduler]:
    bus = _RecordingEventBus()
    rm = ResourceManager()
    vs = VariableSpace()
    scheduler = ScannerScheduler(
        event_bus=bus,
        registry=StepRegistry(event_bus=bus),
        evaluator=ConditionEvaluator({}, rm, None),
        variable_space=vs,
        resource_manager=rm,
        breakpoint_engine=engine,
    )
    for sid in step_ids:
        scheduler._registry.register(sid)
    return bus, scheduler


# ---------------------------------------------------------------------------
# Tolerant parsing — malformed defs never block a run
# ---------------------------------------------------------------------------


def test_parse_keeps_valid_and_counts_dropped() -> None:
    """Valid defs decode; malformed entries are dropped and counted."""
    defs = [
        {"id": "bp1", "kind": "step", "target": "s2", "enabled": True},
        {"id": "bp2", "kind": "condition", "target": "*", "condition": "voltage > 3"},
        {"kind": "step", "target": "s3"},               # missing id
        {"id": "bp4", "kind": "teleport", "target": "x"},  # unknown kind
        {"id": "bp5", "kind": "step", "target": ""},    # empty target
        {"id": "bp6", "kind": "condition", "target": "*", "condition": "x =="},  # bad expr
        "not-a-dict",
    ]
    parsed, dropped = parse_breakpoint_defs(defs)

    assert [bp.id for bp in parsed] == ["bp1", "bp2"]
    assert dropped == 5


def test_parse_non_list_payload_is_empty_not_error() -> None:
    """A wrong envelope yields no breakpoints (never raises)."""
    parsed, dropped = parse_breakpoint_defs({"unexpected": "shape"})
    assert parsed == []
    assert dropped == 0


def test_malformed_defs_never_hang() -> None:
    """A run armed with only malformed defs has no engine and dispatches freely."""
    parsed, _ = parse_breakpoint_defs([{"kind": "step"}, "garbage"])
    engine = EdgeBreakpointEngine(parsed)
    assert engine.breakpoints == ()
    assert engine.check_step("s2", {"scope": {}}) is None


# ---------------------------------------------------------------------------
# Engine matching
# ---------------------------------------------------------------------------


def test_step_breakpoint_fires_once_for_its_step() -> None:
    parsed, _ = parse_breakpoint_defs([{"id": "bp", "kind": "step", "target": "s2"}])
    engine = EdgeBreakpointEngine(parsed)

    assert engine.check_step("s1", {"scope": {}}) is None
    hit = engine.check_step("s2", {"scope": {}})
    assert hit is not None and hit.id == "bp"
    # Fired breakpoint does not re-suspend on a later dispatch pass.
    assert engine.check_step("s2", {"scope": {}}) is None


def test_disabled_breakpoint_does_not_fire() -> None:
    parsed, _ = parse_breakpoint_defs(
        [{"id": "bp", "kind": "step", "target": "s2", "enabled": False}]
    )
    engine = EdgeBreakpointEngine(parsed)
    assert engine.check_step("s2", {"scope": {}}) is None


def test_condition_breakpoint_evaluates_snapshot() -> None:
    parsed, _ = parse_breakpoint_defs(
        [{"id": "bp", "kind": "condition", "target": "*", "condition": "voltage > 3.0"}]
    )
    engine = EdgeBreakpointEngine(parsed)

    assert engine.check_step("s1", {"scope": {"voltage": 3.3}}) is not None
    # Fresh engine for the false case (the true case above marks fired).
    engine2 = EdgeBreakpointEngine(parse_breakpoint_defs(
        [{"id": "bp", "kind": "condition", "target": "*", "condition": "voltage > 3.0"}]
    )[0])
    assert engine2.check_step("s1", {"scope": {"voltage": 1.0}}) is None


def test_non_step_gate_kinds_are_accepted_but_not_hit_at_step_gate() -> None:
    """instrument_call/variable_change defs parse but don't fire at the step gate."""
    parsed, dropped = parse_breakpoint_defs([
        {"id": "a", "kind": "instrument_call", "target": "dmm.measure"},
        {"id": "b", "kind": "variable_change", "target": "scope.voltage"},
    ])
    assert dropped == 0
    engine = EdgeBreakpointEngine(parsed)
    assert engine.check_step("s1", {"scope": {}}) is None


# ---------------------------------------------------------------------------
# Scheduler gate — suspend with variable snapshot, resume on the SAME gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_breakpoint_suspends_emits_snapshot_and_resumes() -> None:
    """Edge suspends at the defined step with no cloud pause; hit carries vars."""
    parsed, _ = parse_breakpoint_defs([{"id": "bp1", "kind": "step", "target": "s2"}])
    bus, scheduler = _make_scheduler(EdgeBreakpointEngine(parsed))
    scheduler._variable_space.set("scope.voltage", 3.3)
    scheduler._variable_space.set("scope.current", 0.5)

    # s1 has no breakpoint — dispatches normally.
    await scheduler._dispatch_step("s1")
    assert _started(bus) == ["s1"]
    assert not scheduler.is_paused

    # s2 must suspend. Drive it as a concurrent task: it blocks on the shared
    # pause gate until resume() (the operator control message) releases it.
    hit_task = asyncio.create_task(scheduler._dispatch_step("s2"))
    await asyncio.sleep(0.05)

    assert scheduler.is_paused  # suspended WITHOUT a cloud pause command
    hits = _hits(bus)
    assert len(hits) == 1
    assert hits[0]["breakpoint_id"] == "bp1"
    assert hits[0]["kind"] == "step"
    assert hits[0]["target"] == "s2"
    assert hits[0]["step_id"] == "s2"
    assert hits[0]["variables"]["scope"] == {"voltage": 3.3, "current": 0.5}
    # The suspended step has not actually started yet.
    assert "s2" not in _started(bus)

    # Resume via the same gate T40/DSL use (no per-step cloud driving).
    scheduler.resume()
    await asyncio.wait_for(hit_task, timeout=2.0)

    # The consumed dispatch returns without starting s2; the next dispatch
    # pass (watchdog/reactive) starts it and the fired breakpoint does not
    # re-suspend.
    await scheduler._dispatch_step("s2")
    assert "s2" in _started(bus)
    assert not scheduler.is_paused


@pytest.mark.asyncio
async def test_no_engine_means_never_suspends() -> None:
    """Headless / no-defs path: breakpoint engine is None → straight through."""
    bus, scheduler = _make_scheduler(None)
    await asyncio.wait_for(scheduler._dispatch_step("s1"), timeout=2.0)
    await asyncio.wait_for(scheduler._dispatch_step("s2"), timeout=2.0)
    assert _started(bus) == ["s1", "s2"]
    assert _hits(bus) == []
    assert not scheduler.is_paused


@pytest.mark.asyncio
async def test_condition_breakpoint_suspends_when_truthy() -> None:
    parsed, _ = parse_breakpoint_defs(
        [{"id": "c1", "kind": "condition", "target": "*",
          "condition": "voltage >= 3.0"}]
    )
    bus, scheduler = _make_scheduler(EdgeBreakpointEngine(parsed))
    scheduler._variable_space.set("scope.voltage", 4.2)

    task = asyncio.create_task(scheduler._dispatch_step("s1"))
    await asyncio.sleep(0.05)
    try:
        assert scheduler.is_paused
        hits = _hits(bus)
        assert len(hits) == 1
        assert hits[0]["variables"]["scope"]["voltage"] == 4.2
    finally:
        scheduler.resume()
        await asyncio.wait_for(task, timeout=2.0)


# ---------------------------------------------------------------------------
# Watchdog emergency-scan gate — the F2 regression (BLOCKER B1).
#
# _emergency_scan() used to emit STEP_STARTED WITHOUT consulting the edge
# breakpoint engine or the pause gate, so under event-loop contention a step
# that should pause could be dispatched straight past its breakpoint (the hit
# then landed one step late). These tests drive the emergency-scan path
# DIRECTLY and DETERMINISTICALLY (no wall-clock races, no running scan loop)
# through an inline event bus that dispatches subscribers synchronously —
# exactly like the real JetStreamWorker executing STEP_STARTED in place.
# ---------------------------------------------------------------------------


class _InlineEventBus(EventBus):
    """Deterministic bus: publish()/publish_sync() fan out inline (no task/queue).

    Every event is delivered to subscribers before the call returns, so a worker
    that executes STEP_STARTED and a recorder that captures BREAKPOINT_HIT both
    settle synchronously. No background loop, no sleeps — this is what makes the
    emergency-scan regression tests deterministic.
    """

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[EventType, dict[str, Any]]] = []

    def _deliver(self, event_type: EventType, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))
        event = Event(type=event_type, data=dict(data),
                      category=get_event_category(event_type))
        for callback in list(self._subscribers.get(event_type, [])):
            result = callback(event)
            # Harness subscribers are all sync; close an accidental coroutine
            # rather than leaving it un-awaited (no async fan-out by design).
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if close is not None:
                    close()

    async def publish(self, event_type: EventType, data: dict[str, Any]) -> None:
        self._deliver(event_type, data)

    def publish_sync(self, event_type: EventType, data: dict[str, Any]) -> None:
        self._deliver(event_type, data)


class _FakeStepExecutor:
    """Scheduler only needs pool_stats() for straight-line steps."""

    def pool_stats(self) -> dict[str, Any]:
        return {"active": 0, "max": 4, "utilization": 0.0, "queued": 0}


class _InlineWorker:
    """STEP_STARTED -> execute -> report, synchronously (mirrors JetStreamWorker)."""

    def __init__(
        self, bus: _InlineEventBus, registry: StepRegistry, vs: VariableSpace,
    ) -> None:
        self.registry = registry
        self.vs = vs
        self.run_counts: dict[str, int] = {}
        self.executed: list[str] = []
        bus.subscribe(EventType.STEP_STARTED, self._on_started)

    def _on_started(self, event: Event) -> None:
        sid = str(event.data.get("step_id"))
        if self.registry.get_status(sid) != StepStatus.PENDING:
            return
        self.run_counts[sid] = self.run_counts.get(sid, 0) + 1
        self.executed.append(sid)
        self.registry.update_status(sid, StepStatus.RUNNING)
        self.vs.set(f"scope.{sid}_done", True)
        self.vs.set("scope.last_step", sid)
        if sid == "s1":
            self.vs.set("scope.voltage", 3.31)
        if sid == "s2":
            self.vs.set("scope.ripple_mv", 85)
        self.registry.update_status(sid, StepStatus.PASSED)


def _serial_world(
    defs: list[dict[str, Any]] | None,
) -> tuple[_InlineEventBus, ScannerScheduler, _InlineWorker]:
    """Serial s1->s2->s3->s4 plan on the deterministic inline bus, with the
    scheduler NOT started (no scan loop) so only direct _emergency_scan() calls
    drive dispatch."""
    bus = _InlineEventBus()
    registry = StepRegistry(event_bus=bus)
    vs = VariableSpace(event_bus=bus)
    rm = ResourceManager(event_bus=bus)
    evaluator = ConditionEvaluator({}, resource_manager=rm, variable_space=vs)

    parsed, _dropped = parse_breakpoint_defs(defs)
    engine = EdgeBreakpointEngine(parsed) if parsed else None

    scheduler = ScannerScheduler(
        event_bus=bus,
        registry=registry,
        evaluator=evaluator,
        variable_space=vs,
        resource_manager=rm,
        step_executor=_FakeStepExecutor(),  # type: ignore[arg-type]
        breakpoint_engine=engine,
    )

    steps = ("s1", "s2", "s3", "s4")
    pairs: list[tuple[str, Condition | None]] = [("s1", None)]
    for prev, cur in zip(steps, steps[1:], strict=False):
        pairs.append((cur, Condition(step=prev, status="PASSED")))
    for sid, cond in pairs:
        registry.register(sid, cond)
    scheduler.compile_plan(pairs)

    worker = _InlineWorker(bus, registry, vs)
    return bus, scheduler, worker


def _record_hits(bus: _InlineEventBus) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    bus.subscribe(EventType.BREAKPOINT_HIT, lambda ev: hits.append(dict(ev.data)))
    return hits


async def _drive_to(
    scheduler: ScannerScheduler, worker: _InlineWorker, target: str,
) -> None:
    """Run emergency scans until `target` has executed (a serial chain advances
    one PASSED step per scan). Deterministic — no sleeps, no wall clock."""
    for _ in range(8):
        if target in worker.executed:
            return
        await scheduler._emergency_scan()
    raise AssertionError(f"{target} never executed; ran {worker.executed}")


@pytest.mark.asyncio
async def test_emergency_scan_step_breakpoint_pauses_instead_of_dispatching() -> None:
    """F2 B1: a step breakpoint on s3 pauses the WATCHDOG path too — s3 is never
    dispatched past its breakpoint; BREAKPOINT_HIT fires for s3 with a snapshot;
    resume then lets the plan complete via the emergency-scan path."""
    bus, scheduler, worker = _serial_world(
        [{"id": "bp-s3", "kind": "step", "target": "s3", "enabled": True}]
    )
    hits = _record_hits(bus)

    # Run s1, s2 to completion via the emergency-scan (watchdog) path ONLY.
    await _drive_to(scheduler, worker, "s2")
    assert worker.executed == ["s1", "s2"]

    # s3 is now ready and the reactive path has NOT touched it. Drive the
    # emergency scan directly — it MUST hit the breakpoint, not run s3.
    await scheduler._emergency_scan()

    assert scheduler.is_paused is True
    assert len(hits) == 1
    assert hits[0]["step_id"] == "s3"
    assert hits[0]["breakpoint_id"] == "bp-s3"
    assert hits[0]["kind"] == "step"
    assert hits[0]["target"] == "s3"
    scope = hits[0]["variables"]["scope"]
    assert scope.get("s2_done") is True
    assert "s3_done" not in scope  # snapshot taken BEFORE s3 runs
    # s3 must NOT have started.
    assert "s3" not in worker.executed
    assert worker.run_counts.get("s3", 0) == 0
    assert scheduler._registry.get_status("s3") == StepStatus.PENDING
    assert "s3" not in _started(bus)

    # While paused, further emergency scans dispatch NOTHING.
    await scheduler._emergency_scan()
    assert worker.executed == ["s1", "s2"]

    # Operator resumes; the emergency-scan path then completes the plan.
    scheduler.resume()
    await _drive_to(scheduler, worker, "s4")
    assert worker.executed == ["s1", "s2", "s3", "s4"]
    assert worker.run_counts == dict.fromkeys(["s1", "s2", "s3", "s4"], 1)


@pytest.mark.asyncio
async def test_emergency_scan_condition_breakpoint_fires_on_correct_step() -> None:
    """F2 B1 deterministic cover for the previously-flaky condition scenario:
    'ripple_mv > 50' (set by s2) must suspend at s3 through the emergency-scan
    path — BREAKPOINT_HIT reports s3, NOT s4."""
    defs = [{
        "id": "bp-ripple", "kind": "condition", "target": "*",
        "condition": "ripple_mv > 50", "enabled": True,
    }]
    bus, scheduler, worker = _serial_world(defs)
    hits = _record_hits(bus)

    await _drive_to(scheduler, worker, "s2")  # s2 sets ripple_mv=85
    await scheduler._emergency_scan()        # s3 ready -> must hit at s3

    assert scheduler.is_paused is True
    assert len(hits) == 1
    assert hits[0]["step_id"] == "s3", (
        f"condition breakpoint must fire on s3, got {hits[0]['step_id']!r}"
    )
    assert hits[0]["kind"] == "condition"
    assert hits[0]["variables"]["scope"].get("ripple_mv") == 85
    assert "s3" not in worker.executed
    assert "s4" not in worker.executed

    scheduler.resume()
    await _drive_to(scheduler, worker, "s4")
    assert worker.executed == ["s1", "s2", "s3", "s4"]
    assert len(hits) == 1  # fired exactly once, on s3


@pytest.mark.asyncio
async def test_emergency_scan_does_not_dispatch_while_paused() -> None:
    """MINOR pause-semantics: while paused, an emergency scan must not dispatch
    even an independent, ready, non-breakpointed step."""
    bus, scheduler, worker = _serial_world(None)  # no breakpoints

    scheduler.pause()
    assert scheduler.is_paused is True

    await scheduler._emergency_scan()
    await scheduler._emergency_scan()

    assert worker.executed == []
    assert _started(bus) == []
    assert scheduler._registry.get_status("s1") == StepStatus.PENDING

    # Resume -> the watchdog path dispatches normally.
    scheduler.resume()
    await _drive_to(scheduler, worker, "s1")
    assert worker.executed == ["s1"]



# ---------------------------------------------------------------------------
# Worker control — sync_breakpoints arms the edge engine
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Event-bus codec round-trip — BREAKPOINT_HIT must survive serialize/deserialize
# ---------------------------------------------------------------------------


def test_breakpoint_hit_round_trips_through_event_data_classes() -> None:
    """A BREAKPOINT_HIT payload with a variable snapshot is losslessly rebuilt.

    Given: the payload the edge scheduler publishes (asdict(BreakpointHitData)),
    When:  it goes over the wire as JSON (jetstream forwarder format) and is
           rebuilt via EVENT_DATA_CLASSES,
    Then:  the result is a BreakpointHitData with the variable snapshot intact.
    """
    from dataclasses import asdict

    from shared.events import (
        EVENT_DATA_CLASSES,
        BreakpointHitData,
    )

    data_cls = EVENT_DATA_CLASSES[EventType.BREAKPOINT_HIT]
    assert data_cls is BreakpointHitData

    snapshot = {
        "scope": {"voltage": 3.3, "current": 0.5, "label": "rail-1", "ok": True},
        "steps": {"s1": {"result": "passed"}},
        "loop": {},
    }
    published = asdict(
        BreakpointHitData(
            breakpoint_id="bp1",
            kind="step",
            target="s2",
            step_id="s2",
            variables=snapshot,
        )
    )

    # Wire format matches jetstream_worker._setup_status_forwarding.
    wire = json.dumps({"type": EventType.BREAKPOINT_HIT.value, "data": published})
    received = json.loads(wire)

    rebuilt = EVENT_DATA_CLASSES[EventType(received["type"])](**received["data"])

    assert isinstance(rebuilt, BreakpointHitData)
    assert rebuilt.breakpoint_id == "bp1"
    assert rebuilt.kind == "step"
    assert rebuilt.target == "s2"
    assert rebuilt.step_id == "s2"
    assert rebuilt.run_id is None
    assert rebuilt.variables == snapshot


class FakeControlMsg:
    """Fake core-NATS control message supporting request-reply."""

    def __init__(self, payload: dict[str, Any], subject: str = "ate.control.exec-1") -> None:
        self.data = json.dumps(payload).encode("utf-8")
        self.subject = subject
        self.replies: list[dict[str, Any]] = []

    async def respond(self, data: bytes) -> None:
        self.replies.append(json.loads(data.decode("utf-8")))


class _FakeTaskMsg:
    def __init__(self, data: bytes, headers: dict[str, str] | None = None) -> None:
        self.data = data
        self.headers = headers
        self.acked = False

    async def ack(self) -> None:
        self.acked = True

    async def nak(self) -> None:
        pass


def _mock_nc() -> MagicMock:
    mock_nc = MagicMock()
    mock_nc.is_connected = True
    mock_nc.publish = AsyncMock()
    mock_nc.close = AsyncMock()
    sub = MagicMock()
    sub.unsubscribe = AsyncMock()
    mock_nc.subscribe = AsyncMock(return_value=sub)
    js = MagicMock()
    kv = MagicMock()
    kv.put = AsyncMock(return_value=1)
    js.key_value = AsyncMock(return_value=kv)
    js.pull_subscribe = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=js)
    return mock_nc


def _gated_plan() -> YamlPlan:
    # Preconditions that never satisfy → steps register but don't auto-run,
    # keeping the booted worker free of script execution.
    return YamlPlan(
        name="t",
        version="1.0",
        steps=[
            YamlStep(id="s1", script="s1.py", preconditions=["gate"]),
            YamlStep(id="s2", script="s2.py", preconditions=["gate"]),
        ],
    )


async def _boot_worker(tmp_path: Path) -> JetStreamWorker:
    from dataclasses import asdict
    from enum import Enum

    def _enum_value(o: Any) -> Any:
        if isinstance(o, Enum):
            return o.value
        raise TypeError(type(o).__name__)

    nc = _mock_nc()
    payload = json.dumps(asdict(_gated_plan()), default=_enum_value).encode("utf-8")
    psub = MagicMock()
    psub.fetch = AsyncMock(return_value=[_FakeTaskMsg(payload, {"execution_id": "exec-1"})])
    nc.jetstream.return_value.pull_subscribe = AsyncMock(return_value=psub)

    worker = JetStreamWorker(worker_id_path=str(tmp_path / "worker_id"))
    await worker.start(nc=nc)
    assert await worker.pull_and_process_one(timeout=1.0) is True
    return worker


@pytest.mark.asyncio
async def test_worker_sync_breakpoints_arms_engine(tmp_path: Path) -> None:
    worker = await _boot_worker(tmp_path)
    try:
        scheduler = worker._current_scheduler
        assert scheduler is not None
        assert scheduler._breakpoint_engine is None  # no defs in task payload

        msg = FakeControlMsg({
            "action": "sync_breakpoints",
            "breakpoints": [
                {"id": "bp1", "kind": "step", "target": "s2"},
                {"id": "bad", "kind": "step"},  # malformed — dropped, no hang
            ],
        })
        await worker._on_control_message(msg)

        assert msg.replies == [{
            "status": "ok",
            "action": "sync_breakpoints",
            "armed": 1,
            "dropped": 1,
            "phase": "active",
        }]
        engine = scheduler._breakpoint_engine
        assert engine is not None
        assert [bp.id for bp in engine.breakpoints] == ["bp1"]
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_breakpoints_in_task_payload_arm_at_bootstrap(tmp_path: Path) -> None:
    """Breakpoint defs carried on the task message arm the engine at boot."""
    from dataclasses import asdict
    from enum import Enum

    def _enum_value(o: Any) -> Any:
        if isinstance(o, Enum):
            return o.value
        raise TypeError(type(o).__name__)

    data = asdict(_gated_plan())
    data["breakpoints"] = [{"id": "bp1", "kind": "step", "target": "s2"}]
    payload = json.dumps(data, default=_enum_value).encode("utf-8")

    nc = _mock_nc()
    psub = MagicMock()
    psub.fetch = AsyncMock(return_value=[_FakeTaskMsg(payload, {"execution_id": "exec-1"})])
    nc.jetstream.return_value.pull_subscribe = AsyncMock(return_value=psub)

    worker = JetStreamWorker(worker_id_path=str(tmp_path / "worker_id"))
    await worker.start(nc=nc)
    try:
        assert await worker.pull_and_process_one(timeout=1.0) is True
        scheduler = worker._current_scheduler
        assert scheduler is not None
        assert scheduler._breakpoint_engine is not None
        assert [bp.id for bp in scheduler._breakpoint_engine.breakpoints] == ["bp1"]
    finally:
        await worker.stop()
