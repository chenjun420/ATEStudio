"""Tests for the DSL ``type: breakpoint`` step (plan task 18).

Covers end-to-end across the EDGE engine:
- parser: breakpoint parses with and without an optional ``condition``;
  a breakpoint without ``id`` is a malformed step → ValueError (never a
  silent SKIP)
- compiler: a breakpoint compiles to a BREAKPOINT CompiledStep carrying
  its condition
- v32 semantic dispatcher: breakpoint is a real dispatched type (never
  "Unsupported step type"); suspends via an injected pause hook and resumes
  on signal; condition-false does not suspend; no hook (headless/unattended)
  is a no-op pass-through that cannot hang
- LoopExecutor (production loop path): breakpoint inside a loop hits the
  injected pause gate; condition-false does not; no hook passes through
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ate_platform.dsl.parser import YamlParser
from ate_platform.executor.loop_executor import LoopExecutor
from ate_platform.executor.v32_dispatcher import V32PlanDispatcher
from ate_platform.scheduler.compiler import SequenceCompiler
from ate_platform.scheduler.event_bus import EventBus
from ate_platform.scheduler.variable_space import VariableSpace
from shared.dsl import LoopType, StepType, YamlLoop, YamlPlan, YamlStep
from shared.types import StepResult, StepStatus


def _write(tmp_path: Path, steps_yaml: str) -> Path:
    content = (
        'name: bp_plan\nversion: "3.2"\nscope: production\nsteps:\n' + steps_yaml
    )
    yaml_file = tmp_path / "bp_plan.yaml"
    yaml_file.write_text(content, encoding="utf-8")
    return yaml_file


def _bp_step(step_id: str, *, condition: str | None = None) -> YamlStep:
    return YamlStep(id=step_id, type=StepType.BREAKPOINT, condition=condition)


def _plan(steps: list[Any]) -> YamlPlan:
    return YamlPlan(name="bp", version="3.2", steps=steps)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestBreakpointParse:
    def test_parse_with_condition(self, tmp_path: Path) -> None:
        f = _write(
            tmp_path,
            '  - id: bp1\n    type: breakpoint\n    condition: "${scope.count} > 3"\n',
        )
        plan = YamlParser().parse(f)
        step = plan.steps[0]
        assert isinstance(step, YamlStep)
        assert step.type is StepType.BREAKPOINT
        assert step.condition == "${scope.count} > 3"

    def test_parse_without_condition(self, tmp_path: Path) -> None:
        f = _write(tmp_path, '  - id: bp1\n    type: breakpoint\n')
        plan = YamlParser().parse(f)
        step = plan.steps[0]
        assert isinstance(step, YamlStep)
        assert step.type is StepType.BREAKPOINT
        assert step.condition is None

    def test_malformed_missing_id_raises(self, tmp_path: Path) -> None:
        # A breakpoint with no id is malformed — must raise, never silently skip.
        f = _write(tmp_path, '  - type: breakpoint\n')
        with pytest.raises(ValueError, match="[Bb]reakpoint missing required field: 'id'"):
            YamlParser().parse(f)

    def test_non_string_condition_raises(self, tmp_path: Path) -> None:
        f = _write(tmp_path, '  - id: bp1\n    type: breakpoint\n    condition: 123\n')
        with pytest.raises(ValueError, match="condition"):
            YamlParser().parse(f)


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------


class TestBreakpointCompile:
    def test_compiles_to_breakpoint_node_with_condition(self) -> None:
        plan = _plan([_bp_step("bp1", condition="${scope.ready} == True")])
        nodes = SequenceCompiler().compile(plan)
        assert len(nodes) == 1
        node = nodes[0]
        assert node.type is StepType.BREAKPOINT
        assert node.condition == "${scope.ready} == True"
        assert node.id == "bp1"

    def test_compiles_unconditional_breakpoint(self) -> None:
        plan = _plan([_bp_step("bp1")])
        nodes = SequenceCompiler().compile(plan)
        assert nodes[0].type is StepType.BREAKPOINT
        assert nodes[0].condition is None


# ---------------------------------------------------------------------------
# v32 semantic dispatcher
# ---------------------------------------------------------------------------


class TestBreakpointDispatcher:
    def test_no_gate_headless_passes_through_without_hanging(self) -> None:
        """No breakpoint hook (headless/unattended) → PASS pass-through, fast."""
        plan = _plan([_bp_step("bp1"), _bp_step("bp2", condition="False")])

        async def run() -> list:
            return await asyncio.wait_for(V32PlanDispatcher(plan).run(), timeout=5.0)

        outcomes = asyncio.run(run())
        assert [o.status for o in outcomes] == ["PASS", "PASS"]
        assert all(o.step_type == "breakpoint" for o in outcomes)
        assert not any("Unsupported" in o.detail for o in outcomes)
        assert "pass-through" in outcomes[0].detail

    def test_dispatcher_does_not_report_unsupported(self) -> None:
        plan = _plan([_bp_step("bp1")])
        outcomes = asyncio.run(V32PlanDispatcher(plan).run())
        assert outcomes[0].step_type == "breakpoint"
        assert "Unsupported step type" not in outcomes[0].detail

    def test_hit_suspends_then_resumes_on_signal(self) -> None:
        """Unconditional breakpoint with a hook: hook is awaited (suspend)
        and execution resumes after it returns (resume signal)."""
        plan = _plan([_bp_step("bp1")])
        hits: list[str] = []
        resumed = asyncio.Event()

        async def hook(step: YamlStep) -> None:
            hits.append(step.id)
            # Simulate the scheduler pause gate blocking until operator resume.
            await resumed.wait()

        async def run() -> list:
            dispatcher = V32PlanDispatcher(plan, breakpoint_hook=hook)
            task = asyncio.ensure_future(dispatcher.run())
            await asyncio.sleep(0.05)
            assert not task.done(), "dispatcher should be suspended at the breakpoint"
            assert hits == ["bp1"]
            resumed.set()  # operator resume signal
            return await task

        outcomes = asyncio.run(run())
        assert hits == ["bp1"]
        assert outcomes[0].status == "PASS"
        assert "suspended then resumed" in outcomes[0].detail

    def test_condition_true_suspends(self) -> None:
        plan = _plan([_bp_step("bp1", condition="True")])
        hits: list[str] = []

        async def hook(step: YamlStep) -> None:
            hits.append(step.id)

        async def run() -> list:
            return await V32PlanDispatcher(plan, breakpoint_hook=hook).run()

        outcomes = asyncio.run(run())
        assert hits == ["bp1"]
        assert outcomes[0].status == "PASS"

    def test_condition_false_does_not_suspend(self) -> None:
        plan = _plan([_bp_step("bp1", condition="False")])
        hits: list[str] = []

        async def hook(step: YamlStep) -> None:
            hits.append(step.id)  # must never be called

        async def run() -> list:
            return await asyncio.wait_for(
                V32PlanDispatcher(plan, breakpoint_hook=hook).run(), timeout=5.0
            )

        outcomes = asyncio.run(run())
        assert hits == []
        assert outcomes[0].status == "PASS"
        assert "condition false" in outcomes[0].detail


# ---------------------------------------------------------------------------
# LoopExecutor (production loop path)
# ---------------------------------------------------------------------------


class _FakeStepExecutor:
    """Minimal StepExecutor stand-in: scripts always pass."""

    async def execute_async(self, **kwargs: Any) -> StepResult:
        return StepResult(status=StepStatus.PASSED)

    async def execute_batch(self, tasks: Any, max_concurrency: Any = None) -> list:
        return [StepResult(status=StepStatus.PASSED) for _ in tasks]


class TestBreakpointInLoop:
    def test_breakpoint_hits_gate_in_serial_loop(self) -> None:
        bus = EventBus()
        vs = VariableSpace(event_bus=bus)
        hits: list[str] = []
        resumed = asyncio.Event()

        async def hook(step: YamlStep) -> None:
            hits.append(step.id)
            await resumed.wait()

        loop = YamlLoop(
            id="l1",
            loop_type=LoopType.FOR,
            count=1,
            steps=[
                _bp_step("bp1"),
                YamlStep(id="s1", script="s.py"),
            ],
        )
        executor = LoopExecutor(
            _FakeStepExecutor(), event_bus=bus, variable_space=vs, breakpoint_hook=hook
        )

        async def run() -> Any:
            task = asyncio.ensure_future(executor.execute_loop(loop))
            await asyncio.sleep(0.05)
            assert not task.done(), "loop should be suspended at breakpoint"
            resumed.set()
            return await task

        result = asyncio.run(run())
        assert hits == ["bp1"]
        assert result.status == StepStatus.PASSED

    def test_no_gate_loop_passes_through(self) -> None:
        """Headless loop with a breakpoint and no hook completes (never hangs)."""
        bus = EventBus()
        vs = VariableSpace(event_bus=bus)
        loop = YamlLoop(
            id="l1",
            loop_type=LoopType.FOR,
            count=2,
            steps=[_bp_step("bp1"), YamlStep(id="s1", script="s.py")],
        )
        executor = LoopExecutor(_FakeStepExecutor(), event_bus=bus, variable_space=vs)

        async def run() -> Any:
            return await asyncio.wait_for(executor.execute_loop(loop), timeout=5.0)

        result = asyncio.run(run())
        assert result.status == StepStatus.PASSED
        assert result.total_iterations == 2

    def test_condition_false_does_not_suspend_in_loop(self) -> None:
        bus = EventBus()
        vs = VariableSpace(event_bus=bus)
        vs.set("scope.ready", True)
        hits: list[str] = []

        async def hook(step: YamlStep) -> None:
            hits.append(step.id)

        loop = YamlLoop(
            id="l1",
            loop_type=LoopType.FOR,
            count=1,
            steps=[
                _bp_step("bp_false", condition="${scope.ready} == False"),
                _bp_step("bp_true", condition="${scope.ready} == True"),
            ],
        )
        executor = LoopExecutor(
            _FakeStepExecutor(), event_bus=bus, variable_space=vs, breakpoint_hook=hook
        )

        async def run() -> Any:
            return await asyncio.wait_for(executor.execute_loop(loop), timeout=5.0)

        asyncio.run(run())
        assert hits == ["bp_true"]
