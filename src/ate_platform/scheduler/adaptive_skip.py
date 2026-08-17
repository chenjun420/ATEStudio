"""Adaptive skip-if condition evaluator for DTA-QC mode.

Implements context-aware test step skipping that goes beyond simple
``skip_if`` expression evaluation. The evaluator integrates three
runtime data sources:

1. **SPC processor** — batch quality metrics (Cpk, Ppk) from the
   sliding-window SPC engine. When early-stage Cpk is excellent,
   later redundant test steps can be adaptively skipped.
2. **Fault predictor** — per-step fault probability from the
   logistic-regression classifier. Low-risk steps can be skipped
   to reduce test time without sacrificing coverage.
3. **Step-level features** — current step results and product context
   variables (temperature, humidity, etc.) from the VariableSpace.

Each step may declare a ``skip_conditions`` dict with one or more
typed condition entries. All declared conditions must evaluate True
for the step to be skipped (logical AND, matching the existing
``ConditionEvaluator.evaluate()`` semantics).

Supported condition types:
    - ``depends_on_result``: previous step result matches (PASS/FAIL)
    - ``product_context``: product context variable comparison
    - ``batch_quality``: SPC Cpk threshold for a measurement stream
    - ``fault_probability``: FaultPredictor probability threshold

When SPC or FaultPredictor is not available, the evaluator gracefully
degrades — batch_quality and fault_probability conditions are treated
as not-skip (fail-safe), while depends_on_result and product_context
continue to work with just the VariableSpace and step_results.
"""

from __future__ import annotations

import logging
import operator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, cast

from .variable_space import VariableSpace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Comparison operators — string → callable mapping
# ---------------------------------------------------------------------------

_COMPARATORS: dict[str, Any] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


class SkipConditionType(Enum):
    """Enumerates the supported adaptive skip condition types."""

    DEPENDS_ON_RESULT = "depends_on_result"
    PRODUCT_CONTEXT = "product_context"
    BATCH_QUALITY = "batch_quality"
    FAULT_PROBABILITY = "fault_probability"


# ---------------------------------------------------------------------------
# Condition dataclasses — one per type, all parsed at the boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DependsOnResultCondition:
    """Skip when a previous step's result matches the expected status.

    Attributes:
        step_id: The step whose result to check.
        result: Expected result — "PASS" or "FAIL".
    """

    step_id: str
    result: str


@dataclass(frozen=True, slots=True)
class ProductContextCondition:
    """Skip when a product context variable satisfies a comparison.

    Attributes:
        variable: Variable name (e.g. "scope.temperature").
        operator_str: Comparison operator (==, !=, >, >=, <, <=).
        value: Threshold value to compare against.
    """

    variable: str
    operator_str: str
    value: float


@dataclass(frozen=True, slots=True)
class BatchQualityCondition:
    """Skip based on SPC Cpk for a measurement stream.

    Attributes:
        product_type: Product type for the SPC stream.
        measurement_name: Measurement name for the SPC stream.
        operator_str: Comparison operator for the Cpk value.
        threshold: Cpk threshold to compare against.
    """

    product_type: str
    measurement_name: str
    operator_str: str
    threshold: float


@dataclass(frozen=True, slots=True)
class FaultProbabilityCondition:
    """Skip based on FaultPredictor probability for a step.

    Attributes:
        step_id: Step ID to evaluate fault probability for.
        operator_str: Comparison operator for the probability.
        threshold: Probability threshold (0.0–1.0).
    """

    step_id: str
    operator_str: str
    threshold: float


# Union of all condition types — exhaustive match enforced at evaluation
SkipCondition = (
    DependsOnResultCondition
    | ProductContextCondition
    | BatchQualityCondition
    | FaultProbabilityCondition
)


@dataclass(frozen=True, slots=True)
class SkipConditions:
    """Container for a step's adaptive skip conditions.

    All conditions must evaluate True for the step to be skipped.

    Attributes:
        conditions: List of typed skip conditions (logical AND).
        reason: Human-readable reason for skipping (optional).
    """

    conditions: list[SkipCondition] = field(default_factory=list)
    reason: str | None = None


# ---------------------------------------------------------------------------
# SPC / FaultPredictor protocols — structural typing for graceful degradation
# ---------------------------------------------------------------------------


class _SPCLike(Protocol):
    """Structural protocol for SPC-like processors."""

    def get_statistics(
        self, product_type: str, measurement_name: str
    ) -> Any: ...


class _FaultPredictorLike(Protocol):
    """Structural protocol for FaultPredictor-like predictors."""

    def get_step_fault_probability(
        self,
        step_id: str,
        product_type: str = "",
        time_of_day: int = 12,
        instrument_id: str = "",
        recent_failure_count: int = 0,
    ) -> float: ...


# ---------------------------------------------------------------------------
# Parsing — boundary validation for raw condition dicts
# ---------------------------------------------------------------------------

_VALID_RESULTS = frozenset({"PASS", "FAIL"})
_VALID_OPERATORS = frozenset(_COMPARATORS.keys())


def parse_skip_conditions(raw: dict[str, Any]) -> SkipConditions:
    """Parse a raw skip_conditions dict into typed SkipConditions.

    Args:
        raw: Dict with ``conditions`` (list of condition dicts) and
            optional ``reason`` (str). Each condition dict must have
            a ``type`` key matching a SkipConditionType value.

    Returns:
        Parsed SkipConditions dataclass.

    Raises:
        ValueError: If the raw dict is malformed or a condition type
            is unknown.
    """
    conditions: list[SkipCondition] = []
    raw_conditions = raw.get("conditions", [])
    if not isinstance(raw_conditions, list):
        raise ValueError(
            f"skip_conditions.conditions must be a list, got {type(raw_conditions).__name__}"
        )

    for i, cond in enumerate(raw_conditions):
        if not isinstance(cond, dict):
            raise ValueError(
                f"skip_conditions.conditions[{i}] must be a dict, got {type(cond).__name__}"
            )

        cond_type = cond.get("type")
        if cond_type is None:
            raise ValueError(
                f"skip_conditions.conditions[{i}] missing 'type' key"
            )

        try:
            cond_enum = SkipConditionType(cond_type)
        except ValueError:
            valid = [e.value for e in SkipConditionType]
            raise ValueError(
                f"skip_conditions.conditions[{i}] unknown type '{cond_type}'. "
                f"Valid types: {valid}"
            ) from None

        conditions.append(_parse_single_condition(cond_enum, cond, i))

    reason = raw.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError(
            f"skip_conditions.reason must be a string, got {type(reason).__name__}"
        )

    return SkipConditions(conditions=conditions, reason=reason)


def _parse_single_condition(
    cond_type: SkipConditionType,
    cond: dict[str, Any],
    index: int,
) -> SkipCondition:
    """Parse a single condition dict into its typed dataclass.

    Args:
        cond_type: The parsed condition type enum.
        cond: The raw condition dict.
        index: Index for error messages.

    Returns:
        Typed condition dataclass.

    Raises:
        ValueError: If required keys are missing or values are invalid.
    """
    prefix = f"skip_conditions.conditions[{index}]"

    match cond_type:
        case SkipConditionType.DEPENDS_ON_RESULT:
            step_id = cond.get("step_id")
            result = cond.get("result")
            if not step_id or not isinstance(step_id, str):
                raise ValueError(f"{prefix} depends_on_result requires 'step_id' (str)")
            if not result or not isinstance(result, str):
                raise ValueError(f"{prefix} depends_on_result requires 'result' (str)")
            result_upper = result.upper()
            if result_upper not in _VALID_RESULTS:
                raise ValueError(
                    f"{prefix} depends_on_result.result must be 'PASS' or 'FAIL', got '{result}'"
                )
            return DependsOnResultCondition(step_id=step_id, result=result_upper)

        case SkipConditionType.PRODUCT_CONTEXT:
            variable = cond.get("variable")
            op = cond.get("operator")
            value = cond.get("value")
            if not variable or not isinstance(variable, str):
                raise ValueError(f"{prefix} product_context requires 'variable' (str)")
            if not op or not isinstance(op, str) or op not in _VALID_OPERATORS:
                raise ValueError(
                    f"{prefix} product_context.operator must be one of {sorted(_VALID_OPERATORS)}"
                )
            if value is None:
                raise ValueError(f"{prefix} product_context requires 'value' (number)")
            return ProductContextCondition(
                variable=variable,
                operator_str=op,
                value=float(value),
            )

        case SkipConditionType.BATCH_QUALITY:
            pt = cond.get("product_type")
            mn = cond.get("measurement_name")
            op = cond.get("operator")
            threshold = cond.get("threshold")
            if not pt or not isinstance(pt, str):
                raise ValueError(f"{prefix} batch_quality requires 'product_type' (str)")
            if not mn or not isinstance(mn, str):
                raise ValueError(f"{prefix} batch_quality requires 'measurement_name' (str)")
            if not op or not isinstance(op, str) or op not in _VALID_OPERATORS:
                raise ValueError(
                    f"{prefix} batch_quality.operator must be one of {sorted(_VALID_OPERATORS)}"
                )
            if threshold is None:
                raise ValueError(f"{prefix} batch_quality requires 'threshold' (number)")
            return BatchQualityCondition(
                product_type=pt,
                measurement_name=mn,
                operator_str=op,
                threshold=float(threshold),
            )

        case SkipConditionType.FAULT_PROBABILITY:
            step_id = cond.get("step_id")
            op = cond.get("operator")
            threshold = cond.get("threshold")
            if not step_id or not isinstance(step_id, str):
                raise ValueError(f"{prefix} fault_probability requires 'step_id' (str)")
            if not op or not isinstance(op, str) or op not in _VALID_OPERATORS:
                raise ValueError(
                    f"{prefix} fault_probability.operator must be one of {sorted(_VALID_OPERATORS)}"
                )
            if threshold is None:
                raise ValueError(f"{prefix} fault_probability requires 'threshold' (number)")
            return FaultProbabilityCondition(
                step_id=step_id,
                operator_str=op,
                threshold=float(threshold),
            )


# ---------------------------------------------------------------------------
# AdaptiveConditionEvaluator — the main class
# ---------------------------------------------------------------------------


class AdaptiveConditionEvaluator:
    """Evaluates adaptive skip_conditions at schedule time.

    Integrates SPC (Cpk), FaultPredictor (fault probability), step
    results, and product context variables to determine whether a step
    should be adaptively skipped.

    When SPC or FaultPredictor is not available, batch_quality and
    fault_probability conditions gracefully degrade to not-skip
    (fail-safe) — the step is NOT skipped when the data source needed
    to evaluate the condition is absent.

    Args:
        spc_processor: Optional SPC processor for batch_quality conditions.
        fault_predictor: Optional FaultPredictor for fault_probability conditions.
        step_results: Mapping of step_id to result dict with 'status' key
            (StepStatus enum or string). Used for depends_on_result.
        variable_space: VariableSpace for product_context variable resolution.
        product_type: Default product_type for fault probability feature extraction.
        time_of_day: Default hour-of-day for fault probability (0-23).
        instrument_id: Default instrument ID for fault probability.
    """

    def __init__(
        self,
        spc_processor: _SPCLike | None = None,
        fault_predictor: _FaultPredictorLike | None = None,
        step_results: dict[str, Any] | None = None,
        variable_space: VariableSpace | None = None,
        product_type: str = "",
        time_of_day: int = 12,
        instrument_id: str = "",
    ) -> None:
        self._spc = spc_processor
        self._fault_predictor = fault_predictor
        self._step_results = step_results or {}
        self._variable_space = variable_space or VariableSpace()
        self._product_type = product_type
        self._time_of_day = time_of_day
        self._instrument_id = instrument_id

    def should_skip(self, skip_conditions: SkipConditions) -> bool:
        """Evaluate all conditions; return True if step should be skipped.

        All conditions must evaluate True (logical AND). If no
        conditions are declared, returns False (no skip).

        Args:
            skip_conditions: Parsed SkipConditions for the step.

        Returns:
            True if the step should be skipped, False otherwise.
        """
        if not skip_conditions.conditions:
            return False

        return all(
            self._evaluate_single(cond) for cond in skip_conditions.conditions
        )

    def evaluate_skip_reason(self, skip_conditions: SkipConditions) -> str | None:
        """Return the skip reason if the step should be skipped, else None.

        Args:
            skip_conditions: Parsed SkipConditions for the step.

        Returns:
            The reason string if skipping, None if not skipping.
            Falls back to a generated reason if none was declared.
        """
        if not self.should_skip(skip_conditions):
            return None

        if skip_conditions.reason:
            return skip_conditions.reason

        # Generate a reason from the condition types
        types = [type(c).__name__ for c in skip_conditions.conditions]
        return f"Adaptive skip: {', '.join(types)}"

    def prune_dependency_graph(
        self,
        step_conditions: dict[str, SkipConditions],
    ) -> set[str]:
        """Pre-evaluate skip conditions and return steps to prune.

        Called at schedule time to prune the dependency graph: steps
        whose skip conditions are already satisfied are removed from
        the execution plan, and their dependents are notified (via
        the existing SKIPPED cascade mechanism in the scheduler).

        Args:
            step_conditions: Mapping of step_id to SkipConditions.

        Returns:
            Set of step_ids that should be skipped (pruned from graph).
        """
        to_skip: set[str] = set()
        for step_id, conditions in step_conditions.items():
            if self.should_skip(conditions):
                to_skip.add(step_id)
                logger.info(
                    "Adaptive skip: step '%s' pruned from dependency graph",
                    step_id,
                )
        return to_skip

    # ------------------------------------------------------------------
    # Internal evaluation — exhaustive match on condition types
    # ------------------------------------------------------------------

    def _evaluate_single(self, condition: SkipCondition) -> bool:
        """Evaluate a single typed condition.

        Uses exhaustive match on the condition union. Each branch
        delegates to a specialized evaluator.

        Args:
            condition: One of the typed condition dataclasses.

        Returns:
            True if the condition is satisfied (contributes to skip).
        """
        match condition:
            case DependsOnResultCondition():
                return self._eval_depends_on_result(condition)
            case ProductContextCondition():
                return self._eval_product_context(condition)
            case BatchQualityCondition():
                return self._eval_batch_quality(condition)
            case FaultProbabilityCondition():
                return self._eval_fault_probability(condition)

    def _eval_depends_on_result(
        self, cond: DependsOnResultCondition
    ) -> bool:
        """Check if a previous step's result matches the expected value.

        Args:
            cond: The depends_on_result condition.

        Returns:
            True if the step's status matches the expected result.
        """
        result = self._step_results.get(cond.step_id)
        if result is None:
            return False

        # Handle both StepStatus enum and raw dict
        status = self._extract_status(result)
        if status is None:
            return False

        # Map PASS -> PASSED, FAIL -> FAILED (normalize case)
        expected = "PASSED" if cond.result.upper() == "PASS" else "FAILED"
        return status == expected

    def _eval_product_context(self, cond: ProductContextCondition) -> bool:
        """Check a product context variable against a threshold.

        Resolves the variable from VariableSpace and applies the
        comparison operator.

        Args:
            cond: The product_context condition.

        Returns:
            True if the comparison is satisfied.
        """
        value = self._variable_space.get(cond.variable)
        if value is None:
            return False

        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return False

        cmp = _COMPARATORS.get(cond.operator_str)
        if cmp is None:
            return False

        return bool(cmp(numeric_value, cond.value))

    def _eval_batch_quality(self, cond: BatchQualityCondition) -> bool:
        """Check SPC Cpk for a measurement stream.

        Gracefully degrades: if no SPC processor is available, returns
        False (do not skip — fail-safe).

        Args:
            cond: The batch_quality condition.

        Returns:
            True if the Cpk comparison is satisfied.
        """
        if self._spc is None:
            logger.debug(
                "batch_quality condition for %s/%s: SPC processor not available — not skipping",
                cond.product_type,
                cond.measurement_name,
            )
            return False

        try:
            stats = self._spc.get_statistics(
                cond.product_type, cond.measurement_name
            )
        except Exception:
            logger.warning(
                "batch_quality: SPC get_statistics failed for %s/%s — not skipping",
                cond.product_type,
                cond.measurement_name,
                exc_info=True,
            )
            return False

        cpk = self._extract_cpk(stats)
        if cpk is None:
            return False

        cmp = _COMPARATORS.get(cond.operator_str)
        if cmp is None:
            return False

        return bool(cmp(cpk, cond.threshold))

    def _eval_fault_probability(
        self, cond: FaultProbabilityCondition
    ) -> bool:
        """Check FaultPredictor probability for a step.

        Gracefully degrades: if no FaultPredictor is available (or it
        is untrained), returns False (do not skip — fail-safe).

        Args:
            cond: The fault_probability condition.

        Returns:
            True if the probability comparison is satisfied.
        """
        if self._fault_predictor is None:
            logger.debug(
                "fault_probability condition for step '%s': "
                "FaultPredictor not available — not skipping",
                cond.step_id,
            )
            return False

        try:
            prob = self._fault_predictor.get_step_fault_probability(
                step_id=cond.step_id,
                product_type=self._product_type,
                time_of_day=self._time_of_day,
                instrument_id=self._instrument_id,
            )
        except Exception:
            logger.warning(
                "fault_probability: prediction failed for step '%s' — not skipping",
                cond.step_id,
                exc_info=True,
            )
            return False

        cmp = _COMPARATORS.get(cond.operator_str)
        if cmp is None:
            return False

        return bool(cmp(prob, cond.threshold))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_status(result: Any) -> str | None:
        """Extract a normalized status string from a step result.

        Handles StepStatus enum, dict with 'status' key, and string.

        Args:
            result: Step result (StepStatus, dict, or string).

        Returns:
            Uppercase status string (e.g. 'PASSED'), or None.
        """
        # StepStatus enum
        if hasattr(result, "value"):
            return cast(str, result.value)

        # Dict with 'status' key
        if isinstance(result, dict):
            status_val = result.get("status")
            if status_val is None:
                return None
            if hasattr(status_val, "value"):
                return cast(str, status_val.value)
            if isinstance(status_val, str):
                return status_val.upper()
            return None

        # Raw string
        if isinstance(result, str):
            return result.upper()

        return None

    @staticmethod
    def _extract_cpk(stats: Any) -> float | None:
        """Extract the Cpk value from SPC statistics.

        Handles SPCStatistics (Pydantic model), dict, and None.

        Args:
            stats: SPC statistics object.

        Returns:
            Cpk float value, or None if not available.
        """
        if stats is None:
            return None

        # Pydantic model (SPCStatistics)
        if hasattr(stats, "cpk"):
            cpk = stats.cpk
            return float(cpk) if cpk is not None else None

        # Dict
        if isinstance(stats, dict):
            cpk = stats.get("cpk")
            return float(cpk) if cpk is not None else None

        return None


__all__ = [
    "AdaptiveConditionEvaluator",
    "BatchQualityCondition",
    "DependsOnResultCondition",
    "FaultProbabilityCondition",
    "ProductContextCondition",
    "SkipCondition",
    "SkipConditionType",
    "SkipConditions",
    "parse_skip_conditions",
]
