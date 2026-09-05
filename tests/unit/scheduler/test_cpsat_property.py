"""Property-based tests for CPSATScheduler using Hypothesis.

These tests verify fundamental invariants of the CP-SAT scheduler that must
hold for *any* valid input:

1. Resource constraints are never violated — steps sharing a resource never
   overlap in time.
2. Step count invariant — the result dict has exactly as many entries as the
   input step list.
3. Precedence constraints are respected — a step never starts before its
   preconditions end.
4. Scheduling always completes — for any valid DAG of steps, the solver
   returns a non-None schedule (within the time budget).
5. Duration integrity — each scheduled step's (end - start) equals its
   configured timeout (or 1 when timeout <= 0).
"""

from __future__ import annotations

import pytest

# Hypothesis is an optional test dependency. When it is not installed (minimal
# venv) skip this module cleanly at collection instead of raising
# ModuleNotFoundError (which would ERROR the whole suite). The property tests
# collect and run normally wherever hypothesis (and OR-Tools, guarded below)
# are present.
pytest.importorskip("hypothesis")

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from shared.dsl import YamlStep  # noqa: E402

# ---------------------------------------------------------------------------
# Skip if OR-Tools is not installed (mirrors tests/scheduler/test_cpsat.py)
# ---------------------------------------------------------------------------
ortools_available = False
try:
    from ortools.sat.python import cp_model  # noqa: F401

    ortools_available = True
except ImportError:
    pass

pytestmark = pytest.mark.skipif(
    not ortools_available,
    reason="OR-Tools not installed",
)


# ---------------------------------------------------------------------------
# Hypothesis strategy: generate valid step lists (acyclic dependency DAG)
# ---------------------------------------------------------------------------

_RESOURCE_NAMES = st.sampled_from(["r1", "r2", "r3"])


@st.composite
def steps_strategy(draw: st.DrawFn) -> list[YamlStep]:
    """Generate a list of YamlStep instances forming a valid acyclic DAG.

    Steps are generated sequentially so that preconditions only reference
    earlier step IDs, guaranteeing acyclicity.
    """
    n = draw(st.integers(min_value=1, max_value=6))
    steps: list[YamlStep] = []
    for i in range(n):
        sid = f"s{i}"
        prior_ids = [f"s{j}" for j in range(i)]
        preconditions = draw(
            st.lists(st.sampled_from(prior_ids), unique=True, max_size=2)
            if prior_ids
            else st.just([])
        )
        resources = draw(
            st.dictionaries(
                keys=_RESOURCE_NAMES,
                values=st.just(1),
                max_size=1,
            )
        )
        timeout = draw(st.integers(min_value=1, max_value=5))
        steps.append(
            YamlStep(
                id=sid,
                script="echo test",
                preconditions=preconditions,
                resources=resources,
                timeout=timeout,
            )
        )
    return steps


def _has_time_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Return True if two (start, end) intervals overlap."""
    return a[0] < b[1] and b[0] < a[1]


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

class TestCPSATPropertyInvariants:
    """Property-based invariants for CPSATScheduler.schedule()."""

    @given(steps=steps_strategy())
    @settings(max_examples=100, deadline=5000)
    @pytest.mark.hypothesis
    def test_resource_constraints_never_violated(self, steps: list[YamlStep]) -> None:
        """Given any step list, no two steps sharing a resource overlap in time."""
        from ate_platform.scheduler.cpsat import CPSATScheduler

        scheduler = CPSATScheduler(time_limit=3.0)
        result = scheduler.schedule(steps)
        if result is None:
            pytest.skip("Solver returned None (timeout) — invariant not testable")

        # Group step IDs by resource
        resource_to_steps: dict[str, list[str]] = {}
        for step in steps:
            for rid in step.resources:
                resource_to_steps.setdefault(rid, []).append(step.id)

        for rid, sids in resource_to_steps.items():
            for i, sid_i in enumerate(sids):
                for sid_j in sids[i + 1:]:
                    interval_i = (result[sid_i][0], result[sid_i][1])
                    interval_j = (result[sid_j][0], result[sid_j][1])
                    assert not _has_time_overlap(interval_i, interval_j), (
                        f"Resource '{rid}' conflict: {sid_i} {interval_i} "
                        f"overlaps {sid_j} {interval_j}"
                    )

    @given(steps=steps_strategy())
    @settings(max_examples=100, deadline=5000)
    @pytest.mark.hypothesis
    def test_step_count_invariant(self, steps: list[YamlStep]) -> None:
        """Given any step list, the result has exactly len(steps) entries."""
        from ate_platform.scheduler.cpsat import CPSATScheduler

        scheduler = CPSATScheduler(time_limit=3.0)
        result = scheduler.schedule(steps)
        if result is None:
            pytest.skip("Solver returned None (timeout) — invariant not testable")

        assert len(result) == len(steps), (
            f"Expected {len(steps)} scheduled steps, got {len(result)}"
        )
        expected_ids = {s.id for s in steps}
        assert set(result.keys()) == expected_ids

    @given(steps=steps_strategy())
    @settings(max_examples=100, deadline=5000)
    @pytest.mark.hypothesis
    def test_precedence_constraints_respected(self, steps: list[YamlStep]) -> None:
        """Given any step list, each step starts at or after its preconditions end."""
        from ate_platform.scheduler.cpsat import CPSATScheduler

        scheduler = CPSATScheduler(time_limit=3.0)
        result = scheduler.schedule(steps)
        if result is None:
            pytest.skip("Solver returned None (timeout) — invariant not testable")

        step_ids = {s.id for s in steps}
        for step in steps:
            for dep_id in step.preconditions:
                if dep_id not in step_ids:
                    continue
                _, dep_end, _ = result[dep_id]
                step_start, _, _ = result[step.id]
                assert step_start >= dep_end, (
                    f"Step '{step.id}' starts at {step_start} but dependency "
                    f"'{dep_id}' ends at {dep_end}"
                )

    @given(steps=steps_strategy())
    @settings(max_examples=100, deadline=5000)
    @pytest.mark.hypothesis
    def test_scheduling_always_completes(self, steps: list[YamlStep]) -> None:
        """Given any valid step list, the solver returns a non-None schedule.

        The generated steps form a valid acyclic DAG with small durations and
        at most 6 steps — the solver should always find a feasible solution
        within the 3-second time budget.
        """
        from ate_platform.scheduler.cpsat import CPSATScheduler

        scheduler = CPSATScheduler(time_limit=3.0)
        result = scheduler.schedule(steps)
        assert result is not None, (
            f"Solver returned None for {len(steps)} steps — expected feasible solution"
        )

    @given(steps=steps_strategy())
    @settings(max_examples=100, deadline=5000)
    @pytest.mark.hypothesis
    def test_duration_integrity(self, steps: list[YamlStep]) -> None:
        """Given any step list, each scheduled step's duration matches its timeout."""
        from ate_platform.scheduler.cpsat import CPSATScheduler

        scheduler = CPSATScheduler(time_limit=3.0)
        result = scheduler.schedule(steps)
        if result is None:
            pytest.skip("Solver returned None (timeout) — invariant not testable")

        for step in steps:
            start, end, _ = result[step.id]
            expected_duration = max(1, step.timeout) if step.timeout > 0 else 1
            actual_duration = end - start
            assert actual_duration == expected_duration, (
                f"Step '{step.id}': expected duration {expected_duration} "
                f"(timeout={step.timeout}), got {actual_duration}"
            )
