"""Unit tests for AdaptiveConditionEvaluator (adaptive skip-if).

Tests cover:
- depends_on_result: PASS/FAIL matching against step_results
- product_context: variable resolution + comparison operators
- batch_quality: SPC Cpk threshold evaluation + graceful degradation
- fault_probability: FaultPredictor probability + graceful degradation
- combined conditions: logical AND semantics
- no-skip case: empty conditions / all-false conditions
- parse_skip_conditions: boundary validation
- prune_dependency_graph: schedule-time graph pruning
- integration with ScannerScheduler._evaluate_step_skip()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from ate_platform.scheduler.adaptive_skip import (
    AdaptiveConditionEvaluator,
    BatchQualityCondition,
    DependsOnResultCondition,
    FaultProbabilityCondition,
    ProductContextCondition,
    SkipConditions,
    parse_skip_conditions,
)
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import StepStatus

# ---------------------------------------------------------------------------
# Fakes — in-memory SPC and FaultPredictor stand-ins
# ---------------------------------------------------------------------------


@dataclass
class FakeSPCStats:
    """Mimics SPCStatistics (Pydantic model) with just the fields we need."""

    cpk: float | None = None
    ppk: float | None = None
    sample_count: int = 0


class FakeSPCProcessor:
    """In-memory SPC processor for testing batch_quality conditions."""

    def __init__(self, stats_map: dict[tuple[str, str], FakeSPCStats] | None = None) -> None:
        self._stats_map = stats_map or {}

    def get_statistics(self, product_type: str, measurement_name: str) -> FakeSPCStats:
        return self._stats_map.get(
            (product_type, measurement_name),
            FakeSPCStats(cpk=None, sample_count=0),
        )


class FakeFaultPredictor:
    """In-memory fault predictor for testing fault_probability conditions."""

    def __init__(self, prob_map: dict[str, float] | None = None) -> None:
        self._prob_map = prob_map or {}

    def get_step_fault_probability(
        self,
        step_id: str,
        product_type: str = "",
        time_of_day: int = 12,
        instrument_id: str = "",
        recent_failure_count: int = 0,
    ) -> float:
        return self._prob_map.get(step_id, 0.5)


# ---------------------------------------------------------------------------
# Tests: depends_on_result
# ---------------------------------------------------------------------------


class TestDependsOnResult:
    """Tests for depends_on_result condition type."""

    def test_skip_when_previous_step_passed(self) -> None:
        """Given step_a PASSED, depends_on_result(step_a, PASS) → skip."""
        step_results = {"step_a": {"status": StepStatus.PASSED}}
        evaluator = AdaptiveConditionEvaluator(step_results=step_results)

        cond = SkipConditions(
            conditions=[DependsOnResultCondition(step_id="step_a", result="PASS")],
        )
        assert evaluator.should_skip(cond) is True

    def test_skip_when_previous_step_failed(self) -> None:
        """Given step_a FAILED, depends_on_result(step_a, FAIL) → skip."""
        step_results = {"step_a": {"status": StepStatus.FAILED}}
        evaluator = AdaptiveConditionEvaluator(step_results=step_results)

        cond = SkipConditions(
            conditions=[DependsOnResultCondition(step_id="step_a", result="FAIL")],
        )
        assert evaluator.should_skip(cond) is True

    def test_no_skip_when_result_mismatch(self) -> None:
        """Given step_a PASSED, depends_on_result(step_a, FAIL) → no skip."""
        step_results = {"step_a": {"status": StepStatus.PASSED}}
        evaluator = AdaptiveConditionEvaluator(step_results=step_results)

        cond = SkipConditions(
            conditions=[DependsOnResultCondition(step_id="step_a", result="FAIL")],
        )
        assert evaluator.should_skip(cond) is False

    def test_no_skip_when_step_not_found(self) -> None:
        """Given no results, depends_on_result(unknown, PASS) → no skip."""
        evaluator = AdaptiveConditionEvaluator(step_results={})

        cond = SkipConditions(
            conditions=[DependsOnResultCondition(step_id="unknown", result="PASS")],
        )
        assert evaluator.should_skip(cond) is False

    def test_skip_with_string_status(self) -> None:
        """Should handle raw string status values (not just StepStatus enum)."""
        step_results = {"step_a": {"status": "PASSED"}}
        evaluator = AdaptiveConditionEvaluator(step_results=step_results)

        cond = SkipConditions(
            conditions=[DependsOnResultCondition(step_id="step_a", result="PASS")],
        )
        assert evaluator.should_skip(cond) is True

    def test_skip_with_stepstatus_enum_directly(self) -> None:
        """Should handle StepStatus enum as the result value directly."""
        step_results = {"step_a": StepStatus.FAILED}
        evaluator = AdaptiveConditionEvaluator(step_results=step_results)

        cond = SkipConditions(
            conditions=[DependsOnResultCondition(step_id="step_a", result="FAIL")],
        )
        assert evaluator.should_skip(cond) is True

    def test_skip_with_lowercase_result_in_condition(self) -> None:
        """Condition result 'pass' should be normalized to 'PASS'."""
        step_results = {"step_a": {"status": StepStatus.PASSED}}
        evaluator = AdaptiveConditionEvaluator(step_results=step_results)

        cond = SkipConditions(
            conditions=[DependsOnResultCondition(step_id="step_a", result="pass")],
        )
        assert evaluator.should_skip(cond) is True


# ---------------------------------------------------------------------------
# Tests: product_context
# ---------------------------------------------------------------------------


class TestProductContext:
    """Tests for product_context condition type."""

    def test_skip_when_temperature_exceeds_threshold(self) -> None:
        """Given temperature=85, product_context(temperature > 80) → skip."""
        vs = VariableSpace()
        vs.set("scope.temperature", 85)
        evaluator = AdaptiveConditionEvaluator(variable_space=vs)

        cond = SkipConditions(
            conditions=[ProductContextCondition(
                variable="scope.temperature", operator_str=">", value=80.0,
            )],
        )
        assert evaluator.should_skip(cond) is True

    def test_no_skip_when_temperature_below_threshold(self) -> None:
        """Given temperature=70, product_context(temperature > 80) → no skip."""
        vs = VariableSpace()
        vs.set("scope.temperature", 70)
        evaluator = AdaptiveConditionEvaluator(variable_space=vs)

        cond = SkipConditions(
            conditions=[ProductContextCondition(
                variable="scope.temperature", operator_str=">", value=80.0,
            )],
        )
        assert evaluator.should_skip(cond) is False

    def test_all_comparison_operators(self) -> None:
        """All six comparison operators should work correctly."""
        vs = VariableSpace()
        vs.set("scope.voltage", 5.0)
        evaluator = AdaptiveConditionEvaluator(variable_space=vs)

        cases = [
            ("==", 5.0, True),
            ("==", 3.3, False),
            ("!=", 3.3, True),
            ("!=", 5.0, False),
            (">", 3.3, True),
            (">", 5.0, False),
            (">=", 5.0, True),
            (">=", 5.1, False),
            ("<", 5.1, True),
            ("<", 5.0, False),
            ("<=", 5.0, True),
            ("<=", 4.9, False),
        ]

        for op, val, expected in cases:
            cond = SkipConditions(
                conditions=[ProductContextCondition(
                    variable="scope.voltage", operator_str=op, value=val,
                )],
            )
            assert evaluator.should_skip(cond) is expected, f"Failed: {op} {val}"

    def test_no_skip_when_variable_missing(self) -> None:
        """When the variable doesn't exist, condition → no skip."""
        vs = VariableSpace()
        evaluator = AdaptiveConditionEvaluator(variable_space=vs)

        cond = SkipConditions(
            conditions=[ProductContextCondition(
                variable="scope.missing", operator_str=">", value=0.0,
            )],
        )
        assert evaluator.should_skip(cond) is False

    def test_no_skip_when_variable_non_numeric(self) -> None:
        """When the variable is a string, condition → no skip."""
        vs = VariableSpace()
        vs.set("scope.label", "hello")
        evaluator = AdaptiveConditionEvaluator(variable_space=vs)

        cond = SkipConditions(
            conditions=[ProductContextCondition(
                variable="scope.label", operator_str="==", value=0.0,
            )],
        )
        assert evaluator.should_skip(cond) is False


# ---------------------------------------------------------------------------
# Tests: batch_quality (Cpk)
# ---------------------------------------------------------------------------


class TestBatchQuality:
    """Tests for batch_quality condition type with SPC Cpk."""

    def test_skip_when_cpk_exceeds_threshold(self) -> None:
        """Given Cpk=2.5, batch_quality(Cpk > 2.0) → skip (excellent quality)."""
        spc = FakeSPCProcessor({
            ("WidgetA", "voltage"): FakeSPCStats(cpk=2.5, sample_count=50),
        })
        evaluator = AdaptiveConditionEvaluator(spc_processor=spc)

        cond = SkipConditions(
            conditions=[BatchQualityCondition(
                product_type="WidgetA",
                measurement_name="voltage",
                operator_str=">",
                threshold=2.0,
            )],
        )
        assert evaluator.should_skip(cond) is True

    def test_no_skip_when_cpk_below_threshold(self) -> None:
        """Given Cpk=1.0, batch_quality(Cpk > 2.0) → no skip."""
        spc = FakeSPCProcessor({
            ("WidgetA", "voltage"): FakeSPCStats(cpk=1.0, sample_count=50),
        })
        evaluator = AdaptiveConditionEvaluator(spc_processor=spc)

        cond = SkipConditions(
            conditions=[BatchQualityCondition(
                product_type="WidgetA",
                measurement_name="voltage",
                operator_str=">",
                threshold=2.0,
            )],
        )
        assert evaluator.should_skip(cond) is False

    def test_skip_when_cpk_below_threshold_low_quality(self) -> None:
        """Given Cpk=0.5, batch_quality(Cpk < 1.0) → skip (poor quality)."""
        spc = FakeSPCProcessor({
            ("WidgetA", "resistance"): FakeSPCStats(cpk=0.5, sample_count=30),
        })
        evaluator = AdaptiveConditionEvaluator(spc_processor=spc)

        cond = SkipConditions(
            conditions=[BatchQualityCondition(
                product_type="WidgetA",
                measurement_name="resistance",
                operator_str="<",
                threshold=1.0,
            )],
        )
        assert evaluator.should_skip(cond) is True

    def test_graceful_degradation_no_spc(self) -> None:
        """Without SPC processor, batch_quality → no skip (fail-safe)."""
        evaluator = AdaptiveConditionEvaluator(spc_processor=None)

        cond = SkipConditions(
            conditions=[BatchQualityCondition(
                product_type="WidgetA",
                measurement_name="voltage",
                operator_str=">",
                threshold=2.0,
            )],
        )
        assert evaluator.should_skip(cond) is False

    def test_no_skip_when_cpk_none(self) -> None:
        """When Cpk is None (no spec limits), condition → no skip."""
        spc = FakeSPCProcessor({
            ("WidgetA", "voltage"): FakeSPCStats(cpk=None, sample_count=5),
        })
        evaluator = AdaptiveConditionEvaluator(spc_processor=spc)

        cond = SkipConditions(
            conditions=[BatchQualityCondition(
                product_type="WidgetA",
                measurement_name="voltage",
                operator_str=">",
                threshold=2.0,
            )],
        )
        assert evaluator.should_skip(cond) is False

    def test_skip_with_dict_stats(self) -> None:
        """Should handle dict-style stats (not just Pydantic model)."""
        class DictSPC:
            def get_statistics(self, pt: str, mn: str) -> dict[str, Any]:
                return {"cpk": 1.67, "sample_count": 100}

        evaluator = AdaptiveConditionEvaluator(spc_processor=DictSPC())

        cond = SkipConditions(
            conditions=[BatchQualityCondition(
                product_type="X", measurement_name="Y",
                operator_str=">=", threshold=1.33,
            )],
        )
        assert evaluator.should_skip(cond) is True

    def test_early_stage_good_cpk_skips_later_steps(self) -> None:
        """DTA-QC scenario: early-stage Cpk good → later test step skipped.

        This is the core verification: when early-stage measurements show
        excellent Cpk (>2.0), a later redundant test step is adaptively
        skipped because the process is demonstrably in control.
        """
        spc = FakeSPCProcessor({
            ("ProductX", "early_voltage"): FakeSPCStats(cpk=2.33, sample_count=50),
        })
        evaluator = AdaptiveConditionEvaluator(spc_processor=spc)

        # Later test step declares: skip if early Cpk > 2.0
        late_step_cond = SkipConditions(
            conditions=[BatchQualityCondition(
                product_type="ProductX",
                measurement_name="early_voltage",
                operator_str=">",
                threshold=2.0,
            )],
            reason="Early-stage Cpk excellent — redundant late test skipped",
        )
        assert evaluator.should_skip(late_step_cond) is True
        assert evaluator.evaluate_skip_reason(late_step_cond) == (
            "Early-stage Cpk excellent — redundant late test skipped"
        )


# ---------------------------------------------------------------------------
# Tests: fault_probability
# ---------------------------------------------------------------------------


class TestFaultProbability:
    """Tests for fault_probability condition type."""

    def test_skip_when_probability_below_threshold(self) -> None:
        """Given prob=0.05, fault_probability(prob < 0.1) → skip (low-risk)."""
        predictor = FakeFaultPredictor({"step_x": 0.05})
        evaluator = AdaptiveConditionEvaluator(fault_predictor=predictor)

        cond = SkipConditions(
            conditions=[FaultProbabilityCondition(
                step_id="step_x", operator_str="<", threshold=0.1,
            )],
        )
        assert evaluator.should_skip(cond) is True

    def test_no_skip_when_probability_above_threshold(self) -> None:
        """Given prob=0.8, fault_probability(prob < 0.1) → no skip."""
        predictor = FakeFaultPredictor({"step_x": 0.8})
        evaluator = AdaptiveConditionEvaluator(fault_predictor=predictor)

        cond = SkipConditions(
            conditions=[FaultProbabilityCondition(
                step_id="step_x", operator_str="<", threshold=0.1,
            )],
        )
        assert evaluator.should_skip(cond) is False

    def test_skip_when_probability_above_threshold_high_risk(self) -> None:
        """Given prob=0.9, fault_probability(prob > 0.7) → skip (high-risk)."""
        predictor = FakeFaultPredictor({"step_y": 0.9})
        evaluator = AdaptiveConditionEvaluator(fault_predictor=predictor)

        cond = SkipConditions(
            conditions=[FaultProbabilityCondition(
                step_id="step_y", operator_str=">", threshold=0.7,
            )],
        )
        assert evaluator.should_skip(cond) is True

    def test_graceful_degradation_no_predictor(self) -> None:
        """Without FaultPredictor, fault_probability → no skip (fail-safe)."""
        evaluator = AdaptiveConditionEvaluator(fault_predictor=None)

        cond = SkipConditions(
            conditions=[FaultProbabilityCondition(
                step_id="step_z", operator_str="<", threshold=0.1,
            )],
        )
        assert evaluator.should_skip(cond) is False

    def test_default_probability_for_unknown_step(self) -> None:
        """Unknown step gets default 0.5 probability."""
        predictor = FakeFaultPredictor({})  # empty — all unknown
        evaluator = AdaptiveConditionEvaluator(fault_predictor=predictor)

        cond = SkipConditions(
            conditions=[FaultProbabilityCondition(
                step_id="unknown", operator_str="==", threshold=0.5,
            )],
        )
        assert evaluator.should_skip(cond) is True


# ---------------------------------------------------------------------------
# Tests: combined conditions (logical AND)
# ---------------------------------------------------------------------------


class TestCombinedConditions:
    """Tests for multiple conditions combined with logical AND."""

    def test_all_conditions_true_skips(self) -> None:
        """When all conditions are True, step is skipped."""
        spc = FakeSPCProcessor({
            ("Widget", "v"): FakeSPCStats(cpk=2.5),
        })
        predictor = FakeFaultPredictor({"late_step": 0.02})
        vs = VariableSpace()
        vs.set("scope.temperature", 90)

        evaluator = AdaptiveConditionEvaluator(
            spc_processor=spc,
            fault_predictor=predictor,
            variable_space=vs,
            step_results={"early_step": {"status": StepStatus.PASSED}},
        )

        cond = SkipConditions(
            conditions=[
                DependsOnResultCondition(step_id="early_step", result="PASS"),
                ProductContextCondition(
                    variable="scope.temperature", operator_str=">", value=80.0,
                ),
                BatchQualityCondition(
                    product_type="Widget", measurement_name="v",
                    operator_str=">", threshold=2.0,
                ),
                FaultProbabilityCondition(
                    step_id="late_step", operator_str="<", threshold=0.1,
                ),
            ],
        )
        assert evaluator.should_skip(cond) is True

    def test_one_condition_false_no_skip(self) -> None:
        """When one condition is False, step is NOT skipped."""
        spc = FakeSPCProcessor({
            ("Widget", "v"): FakeSPCStats(cpk=0.8),  # LOW Cpk — condition fails
        })
        predictor = FakeFaultPredictor({"late_step": 0.02})
        vs = VariableSpace()
        vs.set("scope.temperature", 90)

        evaluator = AdaptiveConditionEvaluator(
            spc_processor=spc,
            fault_predictor=predictor,
            variable_space=vs,
            step_results={"early_step": {"status": StepStatus.PASSED}},
        )

        cond = SkipConditions(
            conditions=[
                DependsOnResultCondition(step_id="early_step", result="PASS"),
                BatchQualityCondition(
                    product_type="Widget", measurement_name="v",
                    operator_str=">", threshold=2.0,  # Cpk=0.8 < 2.0 → False
                ),
            ],
        )
        assert evaluator.should_skip(cond) is False

    def test_empty_conditions_no_skip(self) -> None:
        """Empty conditions list → no skip."""
        evaluator = AdaptiveConditionEvaluator()
        cond = SkipConditions(conditions=[])
        assert evaluator.should_skip(cond) is False


# ---------------------------------------------------------------------------
# Tests: evaluate_skip_reason
# ---------------------------------------------------------------------------


class TestSkipReason:
    """Tests for evaluate_skip_reason()."""

    def test_custom_reason_returned(self) -> None:
        """When skipping, the declared reason is returned."""
        step_results = {"s1": {"status": StepStatus.PASSED}}
        evaluator = AdaptiveConditionEvaluator(step_results=step_results)

        cond = SkipConditions(
            conditions=[DependsOnResultCondition(step_id="s1", result="PASS")],
            reason="Step s1 passed — redundant check skipped",
        )
        assert evaluator.evaluate_skip_reason(cond) == "Step s1 passed — redundant check skipped"

    def test_generated_reason_when_none(self) -> None:
        """When no reason declared, a generated reason is returned."""
        step_results = {"s1": {"status": StepStatus.PASSED}}
        evaluator = AdaptiveConditionEvaluator(step_results=step_results)

        cond = SkipConditions(
            conditions=[DependsOnResultCondition(step_id="s1", result="PASS")],
            reason=None,
        )
        reason = evaluator.evaluate_skip_reason(cond)
        assert reason is not None
        assert "Adaptive skip" in reason
        assert "DependsOnResultCondition" in reason

    def test_none_when_not_skipping(self) -> None:
        """When not skipping, None is returned."""
        evaluator = AdaptiveConditionEvaluator(step_results={})
        cond = SkipConditions(
            conditions=[DependsOnResultCondition(step_id="s1", result="PASS")],
        )
        assert evaluator.evaluate_skip_reason(cond) is None


# ---------------------------------------------------------------------------
# Tests: parse_skip_conditions
# ---------------------------------------------------------------------------


class TestParseSkipConditions:
    """Tests for parse_skip_conditions() boundary parsing."""

    def test_parse_depends_on_result(self) -> None:
        """Parse a depends_on_result condition dict."""
        raw = {
            "conditions": [
                {"type": "depends_on_result", "step_id": "s1", "result": "PASS"},
            ],
            "reason": "test",
        }
        parsed = parse_skip_conditions(raw)
        assert parsed.reason == "test"
        assert len(parsed.conditions) == 1
        assert isinstance(parsed.conditions[0], DependsOnResultCondition)
        assert parsed.conditions[0].step_id == "s1"
        assert parsed.conditions[0].result == "PASS"

    def test_parse_product_context(self) -> None:
        """Parse a product_context condition dict."""
        raw = {
            "conditions": [
                {"type": "product_context", "variable": "scope.temp",
                 "operator": ">", "value": 80},
            ],
        }
        parsed = parse_skip_conditions(raw)
        assert isinstance(parsed.conditions[0], ProductContextCondition)
        assert parsed.conditions[0].value == 80.0

    def test_parse_batch_quality(self) -> None:
        """Parse a batch_quality condition dict."""
        raw = {
            "conditions": [
                {"type": "batch_quality", "product_type": "Widget",
                 "measurement_name": "voltage", "operator": ">",
                 "threshold": 2.0},
            ],
        }
        parsed = parse_skip_conditions(raw)
        assert isinstance(parsed.conditions[0], BatchQualityCondition)
        assert parsed.conditions[0].threshold == 2.0

    def test_parse_fault_probability(self) -> None:
        """Parse a fault_probability condition dict."""
        raw = {
            "conditions": [
                {"type": "fault_probability", "step_id": "s1",
                 "operator": "<", "threshold": 0.1},
            ],
        }
        parsed = parse_skip_conditions(raw)
        assert isinstance(parsed.conditions[0], FaultProbabilityCondition)

    def test_parse_multiple_conditions(self) -> None:
        """Parse multiple conditions of different types."""
        raw = {
            "conditions": [
                {"type": "depends_on_result", "step_id": "s1", "result": "FAIL"},
                {"type": "batch_quality", "product_type": "X",
                 "measurement_name": "Y", "operator": "<", "threshold": 1.0},
            ],
        }
        parsed = parse_skip_conditions(raw)
        assert len(parsed.conditions) == 2

    def test_parse_empty_conditions(self) -> None:
        """Parse with no conditions."""
        parsed = parse_skip_conditions({"conditions": []})
        assert parsed.conditions == []
        assert parsed.reason is None

    def test_parse_unknown_type_raises(self) -> None:
        """Unknown condition type raises ValueError."""
        with pytest.raises(ValueError, match="unknown type"):
            parse_skip_conditions({
                "conditions": [{"type": "unknown_type"}],
            })

    def test_parse_missing_type_raises(self) -> None:
        """Missing 'type' key raises ValueError."""
        with pytest.raises(ValueError, match="missing 'type'"):
            parse_skip_conditions({"conditions": [{}]})

    def test_parse_missing_required_field_raises(self) -> None:
        """Missing required field raises ValueError."""
        with pytest.raises(ValueError, match="requires 'step_id'"):
            parse_skip_conditions({
                "conditions": [{"type": "depends_on_result", "result": "PASS"}],
            })

    def test_parse_invalid_result_raises(self) -> None:
        """Invalid result value raises ValueError."""
        with pytest.raises(ValueError, match="must be 'PASS' or 'FAIL'"):
            parse_skip_conditions({
                "conditions": [
                    {"type": "depends_on_result", "step_id": "s1", "result": "MAYBE"},
                ],
            })

    def test_parse_invalid_operator_raises(self) -> None:
        """Invalid operator raises ValueError."""
        with pytest.raises(ValueError, match="operator"):
            parse_skip_conditions({
                "conditions": [
                    {"type": "product_context", "variable": "v",
                     "operator": "~=", "value": 1},
                ],
            })

    def test_parse_lowercase_result_normalized(self) -> None:
        """Lowercase 'pass' is normalized to 'PASS'."""
        parsed = parse_skip_conditions({
            "conditions": [
                {"type": "depends_on_result", "step_id": "s1", "result": "pass"},
            ],
        })
        assert parsed.conditions[0].result == "PASS"


# ---------------------------------------------------------------------------
# Tests: prune_dependency_graph
# ---------------------------------------------------------------------------


class TestPruneDependencyGraph:
    """Tests for prune_dependency_graph() at schedule time."""

    def test_prune_returns_skipped_steps(self) -> None:
        """Steps with satisfied skip conditions are returned for pruning."""
        spc = FakeSPCProcessor({
            ("P", "m"): FakeSPCStats(cpk=2.5),
        })
        evaluator = AdaptiveConditionEvaluator(spc_processor=spc)

        step_conditions = {
            "step_a": SkipConditions(
                conditions=[BatchQualityCondition(
                    product_type="P", measurement_name="m",
                    operator_str=">", threshold=2.0,
                )],
            ),
            "step_b": SkipConditions(
                conditions=[BatchQualityCondition(
                    product_type="P", measurement_name="m",
                    operator_str="<", threshold=1.0,  # Cpk=2.5 < 1.0 → False
                )],
            ),
            "step_c": SkipConditions(conditions=[]),  # no conditions
        }

        pruned = evaluator.prune_dependency_graph(step_conditions)
        assert pruned == {"step_a"}

    def test_prune_empty_when_no_conditions(self) -> None:
        """No conditions → empty prune set."""
        evaluator = AdaptiveConditionEvaluator()
        assert evaluator.prune_dependency_graph({}) == set()

    def test_prune_all_when_all_skip(self) -> None:
        """All steps skip → all returned."""
        step_results = {"s1": {"status": StepStatus.PASSED}}
        evaluator = AdaptiveConditionEvaluator(step_results=step_results)

        step_conditions = {
            "a": SkipConditions(
                conditions=[DependsOnResultCondition(step_id="s1", result="PASS")],
            ),
            "b": SkipConditions(
                conditions=[DependsOnResultCondition(step_id="s1", result="PASS")],
            ),
        }
        pruned = evaluator.prune_dependency_graph(step_conditions)
        assert pruned == {"a", "b"}


# ---------------------------------------------------------------------------
# Tests: ScannerScheduler integration
# ---------------------------------------------------------------------------


class TestScannerSchedulerIntegration:
    """Integration tests for AdaptiveConditionEvaluator with ScannerScheduler."""

    def test_scheduler_uses_adaptive_evaluator_for_skip(self) -> None:
        """ScannerScheduler._evaluate_step_skip should use AdaptiveConditionEvaluator.

        Verifies that when an AdaptiveConditionEvaluator is attached to the
        scheduler, context-aware skip conditions are evaluated.
        """
        from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
        from ate_platform.scheduler.event_bus import EventBus
        from ate_platform.scheduler.resource_manager import ResourceManager
        from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
        from ate_platform.scheduler.step_registry import StepRegistry

        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        # Set up product context: high temperature
        variable_space.set("scope.temperature", 95)

        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        # Build adaptive evaluator with product_context condition
        adaptive = AdaptiveConditionEvaluator(variable_space=variable_space)
        adaptive_conditions = SkipConditions(
            conditions=[ProductContextCondition(
                variable="scope.temperature", operator_str=">", value=80.0,
            )],
            reason="Temperature too high — skipping thermal test",
        )

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
            adaptive_skip_evaluator=adaptive,
        )

        # Register adaptive skip conditions
        scheduler.register_adaptive_skip_conditions({
            "thermal_test": adaptive_conditions,
        })

        registry.register("thermal_test")

        # _evaluate_step_skip should return True (skip)
        assert scheduler._evaluate_step_skip("thermal_test") is True

    def test_scheduler_falls_back_to_basic_skip_without_adaptive(self) -> None:
        """Without adaptive evaluator, scheduler uses basic skip_if expression."""
        from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
        from ate_platform.scheduler.event_bus import EventBus
        from ate_platform.scheduler.resource_manager import ResourceManager
        from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
        from ate_platform.scheduler.step_registry import StepRegistry

        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        variable_space.set("scope.debug_mode", "1")
        evaluator = ConditionEvaluator({}, variable_space=variable_space)

        # No adaptive_skip_evaluator — falls back to basic expression
        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
        )

        scheduler.register_skip_conditions({
            "step1": ('"${scope.debug_mode}" == "1"', "Debug mode"),
        })

        registry.register("step1")
        assert scheduler._evaluate_step_skip("step1") is True

    def test_scheduler_adaptive_skip_no_match(self) -> None:
        """When adaptive conditions don't match, step is not skipped."""
        from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
        from ate_platform.scheduler.event_bus import EventBus
        from ate_platform.scheduler.resource_manager import ResourceManager
        from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
        from ate_platform.scheduler.step_registry import StepRegistry

        event_bus = EventBus()
        registry = StepRegistry(event_bus=event_bus)
        variable_space = VariableSpace()
        resource_manager = ResourceManager()

        # Temperature is normal — should NOT skip
        variable_space.set("scope.temperature", 25)

        evaluator = ConditionEvaluator({}, variable_space=variable_space)
        adaptive = AdaptiveConditionEvaluator(variable_space=variable_space)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=evaluator,
            variable_space=variable_space,
            resource_manager=resource_manager,
            adaptive_skip_evaluator=adaptive,
        )

        scheduler.register_adaptive_skip_conditions({
            "thermal_test": SkipConditions(
                conditions=[ProductContextCondition(
                    variable="scope.temperature", operator_str=">", value=80.0,
                )],
            ),
        })

        registry.register("thermal_test")
        assert scheduler._evaluate_step_skip("thermal_test") is False
