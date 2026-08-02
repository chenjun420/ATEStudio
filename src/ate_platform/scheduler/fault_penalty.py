"""FaultPenaltyIntegrator — injects fault probabilities as CP-SAT soft constraints.

Bridges FaultPredictor output with the CP-SAT scheduler. For each step,
computes a penalty weight = predict_prob * cost_factor and adds it to the
CP-SAT objective function as a soft constraint. The solver then naturally
prefers scheduling low-risk steps earlier in the execution order.

Soft constraint mechanism:
- Each step gets an integer penalty variable proportional to its fault probability.
- The penalty is added to the objective function alongside makespan.
- penalty_weight = round(predict_prob * cost_factor * step_duration)
- Higher cost_factor → stronger preference for low-risk steps.

Usage:
    from ate_platform.scheduler.fault_predictor import FaultPredictor
    from ate_platform.scheduler.fault_penalty import FaultPenaltyIntegrator

    predictor = FaultPredictor()
    predictor.train_from_samples(training_samples)

    integrator = FaultPenaltyIntegrator(predictor, cost_factor=100)
    penalties = integrator.compute_penalties(steps)
    schedule = scheduler.schedule(steps, fault_penalty=penalties)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ate_platform.scheduler.fault_predictor import FaultPredictor

if TYPE_CHECKING:
    from shared.dsl import YamlStep

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StepFaultPenalty:
    """Penalty data for a single step.

    Attributes:
        step_id: Step identifier.
        fault_probability: Predicted fault probability [0.0, 1.0].
        penalty_weight: Integer penalty to add to CP-SAT objective.
        duration: Step duration used in penalty computation.
    """

    step_id: str
    fault_probability: float
    penalty_weight: int
    duration: int


class FaultPenaltyIntegrator:
    """Integrates fault probabilities into CP-SAT solver as soft constraints.

    Computes per-step penalty weights from FaultPredictor output and provides
    a method to inject them into a CP-SAT model's objective function.

    The penalty for each step is:
        penalty_weight = round(predict_prob * cost_factor * duration)

    This means high-risk steps that take long contribute more to the objective,
    incentivizing the solver to schedule them later (or find alternative orders
    that minimize total risk-weighted time).

    Attributes:
        _predictor: Trained FaultPredictor instance.
        _cost_factor: Scaling factor for penalty weights.
        _product_type: Product type for feature extraction.
        _time_of_day: Hour of day for feature extraction.
        _instrument_id: Default instrument for feature extraction.
    """

    def __init__(
        self,
        predictor: FaultPredictor,
        cost_factor: int = 100,
        product_type: str = "",
        time_of_day: int = 12,
        instrument_id: str = "",
    ) -> None:
        """Initialize the fault penalty integrator.

        Args:
            predictor: Trained FaultPredictor instance.
            cost_factor: Scaling factor for penalty weights. Higher values
                make the solver more strongly prefer low-risk steps.
            product_type: Product type for feature extraction context.
            time_of_day: Hour of day (0-23) for feature extraction.
            instrument_id: Default instrument ID for feature extraction.
        """
        self._predictor = predictor
        self._cost_factor = cost_factor
        self._product_type = product_type
        self._time_of_day = time_of_day
        self._instrument_id = instrument_id

    def compute_penalties(
        self,
        steps: list[YamlStep],
        recent_failure_counts: dict[str, int] | None = None,
    ) -> dict[str, StepFaultPenalty]:
        """Compute fault penalties for all steps.

        Args:
            steps: List of YamlStep instances.
            recent_failure_counts: Optional per-step recent failure counts.

        Returns:
            Dict mapping step_id → StepFaultPenalty.
        """
        rfc = recent_failure_counts or {}
        result: dict[str, StepFaultPenalty] = {}

        for step in steps:
            dur = max(1, step.timeout) if step.timeout > 0 else 1
            prob = self._predictor.get_step_fault_probability(
                step_id=step.id,
                product_type=self._product_type,
                time_of_day=self._time_of_day,
                instrument_id=self._instrument_id,
                recent_failure_count=rfc.get(step.id, 0),
            )
            # Clamp probability to [0.0, 1.0]
            prob = max(0.0, min(1.0, prob))
            penalty_weight = int(round(prob * self._cost_factor * dur))

            result[step.id] = StepFaultPenalty(
                step_id=step.id,
                fault_probability=prob,
                penalty_weight=penalty_weight,
                duration=dur,
            )

        return result

    def get_penalty_weights(self, steps: list[YamlStep]) -> dict[str, int]:
        """Compute penalty weights as a simple dict (step_id → int).

        This is the format expected by CPSATScheduler.schedule(fault_penalty=...).

        Args:
            steps: List of YamlStep instances.

        Returns:
            Dict mapping step_id → integer penalty weight.
        """
        penalties = self.compute_penalties(steps)
        return {sid: p.penalty_weight for sid, p in penalties.items()}

    def inject_into_model(
        self,
        model: Any,
        state: dict[str, Any],
        steps: list[YamlStep],
        penalty_weights: dict[str, int],
    ) -> Any:
        """Inject fault penalties into a CP-SAT model as a soft constraint.

        Creates an integer penalty variable for each step (scaled by start time
        to incentivize scheduling high-risk steps later) and adds the total
        fault penalty to the objective.

        The penalty term is:
            total_fault_penalty = sum(penalty_weight[step] * start[step])

        Since the solver minimizes, high penalty_weight steps are pushed to
        later start times, effectively prioritizing low-risk steps early.

        Args:
            model: CP-SAT CpModel instance.
            state: State dict from CPSATScheduler._build_model().
            steps: List of YamlStep instances.
            penalty_weights: Dict mapping step_id → penalty weight.

        Returns:
            The total fault penalty IntVar added to the model.
        """
        from ortools.sat.python import cp_model

        step_starts = state["step_starts"]
        horizon = state.get("horizon", 1000)

        # Create a penalty term: sum(weight * start) for each step
        # We use a linear expression rather than individual variables
        # to keep the model compact.
        penalty_terms: list[tuple[Any, int]] = []
        for step in steps:
            weight = penalty_weights.get(step.id, 0)
            if weight > 0:
                # Penalty = weight * start_time
                # The solver minimizes, so higher weight × earlier start = more penalty
                # → solver prefers starting high-weight (high-risk) steps later
                penalty_terms.append((step_starts[step.id], weight))

        if not penalty_terms:
            # No penalties to add — return a zero variable
            zero_var = model.NewIntVar(0, 0, "zero_fault_penalty")
            return zero_var

        # Create the total penalty as a linear expression
        # CP-SAT supports: model.AddLinearExpressionInDomain(expr, domain)
        # For objective: we add it as a term to minimize

        # Create a variable to hold the total penalty
        max_penalty = sum(w * horizon for _, w in penalty_terms)
        total_penalty = model.NewIntVar(0, max_penalty, "total_fault_penalty")

        # Build linear expression: sum(weight_i * start_i)
        expr = cp_model.LinearExpr.Sum(
            [cp_model.LinearExpr.Term(var, coeff) for var, coeff in penalty_terms]
        )
        model.Add(total_penalty == expr)

        logger.debug(
            "Injected fault penalty for %d steps (max_penalty=%d)",
            len(penalty_terms),
            max_penalty,
        )

        return total_penalty
