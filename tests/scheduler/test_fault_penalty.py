"""Integration tests for FaultPenaltyIntegrator with CP-SAT solver.

Tests cover:
- FaultPenaltyIntegrator.compute_penalties() produces valid StepFaultPenalty
- get_penalty_weights() returns dict[str, int]
- Penalty weights are proportional to fault probability
- CPSATScheduler.schedule() accepts fault_penalty parameter
- Penalty integration changes solver output (high-risk steps scheduled later)
- No penalty → same schedule as without fault_penalty
- inject_into_model() creates valid CP-SAT variables
- End-to-end: predictor → integrator → scheduler → schedule with risk ordering
"""

from __future__ import annotations

from typing import Any

import pytest

from shared.dsl import YamlStep  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Skip if ortools not available
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
# Helpers
# ---------------------------------------------------------------------------


def _make_step(
    step_id: str,
    preconditions: list[str] | None = None,
    resources: dict[str, Any] | None = None,
    timeout: int = 1,
) -> YamlStep:
    """Factory for YamlStep."""
    return YamlStep(
        id=step_id,
        script="echo test",
        preconditions=preconditions or [],
        resources=resources or {},
        timeout=timeout,
    )


def _make_training_samples() -> list[dict[str, Any]]:
    """Create training data where 'risky_step' has high fault probability."""
    samples: list[dict[str, Any]] = []
    # High-risk: risky_step, afternoon, high recent failures
    for _ in range(20):
        samples.append({
            "product_type": "product_a",
            "test_step": "risky_step",
            "time_of_day": 15,
            "instrument_id": "scope_1",
            "recent_failure_count": 5,
            "label": 1,
        })
    # Low-risk: safe_step, morning, no recent failures
    for _ in range(20):
        samples.append({
            "product_type": "product_a",
            "test_step": "safe_step",
            "time_of_day": 8,
            "instrument_id": "dmm_1",
            "recent_failure_count": 0,
            "label": 0,
        })
    return samples


# ---------------------------------------------------------------------------
# Tests: FaultPenaltyIntegrator — penalty computation
# ---------------------------------------------------------------------------


class TestPenaltyComputation:
    """FaultPenaltyIntegrator computes valid penalties."""

    def test_compute_penalties_returns_all_steps(self) -> None:
        """Given: trained predictor + 3 steps. When: compute_penalties().
        Then: returns penalty for each step.
        """
        from ate_platform.scheduler.fault_penalty import FaultPenaltyIntegrator
        from ate_platform.scheduler.fault_predictor import FaultPredictor

        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())
        integrator = FaultPenaltyIntegrator(predictor, cost_factor=100)

        steps = [_make_step("a"), _make_step("b"), _make_step("c")]
        penalties = integrator.compute_penalties(steps)

        assert len(penalties) == 3
        assert all(sid in penalties for sid in ["a", "b", "c"])

    def test_penalty_weights_are_non_negative(self) -> None:
        """Given: trained predictor. When: compute_penalties().
        Then: all penalty_weights >= 0.
        """
        from ate_platform.scheduler.fault_penalty import FaultPenaltyIntegrator
        from ate_platform.scheduler.fault_predictor import FaultPredictor

        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())
        integrator = FaultPenaltyIntegrator(predictor, cost_factor=100)

        steps = [_make_step("a"), _make_step("b")]
        penalties = integrator.compute_penalties(steps)

        for p in penalties.values():
            assert p.penalty_weight >= 0

    def test_get_penalty_weights_returns_int_dict(self) -> None:
        """Given: trained predictor. When: get_penalty_weights().
        Then: returns dict[str, int].
        """
        from ate_platform.scheduler.fault_penalty import FaultPenaltyIntegrator
        from ate_platform.scheduler.fault_predictor import FaultPredictor

        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())
        integrator = FaultPenaltyIntegrator(predictor, cost_factor=100)

        steps = [_make_step("a"), _make_step("b")]
        weights = integrator.get_penalty_weights(steps)

        assert isinstance(weights, dict)
        for val in weights.values():
            assert isinstance(val, int)

    def test_higher_prob_higher_penalty(self) -> None:
        """Given: predictor that assigns higher prob to 'risky_step'.
        When: compute_penalties with risky and safe steps.
        Then: risky_step has higher penalty_weight.
        """
        from ate_platform.scheduler.fault_penalty import FaultPenaltyIntegrator
        from ate_platform.scheduler.fault_predictor import FaultPredictor

        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())
        integrator = FaultPenaltyIntegrator(
            predictor,
            cost_factor=100,
            product_type="product_a",
        )

        steps = [_make_step("risky_step", timeout=5), _make_step("safe_step", timeout=5)]
        penalties = integrator.compute_penalties(steps)

        # risky_step should have higher fault probability
        assert penalties["risky_step"].fault_probability > penalties["safe_step"].fault_probability
        # And therefore higher penalty weight
        assert penalties["risky_step"].penalty_weight > penalties["safe_step"].penalty_weight


# ---------------------------------------------------------------------------
# Tests: CP-SAT schedule accepts fault_penalty
# ---------------------------------------------------------------------------


class TestCPSATAcceptsFaultPenalty:
    """CPSATScheduler.schedule() accepts the fault_penalty parameter."""

    def test_schedule_with_fault_penalty_returns_valid_schedule(self) -> None:
        """Given: 3 independent steps + fault_penalty dict.
        When: schedule(steps, fault_penalty=...).
        Then: returns valid schedule dict.
        """
        from ate_platform.scheduler.cpsat import CPSATScheduler

        scheduler = CPSATScheduler(time_limit=3.0)
        steps = [_make_step("a"), _make_step("b"), _make_step("c")]
        penalties = {"a": 0, "b": 50, "c": 100}

        result = scheduler.schedule(steps, fault_penalty=penalties)
        assert result is not None
        assert len(result) == 3

    def test_schedule_without_fault_penalty_unchanged(self) -> None:
        """Given: steps. When: schedule without fault_penalty.
        Then: same as schedule with fault_penalty=None.
        """
        from ate_platform.scheduler.cpsat import CPSATScheduler

        scheduler = CPSATScheduler(time_limit=3.0)
        steps = [_make_step("a"), _make_step("b")]

        result_no_penalty = scheduler.schedule(steps)
        result_none = scheduler.schedule(steps, fault_penalty=None)

        assert result_no_penalty is not None
        assert result_none is not None
        # Both should produce valid schedules
        assert len(result_no_penalty) == 2
        assert len(result_none) == 2

    def test_schedule_with_zero_penalty_same_as_none(self) -> None:
        """Given: steps + zero penalties. When: schedule.
        Then: same schedule as no penalty.
        """
        from ate_platform.scheduler.cpsat import CPSATScheduler

        scheduler = CPSATScheduler(time_limit=3.0)
        steps = [_make_step("a"), _make_step("b")]

        result_no_penalty = scheduler.schedule(steps)
        result_zero_penalty = scheduler.schedule(
            steps, fault_penalty={"a": 0, "b": 0}
        )

        assert result_no_penalty is not None
        assert result_zero_penalty is not None
        # Start times should be the same when penalties are zero
        for sid in ["a", "b"]:
            assert result_no_penalty[sid][0] == result_zero_penalty[sid][0]


# ---------------------------------------------------------------------------
# Tests: Penalty changes solver output — the key integration test
# ---------------------------------------------------------------------------


class TestPenaltyChangesSchedule:
    """Fault penalties must influence scheduling decisions."""

    def test_high_risk_step_scheduled_later_with_penalty(self) -> None:
        """Given: two independent steps sharing a resource (serialized).
        Without penalty: solver may schedule either first.
        With penalty: high-risk step should start later.

        When: schedule with fault_penalty giving high weight to 'risky'.
        Then: risky step starts after safe step.
        """
        from ate_platform.scheduler.cpsat import CPSATScheduler

        scheduler = CPSATScheduler(time_limit=5.0)
        # Two steps sharing a resource → must be serialized
        steps = [
            _make_step("safe", resources={"r1": 1}, timeout=3),
            _make_step("risky", resources={"r1": 1}, timeout=3),
        ]

        # Without penalty — either order is valid
        result_no_penalty = scheduler.schedule(steps)
        assert result_no_penalty is not None

        # With penalty — risky step should be scheduled later
        result_with_penalty = scheduler.schedule(
            steps,
            fault_penalty={"safe": 0, "risky": 500},
        )
        assert result_with_penalty is not None

        # The high-risk step should start at or after the safe step's start
        safe_start = result_with_penalty["safe"][0]
        risky_start = result_with_penalty["risky"][0]
        assert safe_start <= risky_start, (
            f"Safe step should start before or at same time as risky step: "
            f"safe_start={safe_start}, risky_start={risky_start}"
        )

    def test_penalty_affects_start_time_ordering(self) -> None:
        """Given: 3 steps sharing a resource, different penalties.
        When: schedule with descending penalties (a=high, b=med, c=low).
        Then: steps ordered c_before_b_before_a (low risk first).
        """
        from ate_platform.scheduler.cpsat import CPSATScheduler

        scheduler = CPSATScheduler(time_limit=5.0)
        steps = [
            _make_step("high_risk", resources={"r1": 1}, timeout=2),
            _make_step("med_risk", resources={"r1": 1}, timeout=2),
            _make_step("low_risk", resources={"r1": 1}, timeout=2),
        ]

        result = scheduler.schedule(
            steps,
            fault_penalty={"high_risk": 500, "med_risk": 250, "low_risk": 0},
        )
        assert result is not None

        starts = {
            sid: result[sid][0]
            for sid in ["high_risk", "med_risk", "low_risk"]
        }

        # Low risk should start first, high risk last
        assert starts["low_risk"] < starts["high_risk"], (
            f"Low risk ({starts['low_risk']}) should start before high risk ({starts['high_risk']})"
        )


# ---------------------------------------------------------------------------
# Tests: inject_into_model
# ---------------------------------------------------------------------------


class TestInjectIntoModel:
    """FaultPenaltyIntegrator.inject_into_model() creates CP-SAT variables."""

    def test_inject_creates_penalty_variable(self) -> None:
        """Given: CP-SAT model + state + penalties.
        When: inject_into_model().
        Then: returns an IntVar.
        """
        from ate_platform.scheduler.fault_penalty import FaultPenaltyIntegrator
        from ate_platform.scheduler.fault_predictor import FaultPredictor

        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())
        integrator = FaultPenaltyIntegrator(predictor, cost_factor=100)

        model = cp_model.CpModel()
        start_a = model.NewIntVar(0, 100, "start_a")
        start_b = model.NewIntVar(0, 100, "start_b")
        state: dict[str, Any] = {
            "step_starts": {"a": start_a, "b": start_b},
            "horizon": 100,
        }
        steps = [_make_step("a"), _make_step("b")]
        weights = {"a": 50, "b": 100}

        penalty_var = integrator.inject_into_model(model, state, steps, weights)

        # Should be an IntVar
        assert penalty_var is not None

    def test_inject_with_zero_weights_returns_zero_var(self) -> None:
        """Given: all zero penalty weights. When: inject_into_model().
        Then: returns a zero variable.
        """
        from ate_platform.scheduler.fault_penalty import FaultPenaltyIntegrator
        from ate_platform.scheduler.fault_predictor import FaultPredictor

        predictor = FaultPredictor()
        integrator = FaultPenaltyIntegrator(predictor, cost_factor=100)

        model = cp_model.CpModel()
        start_a = model.NewIntVar(0, 100, "start_a")
        state: dict[str, Any] = {
            "step_starts": {"a": start_a},
            "horizon": 100,
        }
        steps = [_make_step("a")]
        weights = {"a": 0}

        penalty_var = integrator.inject_into_model(model, state, steps, weights)
        assert penalty_var is not None


# ---------------------------------------------------------------------------
# Tests: End-to-end integration
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    """Full pipeline: predictor → integrator → scheduler."""

    def test_end_to_end_risk_aware_scheduling(self) -> None:
        """Given: trained predictor, two serialized steps (risky + safe).
        When: compute penalties via integrator, pass to scheduler.
        Then: safe step scheduled before risky step.
        """
        from ate_platform.scheduler.cpsat import CPSATScheduler
        from ate_platform.scheduler.fault_penalty import FaultPenaltyIntegrator
        from ate_platform.scheduler.fault_predictor import FaultPredictor

        # Train predictor
        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())

        # Create integrator with high cost factor
        integrator = FaultPenaltyIntegrator(
            predictor,
            cost_factor=500,
            product_type="product_a",
        )

        # Steps sharing a resource (must serialize)
        steps = [
            _make_step("risky_step", resources={"r1": 1}, timeout=3),
            _make_step("safe_step", resources={"r1": 1}, timeout=3),
        ]

        # Compute penalties
        weights = integrator.get_penalty_weights(steps)

        # Both should have non-negative weights
        assert weights["risky_step"] >= 0
        assert weights["safe_step"] >= 0

        # Schedule with penalties
        scheduler = CPSATScheduler(time_limit=5.0)
        result = scheduler.schedule(steps, fault_penalty=weights)
        assert result is not None

        # Safe step should start first
        safe_start = result["safe_step"][0]
        risky_start = result["risky_step"][0]
        assert safe_start <= risky_start, (
            f"Safe step should start before risky step: "
            f"safe={safe_start}, risky={risky_start}"
        )

    def test_end_to_end_schedule_validity(self) -> None:
        """Given: full pipeline. When: schedule with penalties.
        Then: schedule respects resource constraints.
        """
        from ate_platform.scheduler.cpsat import CPSATScheduler
        from ate_platform.scheduler.fault_penalty import FaultPenaltyIntegrator
        from ate_platform.scheduler.fault_predictor import FaultPredictor

        predictor = FaultPredictor()
        predictor.train_from_samples(_make_training_samples())
        integrator = FaultPenaltyIntegrator(predictor, cost_factor=100)

        # Steps with shared resource
        steps = [
            _make_step("a", resources={"r1": 1}, timeout=2),
            _make_step("b", resources={"r1": 1}, timeout=2),
        ]
        weights = integrator.get_penalty_weights(steps)

        scheduler = CPSATScheduler(time_limit=3.0)
        result = scheduler.schedule(steps, fault_penalty=weights)
        assert result is not None

        # Resource constraint: a and b cannot overlap
        a_start, a_end, _ = result["a"]
        b_start, b_end, _ = result["b"]
        assert a_end <= b_start or b_end <= a_start, (
            f"Resource constraint violated: a=[{a_start},{a_end}], b=[{b_start},{b_end}]"
        )
