"""End-to-end closed loop B — edge breakpoints pause a real run and resume to
completion, plus the DSL ``type: breakpoint`` interactive gate (task 29).

Two production breakpoint paths are exercised with REAL services and only the
external hardware/NATS faked (no live instruments, no NATS server):

1. **Wire-def edge breakpoints** (cloud-pushed, edge-evaluated, T39/task 20):
   a real ``EdgeBreakpointEngine`` armed from wire dicts is consulted at the
   real ``ScannerScheduler`` step-dispatch gate
   (``_gate_before_dispatch`` → ``BREAKPOINT_HIT`` event → ``pause()`` /
   ``_pause_event`` / ``resume()`` — the SAME gate the reactive dispatch and
   the watchdog ``_emergency_scan`` both cross, and the same one T40 step-mode
   and DSL breakpoints use). A fake executor worker consumes ``STEP_STARTED``
   exactly
   like ``JetStreamWorker`` does in production. We assert the run suspends
   before the target step, the ``BREAKPOINT_HIT`` event carries the live
   variable snapshot, the scheduler is paused, and ``resume()`` lets the plan
   run to completion (every step exactly once). Covers ``step`` kind,
   ``condition`` kind, and a not-fired control.

2. **DSL ``type: breakpoint`` steps** driven through the real
   ``V32PlanDispatcher``: a false condition passes through WITHOUT the hook;
   a hit awaits an injected ``breakpoint_hook`` that suspends on the same
   pause/resume gate, then the plan completes. A ``PlanBootstrapper`` wiring
   test confirms production decodes wire defs into the armed engine.

Everything runs in the default suite (no integration marker, no skips, no
live NATS/hardware).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.scheduler.edge_breakpoints import (
    EdgeBreakpointEngine,
    parse_breakpoint_defs,
)
from ate_platform.scheduler.event_bus import EventBus, EventType
from ate_platform.scheduler.jetstream_worker import PlanBootstrapper
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
from ate_platform.scheduler.step_registry import StepRegistry
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import Condition
from shared.dsl import StepType, YamlPlan, YamlStep
from shared.events import Event
from shared.types import StepStatus

STEPS = ["s1", "s2", "s3", "s4"]  # serial chain


# ---------------------------------------------------------------------------
# Fake step executor — the scheduler only needs pool_stats() for straight-line
# steps (actual execution is driven by the worker on STEP_STARTED, exactly as
# JetStreamWorker does in production).
# ---------------------------------------------------------------------------


class FakeStepExecutor:
    def pool_stats(self) -> dict[str, Any]:
        return {"active": 0, "max": 4, "utilization": 0.0, "queued": 0}


class ExecutorWorker:
    """Test double for JetStreamWorker: STEP_STARTED → execute → report.

    Faithful to production: it executes every STEP_STARTED it receives for a
    still-PENDING step, exactly once. It has NO pause gate of its own — the
    scheduler withholds STEP_STARTED while a breakpoint is suspended (the
    reactive dispatch blocks on the pause event and the step stays in
    ``_pending_dispatch``, so the watchdog emergency-scan skips it). This is
    the same worker model as tests/platform/test_snapshot_restore_e2e.py.
    """

    def __init__(
        self,
        event_bus: EventBus,
        registry: StepRegistry,
        variable_space: VariableSpace,
    ) -> None:
        self.registry = registry
        self.variable_space = variable_space
        self.run_counts: dict[str, int] = {}
        self.executed: list[str] = []
        event_bus.subscribe(EventType.STEP_STARTED, self._on_step_started)

    def _on_step_started(self, event: Event) -> None:
        step_id = str(event.data.get("step_id"))
        if self.registry.get_status(step_id) != StepStatus.PENDING:
            return  # already executed (dedup any duplicate dispatch)
        self.run_counts[step_id] = self.run_counts.get(step_id, 0) + 1
        self.executed.append(step_id)
        self.registry.update_status(step_id, StepStatus.RUNNING)
        # Real step side effects: publish scope variables (the breakpoint
        # condition reads these via the variable snapshot).
        self.variable_space.set(f"scope.{step_id}_done", True)
        self.variable_space.set("scope.last_step", step_id)
        if step_id == "s1":
            self.variable_space.set("scope.voltage", 3.31)
        if step_id == "s2":
            self.variable_space.set("scope.ripple_mv", 85)
        self.registry.update_status(step_id, StepStatus.PASSED)


class BreakpointRecorder:
    """Captures BREAKPOINT_HIT events for assertions."""

    def __init__(self, event_bus: EventBus) -> None:
        self.hits: list[dict[str, Any]] = []
        event_bus.subscribe(EventType.BREAKPOINT_HIT, self._on_hit)

    def _on_hit(self, event: Event) -> None:
        self.hits.append(dict(event.data))


class World:
    """A fully-wired scheduler assembly with an armed edge-breakpoint engine."""

    def __init__(
        self,
        breakpoint_defs: list[dict[str, Any]] | None,
    ) -> None:
        self.event_bus = EventBus()
        self.registry = StepRegistry(event_bus=self.event_bus)
        self.variable_space = VariableSpace(event_bus=self.event_bus)
        self.resource_manager = ResourceManager(event_bus=self.event_bus)
        self.evaluator = ConditionEvaluator(
            {},
            resource_manager=self.resource_manager,
            variable_space=self.variable_space,
        )

        edge_bps, dropped = parse_breakpoint_defs(breakpoint_defs)
        self.dropped = dropped
        self.engine: EdgeBreakpointEngine | None = (
            EdgeBreakpointEngine(edge_bps) if edge_bps else None
        )

        self.scheduler = ScannerScheduler(
            event_bus=self.event_bus,
            registry=self.registry,
            evaluator=self.evaluator,
            variable_space=self.variable_space,
            resource_manager=self.resource_manager,
            scan_interval=0.02,
            step_executor=FakeStepExecutor(),  # type: ignore[arg-type]
            breakpoint_engine=self.engine,
        )

        pairs: list[tuple[str, Condition | None]] = [("s1", None)]
        for prev, cur in zip(STEPS, STEPS[1:], strict=False):
            pairs.append((cur, Condition(step=prev, status="PASSED")))
        for step_id, condition in pairs:
            self.registry.register(step_id, condition)
        self.scheduler.compile_plan(pairs)

        self.recorder = BreakpointRecorder(self.event_bus)
        self.worker = ExecutorWorker(
            self.event_bus, self.registry, self.variable_space,
        )

    async def start(self) -> None:
        await self.event_bus.start()
        await self.scheduler.start()

    async def stop(self) -> None:
        await self.scheduler.stop()
        await self.event_bus.stop()


async def _wait_until(pred: Any, description: str, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timeout waiting for: {description}")


def _step_breakpoint(target: str) -> dict[str, Any]:
    return {"id": f"bp-{target}", "kind": "step", "target": target, "enabled": True}


# ---------------------------------------------------------------------------
# Edge breakpoint: step kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_breakpoint_pauses_then_resumes_to_completion() -> None:
    """A step breakpoint suspends before s3, emits BREAKPOINT_HIT+snapshot, resumes."""
    world = World([_step_breakpoint("s3")])
    await world.start()
    try:
        # s1, s2 run; s3 must suspend at the dispatch gate.
        await _wait_until(
            lambda: len(world.recorder.hits) == 1, "BREAKPOINT_HIT for s3",
        )
        await _wait_until(
            lambda: world.registry.get_status("s2") == StepStatus.PASSED,
            "s2 PASSED before the paused s3",
        )

        # Scheduler is paused and s3 has NOT executed.
        assert world.scheduler.is_paused is True
        assert world.registry.get_status("s3") == StepStatus.PENDING
        assert world.worker.run_counts.get("s3", 0) == 0

        # The hit event identifies the breakpoint and carries a live snapshot.
        hit = world.recorder.hits[0]
        assert hit["breakpoint_id"] == "bp-s3"
        assert hit["kind"] == "step"
        assert hit["target"] == "s3"
        assert hit["step_id"] == "s3"
        scope = hit["variables"].get("scope", {})
        assert scope.get("s1_done") is True
        assert scope.get("s2_done") is True
        assert scope.get("voltage") == 3.31
        assert "s3_done" not in scope  # snapshot taken BEFORE s3 runs

        # Operator resumes (cloud control command in production). The suspended
        # reactive dispatch of s3 is released and the plan runs to completion.
        world.scheduler.resume()

        await _wait_until(
            lambda: all(
                world.registry.get_status(s) == StepStatus.PASSED for s in STEPS
            ),
            "all 4 steps PASSED after resume",
        )
        # Every step executed exactly once.
        assert world.worker.run_counts == dict.fromkeys(STEPS, 1)
        assert world.scheduler.is_paused is False
    finally:
        await world.stop()


# ---------------------------------------------------------------------------
# Edge breakpoint: condition kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_condition_breakpoint_fires_on_scope_variable() -> None:
    """A condition-kind breakpoint ('ripple_mv > 50') fires at the gate and resumes."""
    defs = [{
        "id": "bp-ripple",
        "kind": "condition",
        "target": "*",
        "condition": "ripple_mv > 50",
        "enabled": True,
    }]
    world = World(defs)
    await world.start()
    try:
        await _wait_until(
            lambda: len(world.recorder.hits) == 1, "condition BREAKPOINT_HIT",
        )
        hit = world.recorder.hits[0]
        assert hit["kind"] == "condition"
        # ripple_mv is set by s2, so the condition is first observable at s3.
        assert hit["step_id"] == "s3"
        assert hit["variables"]["scope"].get("ripple_mv") == 85
        assert world.scheduler.is_paused is True

        world.scheduler.resume()
        await _wait_until(
            lambda: all(
                world.registry.get_status(s) == StepStatus.PASSED for s in STEPS
            ),
            "all steps PASSED after resume",
        )
        assert world.worker.run_counts == dict.fromkeys(STEPS, 1)
    finally:
        await world.stop()


# ---------------------------------------------------------------------------
# F2 BLOCKER B1 (deterministic regression): the watchdog emergency-scan path
# must run the SAME breakpoint/pause gate as reactive dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emergency_scan_cannot_bypass_condition_breakpoint() -> None:
    """The emergency-scan fallback suspends at the conditional breakpoint on s3.

    Deterministic (no wall-clock races): the scheduler is NOT started, so there
    is no watchdog scan loop and no reactive dispatch task — simulating the
    event-loop contention where reactive dispatch is starved and only the
    emergency scan reaches the step. We drive ``_emergency_scan()`` DIRECTLY.
    The real queued EventBus runs (so the ExecutorWorker consumes STEP_STARTED
    exactly like JetStreamWorker); after each scan we publish a sentinel event
    behind the queued events and await its delivery as a deterministic barrier,
    so no ``sleep``/timing is involved.

    The pre-fix bug dispatched s3 ungated via the emergency path and the
    breakpoint was first observed on s4 (``hit["step_id"] == "s4"``).
    """
    defs = [{
        "id": "bp-ripple",
        "kind": "condition",
        "target": "*",
        "condition": "ripple_mv > 50",
        "enabled": True,
    }]
    world = World(defs)
    # Real queued bus with its delivery task, but the scheduler stays stopped.
    await world.event_bus.start()
    _drained = asyncio.Event()
    world.event_bus.subscribe(
        EventType.HEARTBEAT_LOST, lambda _ev: _drained.set()
    )

    async def _settle() -> None:
        """Barrier: resolve only after every event queued before now is delivered."""
        _drained.clear()
        await world.event_bus.publish(EventType.HEARTBEAT_LOST, {"drain": True})
        await asyncio.wait_for(_drained.wait(), timeout=5.0)

    try:
        # Advance the chain through the watchdog path ONLY (one PASSED step per
        # scan). s2 sets ripple_mv=85, making s3 the first observable hit step.
        for _ in range(4):
            await world.scheduler._emergency_scan()  # noqa: SLF001
            await _settle()
            if world.registry.get_status("s2") == StepStatus.PASSED:
                break
        assert world.worker.executed == ["s1", "s2"]

        # s3 is ready; reactive dispatch has NOT run (scheduler never started).
        # The emergency scan MUST suspend at the breakpoint on s3 — not run s3
        # and fire late on s4.
        await world.scheduler._emergency_scan()  # noqa: SLF001
        await _settle()

        assert world.scheduler.is_paused is True
        assert len(world.recorder.hits) == 1
        hit = world.recorder.hits[0]
        assert hit["kind"] == "condition"
        assert hit["step_id"] == "s3", (
            f"conditional breakpoint must fire on s3, got {hit['step_id']!r}"
        )
        assert hit["variables"]["scope"].get("ripple_mv") == 85
        assert "s3_done" not in hit["variables"]["scope"]  # snapshot pre-s3
        assert "s3" not in world.worker.executed
        assert world.registry.get_status("s3") == StepStatus.PENDING

        # While paused the emergency scan dispatches nothing (MINOR pause fix).
        await world.scheduler._emergency_scan()  # noqa: SLF001
        await _settle()
        assert world.worker.executed == ["s1", "s2"]

        # Operator resumes; drive the watchdog path on to completion.
        world.scheduler.resume()
        for _ in range(6):
            await world.scheduler._emergency_scan()  # noqa: SLF001
            await _settle()
            if all(
                world.registry.get_status(s) == StepStatus.PASSED for s in STEPS
            ):
                break

        assert world.worker.run_counts == dict.fromkeys(STEPS, 1)
        assert len(world.recorder.hits) == 1  # fired exactly once, on s3
        assert world.scheduler.is_paused is False
    finally:
        await world.event_bus.stop()


@pytest.mark.asyncio
async def test_false_condition_breakpoint_never_pauses() -> None:
    """A condition that stays false must NOT fire — the plan runs straight through."""
    defs = [{
        "id": "bp-never",
        "kind": "condition",
        "target": "*",
        "condition": "ripple_mv > 9999",  # never true
        "enabled": True,
    }]
    world = World(defs)
    await world.start()
    try:
        await _wait_until(
            lambda: all(
                world.registry.get_status(s) == StepStatus.PASSED for s in STEPS
            ),
            "all steps PASSED with no suspension",
        )
        assert world.recorder.hits == []
        assert world.scheduler.is_paused is False
        assert world.worker.run_counts == dict.fromkeys(STEPS, 1)
    finally:
        await world.stop()


# ---------------------------------------------------------------------------
# Malformed wire defs are dropped (never block a run)
# ---------------------------------------------------------------------------


def test_malformed_breakpoint_defs_are_dropped() -> None:
    """parse_breakpoint_defs tolerantly skips bad entries; the valid one survives."""
    defs = [
        {"id": "bp-ok", "kind": "step", "target": "s2", "enabled": True},
        {"id": "bp-bad-kind", "kind": "teleport", "target": "s3"},  # unknown kind
        {"kind": "step", "target": "s3"},  # missing id
        "not-a-dict",
    ]
    bps, dropped = parse_breakpoint_defs(defs)
    assert dropped == 3
    assert [b.id for b in bps] == ["bp-ok"]


# ---------------------------------------------------------------------------
# PlanBootstrapper wiring: cloud wire defs → armed engine on the scheduler
# ---------------------------------------------------------------------------


def test_bootstrapper_decodes_wire_defs_into_armed_engine() -> None:
    """Production assembly: PlanBootstrapper arms the scheduler breakpoint engine."""
    plan = YamlPlan(
        name="bp-wiring",
        version="1.0",
        steps=[YamlStep(id="s1", script="noop.py")],
    )
    defs = [
        {"id": "bp-s1", "kind": "step", "target": "s1", "enabled": True},
        {"id": "bp-bad", "kind": "nope", "target": "s1"},  # dropped, tolerated
    ]
    bootstrapper = PlanBootstrapper(plan, breakpoint_defs=defs)
    scheduler = bootstrapper.bootstrap()

    assert bootstrapper._breakpoint_engine is not None  # noqa: SLF001
    armed = bootstrapper._breakpoint_engine.breakpoints  # noqa: SLF001
    assert [b.id for b in armed] == ["bp-s1"]
    # The same engine instance is the one the scheduler consults at the gate.
    assert scheduler._breakpoint_engine is bootstrapper._breakpoint_engine  # noqa: SLF001


# ---------------------------------------------------------------------------
# DSL type: breakpoint steps through the real V32PlanDispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dsl_breakpoint_steps_suspend_and_pass_through() -> None:
    """V32PlanDispatcher: false condition passes through; a hit awaits the hook."""
    from ate_platform.executor.v32_dispatcher import V32PlanDispatcher

    hook_calls: list[str] = []
    release = asyncio.Event()
    suspended = asyncio.Event()

    async def breakpoint_hook(step: YamlStep) -> None:
        # Mirrors ScannerScheduler.execute_loop_step._breakpoint_hook:
        # pause, block until the operator resumes.
        hook_calls.append(step.id)
        suspended.set()
        await release.wait()

    plan = YamlPlan(
        name="dsl-bp",
        version="3.2",
        steps=[
            YamlStep(id="setup", script="setup.py"),
            # False condition → pass-through, hook MUST NOT be called.
            YamlStep(
                id="bp_skip",
                type=StepType.BREAKPOINT,
                condition="False",
            ),
            # Unconditional breakpoint → suspends on the hook until resumed.
            YamlStep(id="bp_halt", type=StepType.BREAKPOINT),
            YamlStep(id="teardown", script="teardown.py", preconditions=["bp_halt"]),
        ],
    )
    dispatcher = V32PlanDispatcher(
        plan, simulation=True, breakpoint_hook=breakpoint_hook,
    )

    task = asyncio.create_task(dispatcher.run())

    # Wait until the run suspends at bp_halt.
    await asyncio.wait_for(suspended.wait(), timeout=5.0)
    assert hook_calls == ["bp_halt"], "false-condition breakpoint must not invoke hook"
    assert not task.done(), "run must be suspended at the breakpoint"

    # Operator resumes → the dispatcher proceeds to completion.
    release.set()
    outcomes = await asyncio.wait_for(task, timeout=5.0)

    by_id = {o.step_id: o for o in outcomes}
    assert by_id["bp_skip"].status == "PASS"
    assert "condition false" in by_id["bp_skip"].detail
    assert by_id["bp_halt"].status == "PASS"
    assert "resumed" in by_id["bp_halt"].detail
    assert by_id["setup"].status == "PASS"
    assert by_id["teardown"].status == "PASS"
