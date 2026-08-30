"""Tests for V32PlanDispatcher — DSL v3.2 step semantics executor.

Covers:
- fixture_control dispatch: clamp → release ordering, set_route/read_sensor,
  missing action/fixture_id → FAIL, plan-level fixture_id fallback
- barrier dispatch: all UUTs arrive → PASS; missing UUT → FAIL
- action/script dispatch: retry honored, on_failure abort/continue/skip
- loop container: children expanded per iteration with iterN step_ids
- depends_on topo ordering + unmet dependency → BLOCKED
- JUnit builder (build_junit_xml_from_outcomes) mapping
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ate_platform.executor.v32_dispatcher import StepOutcome, V32PlanDispatcher
from ate_platform.simulation.headless_runner import build_junit_xml_from_outcomes
from shared.dsl import LoopType, StepType, YamlLoop, YamlPlan, YamlStep


def _step(
    step_id: str,
    *,
    step_type: StepType | None = None,
    script: str = "",
    params: dict[str, Any] | None = None,
    depends_on: list[str] | None = None,
    action: str | None = None,
    fixture_id: str | None = None,
    barrier_name: str | None = None,
    retry: int = 0,
    on_failure: str | None = None,
) -> YamlStep:
    """Create a YamlStep with v3.2 fields."""
    return YamlStep(
        id=step_id,
        type=step_type,
        script=script,
        params=params or {},
        depends_on=depends_on or [],
        action=action,
        fixture_id=fixture_id,
        barrier_name=barrier_name,
        retry=retry,
        on_failure=on_failure,
    )


def _plan(
    steps: list[YamlStep | YamlLoop],
    *,
    uut_count: int = 1,
    fixture_id: str | None = None,
) -> YamlPlan:
    return YamlPlan(
        name="v32_test",
        version="3.2",
        uut_count=uut_count,
        fixture_id=fixture_id,
        steps=steps,
    )


def _run(dispatcher: V32PlanDispatcher) -> list[StepOutcome]:
    """Run an async dispatcher synchronously."""
    return asyncio.run(dispatcher.run())


# ---------------------------------------------------------------------------
# fixture_control
# ---------------------------------------------------------------------------


class TestFixtureControl:
    """Tests for fixture_control step dispatch."""

    def test_clamp_then_release_in_order(self) -> None:
        """fixture_control steps dispatch to FixtureController actions in order."""
        plan = _plan([
            _step("clamp", step_type=StepType.FIXTURE_CONTROL, action="clamp",
                  fixture_id="fx1"),
            _step("release", step_type=StepType.FIXTURE_CONTROL, action="release",
                  fixture_id="fx1", depends_on=["clamp"]),
        ])
        outcomes = _run(V32PlanDispatcher(plan))

        assert [o.status for o in outcomes] == ["PASS", "PASS"]
        assert outcomes[0].detail == "clamp on fixture 'fx1'"
        assert outcomes[1].detail == "release on fixture 'fx1'"
        # Reuse the dispatcher from the run for state inspection
        dispatcher = V32PlanDispatcher(plan)
        _run(dispatcher)
        assert dispatcher.fixtures["fx1"].get_state()["status"] == "idle"

    def test_missing_action_fails(self) -> None:
        """A fixture_control step without action → FAIL."""
        plan = _plan([
            _step("bad", step_type=StepType.FIXTURE_CONTROL, fixture_id="fx1"),
        ])
        outcomes = _run(V32PlanDispatcher(plan))
        assert outcomes[0].status == "FAIL"
        assert "action" in outcomes[0].detail

    def test_unknown_action_fails(self) -> None:
        """An unknown fixture action → FAIL with valid actions listed."""
        plan = _plan([
            _step("bad", step_type=StepType.FIXTURE_CONTROL, action="teleport",
                  fixture_id="fx1"),
        ])
        outcomes = _run(V32PlanDispatcher(plan))
        assert outcomes[0].status == "FAIL"
        assert "teleport" in outcomes[0].detail

    def test_missing_fixture_id_falls_back_to_plan(self) -> None:
        """Step without fixture_id uses plan-level fixture_id."""
        plan = _plan(
            [_step("clamp", step_type=StepType.FIXTURE_CONTROL, action="clamp")],
            fixture_id="plan_fx",
        )
        dispatcher = V32PlanDispatcher(plan)
        outcomes = _run(dispatcher)
        assert outcomes[0].status == "PASS"
        assert "plan_fx" in outcomes[0].detail

    def test_missing_fixture_id_everywhere_fails(self) -> None:
        """No fixture_id on step or plan → FAIL."""
        plan = _plan([
            _step("clamp", step_type=StepType.FIXTURE_CONTROL, action="clamp"),
        ])
        outcomes = _run(V32PlanDispatcher(plan))
        assert outcomes[0].status == "FAIL"

    def test_set_route_requires_relay(self) -> None:
        """set_route without params.relay_id → FAIL."""
        plan = _plan([
            _step("route", step_type=StepType.FIXTURE_CONTROL, action="set_route",
                  fixture_id="fx1"),
        ])
        outcomes = _run(V32PlanDispatcher(plan))
        assert outcomes[0].status == "FAIL"
        assert "relay_id" in outcomes[0].detail


# ---------------------------------------------------------------------------
# barrier
# ---------------------------------------------------------------------------


class TestBarrier:
    """Tests for barrier step dispatch via UUTManager."""

    def test_single_uut_barrier_passes(self) -> None:
        """Single UUT arrives → barrier passes immediately."""
        plan = _plan([
            _step("sync", step_type=StepType.BARRIER, barrier_name="b1"),
        ], uut_count=1)
        outcomes = _run(V32PlanDispatcher(plan))
        assert outcomes[0].status == "PASS"
        assert "reached by all UUTs" in outcomes[0].detail

    def test_multi_uut_barrier_all_arrive_passes(self) -> None:
        """All UUTs arrive concurrently → barrier passes."""
        plan = _plan([
            _step("sync", step_type=StepType.BARRIER, barrier_name="b1"),
        ], uut_count=3)
        outcomes = _run(V32PlanDispatcher(plan))
        assert outcomes[0].status == "PASS"

    def test_multi_uut_barrier_timeout_fails(self) -> None:
        """Barrier with UUT that never arrives → FAIL (timeout, §6.3.7)."""
        plan = _plan([
            _step("sync", step_type=StepType.BARRIER, barrier_name="b1"),
        ], uut_count=2)
        # UUT_1 never arrives (simulated missing UUT) → timeout
        outcomes = _run(
            V32PlanDispatcher(plan, uut_timeout=0.1, missing_uuts={"UUT_1"})
        )
        assert outcomes[0].status == "FAIL"
        assert "timed out" in outcomes[0].detail
        assert "UUT_1" in outcomes[0].detail

    def test_barrier_defaults_to_step_id(self) -> None:
        """Barrier without barrier_name uses step id."""
        plan = _plan([
            _step("sync_point", step_type=StepType.BARRIER),
        ], uut_count=1)
        outcomes = _run(V32PlanDispatcher(plan))
        assert outcomes[0].status == "PASS"
        assert "sync_point" in outcomes[0].detail


# ---------------------------------------------------------------------------
# script / retry / on_failure
# ---------------------------------------------------------------------------


class TestScriptRetryOnFailure:
    """Tests for action/script dispatch with retry and on_failure."""

    @pytest.mark.asyncio
    async def test_script_passes_first_attempt(self) -> None:
        """A successful script passes on attempt 1."""
        plan = _plan([_step("a1", step_type=StepType.ACTION, script="x.py")])
        outcomes = await V32PlanDispatcher(plan).run()
        assert outcomes[0].status == "PASS"
        assert outcomes[0].attempts == 1

    @pytest.mark.asyncio
    async def test_retry_then_pass(self) -> None:
        """Failure twice then success with retry=2."""
        plan = _plan([_step("a1", step_type=StepType.ACTION, script="x.py",
                            retry=2, on_failure="continue")])
        calls = {"n": 0}

        async def flaky(step: YamlStep) -> bool:
            calls["n"] += 1
            return calls["n"] >= 3  # fail, fail, pass

        outcomes = await V32PlanDispatcher(plan, script_executor=flaky).run()
        assert outcomes[0].status == "PASS"
        assert outcomes[0].attempts == 3

    @pytest.mark.asyncio
    async def test_retry_exhausted_aborts(self) -> None:
        """Retries exhausted + on_failure=abort → FAIL and subsequent steps skip."""
        plan = _plan([
            _step("a1", step_type=StepType.ACTION, script="x.py", retry=1),
            _step("a2", step_type=StepType.ACTION, script="y.py", depends_on=["a1"]),
        ])
        async def always_fail(step: YamlStep) -> bool:
            return False

        outcomes = await V32PlanDispatcher(plan, script_executor=always_fail).run()
        assert outcomes[0].status == "FAIL"
        assert outcomes[0].attempts == 2  # 1 + retry=1
        # a2 must not have run (abort default)
        assert len(outcomes) == 1

    @pytest.mark.asyncio
    async def test_on_failure_continue(self) -> None:
        """on_failure=continue → FAIL recorded but subsequent step runs."""
        plan = _plan([
            _step("a1", step_type=StepType.ACTION, script="x.py",
                  on_failure="continue"),
            _step("a2", step_type=StepType.ACTION, script="y.py", depends_on=["a1"]),
        ])
        async def fail_x_only(step: YamlStep) -> bool:
            return step.script != "x.py"

        outcomes = await V32PlanDispatcher(plan, script_executor=fail_x_only).run()
        assert [o.status for o in outcomes] == ["FAIL", "PASS"]

    @pytest.mark.asyncio
    async def test_on_failure_skip(self) -> None:
        """on_failure=skip → step marked SKIP, no abort."""
        plan = _plan([
            _step("a1", step_type=StepType.ACTION, script="x.py",
                  on_failure="skip"),
            _step("a2", step_type=StepType.ACTION, script="y.py", depends_on=["a1"]),
        ])
        async def fail_x_only(step: YamlStep) -> bool:
            return step.script != "x.py"

        outcomes = await V32PlanDispatcher(plan, script_executor=fail_x_only).run()
        assert [o.status for o in outcomes] == ["SKIP", "PASS"]


# ---------------------------------------------------------------------------
# loop / topo
# ---------------------------------------------------------------------------


class TestLoopAndTopology:
    """Tests for loop containers and depends_on ordering."""

    def test_loop_expands_children_per_iteration(self) -> None:
        """Loop with count=3 and 2 children → 6 outcomes with iterN ids."""
        loop = YamlLoop(
            id="l1",
            loop_type=LoopType.FOR,
            count=3,
            steps=[
                _step("set", step_type=StepType.ACTION, script="s.py"),
                _step("measure", step_type=StepType.ACTION, script="m.py"),
            ],
        )
        outcomes = _run(V32PlanDispatcher(_plan([loop])))
        assert len(outcomes) == 6
        assert all(o.status == "PASS" for o in outcomes)
        assert outcomes[0].step_id == "l1.iter0.set"
        assert outcomes[-1].step_id == "l1.iter2.measure"

    def test_depends_on_orders_steps(self) -> None:
        """depends_on forces execution order regardless of list order."""
        plan = _plan([
            _step("b", step_type=StepType.ACTION, script="b.py", depends_on=["a"]),
            _step("a", step_type=StepType.ACTION, script="a.py"),
        ])
        outcomes = _run(V32PlanDispatcher(plan))
        assert [o.step_id for o in outcomes] == ["a", "b"]

    def test_unmet_dependency_blocks(self) -> None:
        """Depends on a missing step → BLOCKED (deadlock guard)."""
        plan = _plan([
            _step("a", step_type=StepType.ACTION, script="a.py", depends_on=["ghost"]),
        ])
        outcomes = _run(V32PlanDispatcher(plan))
        assert outcomes[0].status == "BLOCKED"

    def test_legacy_script_type_backward_compat(self) -> None:
        """Steps without type default to SCRIPT and execute."""
        plan = _plan([_step("legacy", script="old.py")])
        outcomes = _run(V32PlanDispatcher(plan))
        assert outcomes[0].status == "PASS"
        assert outcomes[0].step_type == "script"


# ---------------------------------------------------------------------------
# JUnit builder
# ---------------------------------------------------------------------------


class TestJUnitFromOutcomes:
    """Tests for build_junit_xml_from_outcomes mapping."""

    def test_maps_status_to_junit(self) -> None:
        """PASS → testcase, SKIP → skipped, FAIL → failure."""
        plan = _plan([_step("p", step_type=StepType.ACTION, script="p.py")])
        outcomes = [
            StepOutcome("pass_step", "action", "PASS", detail="ok"),
            StepOutcome("skip_step", "action", "SKIP", detail="sk"),
            StepOutcome("fail_step", "action", "FAIL", detail="boom"),
        ]
        root = build_junit_xml_from_outcomes(plan, outcomes, "v32", 0.5)
        suite = root[0]
        assert suite.attrib["tests"] == "3"
        assert suite.attrib["failures"] == "1"
        assert suite.attrib["skipped"] == "1"

        names = [tc.attrib["name"] for tc in suite]
        assert names == ["pass_step", "skip_step", "fail_step"]
        assert suite[0].find("system-out") is not None
        assert suite[1].find("skipped") is not None
        assert suite[2].find("failure") is not None
