"""Unit tests for ConditionEvaluator.

Tests condition evaluation for step execution:
- Step status checks
- Expression evaluation
- Resource availability
- all/any combinations
- Timeout handling
"""

import asyncio
import time

import pytest

from ate_platform.exceptions import ConditionTimeoutError
from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import Condition, StepResult, StepStatus


class TestStepStatusChecks:
    """Test step status condition evaluation."""

    def test_step_status_passed(self):
        """Condition should pass when step has PASSED status."""
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        condition = Condition(step="step1", status="PASSED")
        assert evaluator.evaluate(condition) is True

    def test_step_status_failed(self):
        """Condition should fail when step has FAILED status but PASSED expected."""
        step_results = {
            "step1": StepResult(status=StepStatus.FAILED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        condition = Condition(step="step1", status="PASSED")
        assert evaluator.evaluate(condition) is False

    def test_step_status_running(self):
        """Condition should check RUNNING status correctly."""
        step_results = {
            "step1": StepResult(status=StepStatus.RUNNING, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        condition = Condition(step="step1", status="RUNNING")
        assert evaluator.evaluate(condition) is True

    def test_step_status_skipped(self):
        """Condition should check SKIPPED status correctly."""
        step_results = {
            "step1": StepResult(status=StepStatus.SKIPPED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        condition = Condition(step="step1", status="SKIPPED")
        assert evaluator.evaluate(condition) is True

    def test_step_status_error(self):
        """Condition should check ERROR status correctly."""
        step_results = {
            "step1": StepResult(status=StepStatus.ERROR, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        condition = Condition(step="step1", status="ERROR")
        assert evaluator.evaluate(condition) is True

    def test_step_status_pending(self):
        """Condition should check PENDING status correctly."""
        step_results = {
            "step1": StepResult(status=StepStatus.PENDING, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        condition = Condition(step="step1", status="PENDING")
        assert evaluator.evaluate(condition) is True

    def test_step_not_found_returns_false(self):
        """Condition should fail when referenced step doesn't exist."""
        evaluator = ConditionEvaluator({})

        condition = Condition(step="nonexistent", status="PASSED")
        assert evaluator.evaluate(condition) is False

    def test_step_status_with_dict_result(self):
        """Condition should work with dict results instead of StepResult."""
        step_results = {
            "step1": {"status": StepStatus.PASSED, "outputs": {}}
        }
        evaluator = ConditionEvaluator(step_results)

        condition = Condition(step="step1", status="PASSED")
        assert evaluator.evaluate(condition) is True

    def test_step_status_with_string_status(self):
        """Condition should work with string status in dict results."""
        step_results = {
            "step1": {"status": "PASSED", "outputs": {}}
        }
        evaluator = ConditionEvaluator(step_results)

        condition = Condition(step="step1", status="PASSED")
        assert evaluator.evaluate(condition) is True


class TestExpressionEvaluation:
    """Test expression condition evaluation."""

    def test_simple_comparison_true(self):
        """Expression with true comparison should pass."""
        evaluator = ConditionEvaluator({})

        condition = Condition(expression="5 > 3")
        assert evaluator.evaluate(condition) is True

    def test_simple_comparison_false(self):
        """Expression with false comparison should fail."""
        evaluator = ConditionEvaluator({})

        condition = Condition(expression="3 > 5")
        assert evaluator.evaluate(condition) is False

    def test_expression_with_variables(self):
        """Expression with set names should evaluate correctly."""
        evaluator = ConditionEvaluator({})
        evaluator.set_names({"voltage": 3.3, "threshold": 3.0})

        condition = Condition(expression="voltage > threshold")
        assert evaluator.evaluate(condition) is True

    def test_expression_with_variable_space_resolution(self):
        """Expression should resolve variables from VariableSpace."""
        vs = VariableSpace()
        vs.set("scope.voltage", 3.3)
        vs.set("scope.threshold", 3.0)

        evaluator = ConditionEvaluator({}, variable_space=vs)

        condition = Condition(expression="${scope.voltage} > ${scope.threshold}")
        assert evaluator.evaluate(condition) is True

    def test_expression_arithmetic(self):
        """Expression with arithmetic should work."""
        evaluator = ConditionEvaluator({})

        condition = Condition(expression="10 + 5 == 15")
        assert evaluator.evaluate(condition) is True

    def test_expression_boolean_logic(self):
        """Expression with boolean logic should work."""
        evaluator = ConditionEvaluator({})

        condition = Condition(expression="True and False")
        assert evaluator.evaluate(condition) is False

    def test_expression_with_parentheses(self):
        """Expression with parentheses should evaluate correctly."""
        evaluator = ConditionEvaluator({})

        condition = Condition(expression="(10 > 5) and (3 < 7)")
        assert evaluator.evaluate(condition) is True

    def test_expression_error_returns_false(self):
        """Invalid expression should return False."""
        evaluator = ConditionEvaluator({})

        condition = Condition(expression="undefined_var > 5")
        assert evaluator.evaluate(condition) is False

    def test_time_since_builtin_function(self):
        """time_since() builtin should return time since step completion."""
        step_times = {"step1": time.time() - 5.0}  # Completed 5 seconds ago
        evaluator = ConditionEvaluator({}, step_completion_times=step_times)

        condition = Condition(expression="time_since('step1') > 4.0")
        assert evaluator.evaluate(condition) is True

    def test_time_since_returns_none_for_unknown_step(self):
        """time_since() should return None for unknown steps."""
        evaluator = ConditionEvaluator({})

        condition = Condition(expression="time_since('unknown') is None")
        assert evaluator.evaluate(condition) is True

    def test_step_outputs_builtin_function(self):
        """step_outputs() builtin should retrieve step output values."""
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={"voltage": 3.3})
        }
        evaluator = ConditionEvaluator(step_results)

        condition = Condition(expression="step_outputs('step1', 'voltage') > 3.0")
        assert evaluator.evaluate(condition) is True

    def test_step_outputs_returns_none_for_missing_key(self):
        """step_outputs() should return None for missing keys."""
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        condition = Condition(expression="step_outputs('step1', 'missing') is None")
        assert evaluator.evaluate(condition) is True


class TestResourceAvailability:
    """Test resource availability condition evaluation."""

    def test_single_resource_available(self):
        """Condition should pass when resource is available."""
        rm = ResourceManager()
        evaluator = ConditionEvaluator({}, resource_manager=rm)

        condition = Condition(resource_available=["DMM_CH1"])
        assert evaluator.evaluate(condition) is True

    def test_single_resource_not_available(self):
        """Condition should fail when resource is held."""
        rm = ResourceManager()
        rm.acquire("DMM_CH1", "other_step")

        evaluator = ConditionEvaluator({}, resource_manager=rm)

        condition = Condition(resource_available=["DMM_CH1"])
        assert evaluator.evaluate(condition) is False

    def test_multiple_resources_all_available(self):
        """Condition should pass when all resources are available."""
        rm = ResourceManager()
        evaluator = ConditionEvaluator({}, resource_manager=rm)

        condition = Condition(resource_available=["DMM_CH1", "DMM_CH2", "GPIO_1"])
        assert evaluator.evaluate(condition) is True

    def test_multiple_resources_one_unavailable(self):
        """Condition should fail when any resource is held."""
        rm = ResourceManager()
        rm.acquire("DMM_CH1", "other_step")

        evaluator = ConditionEvaluator({}, resource_manager=rm)

        condition = Condition(resource_available=["DMM_CH1", "DMM_CH2"])
        assert evaluator.evaluate(condition) is False

    def test_no_resource_manager_returns_false(self):
        """Condition should fail when ResourceManager is not provided."""
        evaluator = ConditionEvaluator({})

        condition = Condition(resource_available=["DMM_CH1"])
        assert evaluator.evaluate(condition) is False

    def test_resource_becomes_available(self):
        """Condition should pass after resource is released."""
        rm = ResourceManager()
        rm.acquire("DMM_CH1", "step1")

        evaluator = ConditionEvaluator({}, resource_manager=rm)

        condition = Condition(resource_available=["DMM_CH1"])
        assert evaluator.evaluate(condition) is False

        rm.release("DMM_CH1", "step1")
        assert evaluator.evaluate(condition) is True


class TestCombinedConditions:
    """Test combined condition evaluation."""

    def test_step_status_and_expression(self):
        """Both step status and expression should be evaluated."""
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)
        evaluator.set_names({"value": 10})

        condition = Condition(step="step1", status="PASSED", expression="value > 5")
        assert evaluator.evaluate(condition) is True

    def test_step_status_and_expression_one_fails(self):
        """Condition should fail if any part fails."""
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)
        evaluator.set_names({"value": 3})

        condition = Condition(step="step1", status="PASSED", expression="value > 5")
        assert evaluator.evaluate(condition) is False

    def test_step_status_and_resource_available(self):
        """Both step status and resource should be checked."""
        rm = ResourceManager()
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results, resource_manager=rm)

        condition = Condition(step="step1", status="PASSED", resource_available=["DMM_CH1"])
        assert evaluator.evaluate(condition) is True

    def test_all_three_condition_types(self):
        """All condition types should be evaluated together."""
        rm = ResourceManager()
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={"voltage": 3.3})
        }
        evaluator = ConditionEvaluator(step_results, resource_manager=rm)

        condition = Condition(
            step="step1",
            status="PASSED",
            expression="step_outputs('step1', 'voltage') > 3.0",
            resource_available=["DMM_CH1"]
        )
        assert evaluator.evaluate(condition) is True

    def test_empty_condition_returns_true(self):
        """Condition with no fields should return True."""
        evaluator = ConditionEvaluator({})

        condition = Condition()
        assert evaluator.evaluate(condition) is True


class TestLogicalCombinations:
    """Test all/any logical combinations."""

    def test_evaluate_all_all_pass(self):
        """evaluate_all should return True when all conditions pass."""
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={}),
            "step2": StepResult(status=StepStatus.PASSED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        conditions = [
            Condition(step="step1", status="PASSED"),
            Condition(step="step2", status="PASSED")
        ]
        assert evaluator.evaluate_all(conditions) is True

    def test_evaluate_all_one_fails(self):
        """evaluate_all should return False when any condition fails."""
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={}),
            "step2": StepResult(status=StepStatus.FAILED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        conditions = [
            Condition(step="step1", status="PASSED"),
            Condition(step="step2", status="PASSED")
        ]
        assert evaluator.evaluate_all(conditions) is False

    def test_evaluate_any_all_pass(self):
        """evaluate_any should return True when all conditions pass."""
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={}),
            "step2": StepResult(status=StepStatus.PASSED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        conditions = [
            Condition(step="step1", status="PASSED"),
            Condition(step="step2", status="PASSED")
        ]
        assert evaluator.evaluate_any(conditions) is True

    def test_evaluate_any_one_passes(self):
        """evaluate_any should return True when at least one condition passes."""
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={}),
            "step2": StepResult(status=StepStatus.FAILED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        conditions = [
            Condition(step="step1", status="PASSED"),
            Condition(step="step2", status="PASSED")
        ]
        assert evaluator.evaluate_any(conditions) is True

    def test_evaluate_any_all_fail(self):
        """evaluate_any should return False when all conditions fail."""
        step_results = {
            "step1": StepResult(status=StepStatus.FAILED, outputs={}),
            "step2": StepResult(status=StepStatus.FAILED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        conditions = [
            Condition(step="step1", status="PASSED"),
            Condition(step="step2", status="PASSED")
        ]
        assert evaluator.evaluate_any(conditions) is False

    def test_evaluate_all_empty_list(self):
        """evaluate_all with empty list should return True."""
        evaluator = ConditionEvaluator({})

        assert evaluator.evaluate_all([]) is True

    def test_evaluate_any_empty_list(self):
        """evaluate_any with empty list should return False."""
        evaluator = ConditionEvaluator({})

        assert evaluator.evaluate_any([]) is False


class TestTimeoutHandling:
    """Test async wait_for_condition with timeout."""

    @pytest.mark.asyncio
    async def test_wait_for_condition_immediately_true(self):
        """wait_for_condition should return immediately if condition is already true."""
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        condition = Condition(step="step1", status="PASSED")

        start = time.time()
        result = await evaluator.wait_for_condition(condition, timeout=1.0)
        elapsed = time.time() - start

        assert result is True
        assert elapsed < 0.1  # Should return quickly

    @pytest.mark.asyncio
    async def test_wait_for_condition_timeout(self):
        """wait_for_condition should raise ConditionTimeoutError on timeout."""
        evaluator = ConditionEvaluator({})

        condition = Condition(step="nonexistent", status="PASSED")

        with pytest.raises(ConditionTimeoutError):
            await evaluator.wait_for_condition(condition, timeout=0.2)

    @pytest.mark.asyncio
    async def test_wait_for_condition_eventually_true(self):
        """wait_for_condition should succeed when condition becomes true."""
        step_results: dict = {}
        evaluator = ConditionEvaluator(step_results)

        async def add_result_later():
            await asyncio.sleep(0.1)
            step_results["step1"] = StepResult(status=StepStatus.PASSED, outputs={})

        task = asyncio.create_task(add_result_later())

        condition = Condition(step="step1", status="PASSED")

        start = time.time()
        result = await evaluator.wait_for_condition(condition, timeout=1.0)
        elapsed = time.time() - start

        await task
        assert result is True
        assert elapsed >= 0.1  # Should have waited

    @pytest.mark.asyncio
    async def test_wait_for_all_all_succeed(self):
        """wait_for_all should succeed when all conditions pass."""
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={}),
            "step2": StepResult(status=StepStatus.PASSED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        conditions = [
            Condition(step="step1", status="PASSED"),
            Condition(step="step2", status="PASSED")
        ]

        result = await evaluator.wait_for_all(conditions, timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_all_timeout(self):
        """wait_for_all should raise ConditionTimeoutError on timeout."""
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={}),
            "step2": StepResult(status=StepStatus.FAILED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        conditions = [
            Condition(step="step1", status="PASSED"),
            Condition(step="step2", status="PASSED")  # This will never pass
        ]

        with pytest.raises(ConditionTimeoutError):
            await evaluator.wait_for_all(conditions, timeout=0.2)

    @pytest.mark.asyncio
    async def test_wait_for_any_one_succeeds(self):
        """wait_for_any should succeed when one condition passes."""
        step_results = {
            "step1": StepResult(status=StepStatus.FAILED, outputs={}),
            "step2": StepResult(status=StepStatus.PASSED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        conditions = [
            Condition(step="step1", status="PASSED"),
            Condition(step="step2", status="PASSED")
        ]

        result = await evaluator.wait_for_any(conditions, timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_any_timeout(self):
        """wait_for_any should raise ConditionTimeoutError on timeout."""
        step_results = {
            "step1": StepResult(status=StepStatus.FAILED, outputs={}),
            "step2": StepResult(status=StepStatus.FAILED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        conditions = [
            Condition(step="step1", status="PASSED"),
            Condition(step="step2", status="PASSED")
        ]

        with pytest.raises(ConditionTimeoutError):
            await evaluator.wait_for_any(conditions, timeout=0.2)


class TestUpdateMethods:
    """Test update methods for evaluator state."""

    def test_update_step_completion_time(self):
        """update_step_completion_time should add/update completion times."""
        evaluator = ConditionEvaluator({})

        # Add a new completion time
        evaluator.update_step_completion_time("step1")

        # Should be in the dict
        assert "step1" in evaluator._step_completion_times
        assert evaluator._step_completion_times["step1"] > 0

    def test_update_step_completion_time_custom(self):
        """update_step_completion_time should accept custom time."""
        evaluator = ConditionEvaluator({})

        custom_time = 12345.0
        evaluator.update_step_completion_time("step1", custom_time)

        assert evaluator._step_completion_times["step1"] == custom_time

    def test_set_names(self):
        """set_names should add variables for expression evaluation."""
        evaluator = ConditionEvaluator({})

        evaluator.set_names({"voltage": 5.0, "current": 1.0})

        condition = Condition(expression="voltage * current == 5.0")
        assert evaluator.evaluate(condition) is True


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_none_step_status_in_dict(self):
        """Should handle None status in dict result."""
        step_results = {"step1": {"status": None, "outputs": {}}}
        evaluator = ConditionEvaluator(step_results)

        condition = Condition(step="step1", status="PASSED")
        assert evaluator.evaluate(condition) is False

    def test_invalid_status_string_in_dict(self):
        """Should handle invalid status string in dict result."""
        step_results = {"step1": {"status": "INVALID_STATUS", "outputs": {}}}
        evaluator = ConditionEvaluator(step_results)

        condition = Condition(step="step1", status="PASSED")
        assert evaluator.evaluate(condition) is False

    def test_expression_with_syntax_error(self):
        """Should return False for expression with syntax error."""
        evaluator = ConditionEvaluator({})

        condition = Condition(expression="5 > ")
        assert evaluator.evaluate(condition) is False

    def test_expression_division_by_zero(self):
        """Should return False for expression causing error."""
        evaluator = ConditionEvaluator({})

        condition = Condition(expression="1 / 0 > 0")
        assert evaluator.evaluate(condition) is False

    def test_multiple_conditions_with_one_type(self):
        """Should work with only one condition type specified."""
        step_results = {
            "step1": StepResult(status=StepStatus.PASSED, outputs={})
        }
        evaluator = ConditionEvaluator(step_results)

        # Only step/status specified
        condition = Condition(step="step1", status="PASSED")
        assert evaluator.evaluate(condition) is True

        # Only expression specified
        condition = Condition(expression="True")
        assert evaluator.evaluate(condition) is True
