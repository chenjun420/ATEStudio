"""Current measurement test script using DMM driver.

This script demonstrates a current measurement test using the MockDMMDriver.
It measures DC current and validates against expected range.

Usage:
    python -m examples.run_test current_test --mock
    
Expected behavior:
    - Connects to mock DMM
    - Measures current
    - Validates current is within acceptable range
    - Returns PASSED/FAILED status
"""

from ate_platform.executor.context_proxy import ContextProxy, measure
from ate_platform.drivers.examples.dmm import MockDMMDriver
from ate_platform.types import StepResult, StepStatus


@measure("current", "current_pass")
def main(context: ContextProxy) -> StepResult:
    """Execute current measurement test.
    
    Args:
        context: Execution context providing variable access and resources.
        
    Returns:
        StepResult with status and measured current.
    """
    # Get expected current from context (with default)
    expected_current = context["scope.expected_current"]
    if expected_current is None:
        expected_current = 0.5
    tolerance = context["scope.tolerance"]
    if tolerance is None:
        tolerance = 0.2
    
    context.log("info", f"Starting current test, expected: {expected_current}A")
    
    # Create and connect to mock DMM
    dmm = MockDMMDriver()
    dmm.connect("MOCK::DMM")
    
    try:
        # Measure current
        measured_current = dmm.measure_current(channel=1)
        context.log("info", f"Measured current: {measured_current:.4f}A")
        
        # Validate against expected range
        lower_bound = expected_current - tolerance
        upper_bound = expected_current + tolerance
        
        if lower_bound <= measured_current <= upper_bound:
            context["current"] = measured_current
            context["current_pass"] = True
            context.log("info", f"Current within range [{lower_bound}, {upper_bound}]")
            return StepResult(status=StepStatus.PASSED, outputs={"current": measured_current, "current_pass": True})
        else:
            context["current"] = measured_current
            context["current_pass"] = False
            context.log("warning", f"Current {measured_current:.4f}A outside range [{lower_bound}, {upper_bound}]")
            return StepResult(
                status=StepStatus.FAILED,
                outputs={"current": measured_current, "current_pass": False},
                error=f"Current {measured_current:.4f}A outside expected range"
            )
    finally:
        dmm.disconnect()


if __name__ == "__main__":
    # Standalone execution for testing
    from ate_platform.scheduler.variable_space import VariableSpace
    from ate_platform.scheduler.resource_manager import ResourceManager
    
    vs = VariableSpace()
    rm = ResourceManager()
    proxy = ContextProxy(
        _variable_space=vs,
        _resource_manager=rm,
        _step_id="current_test"
    )
    
    result = main(proxy)
    print(f"Status: {result.status.value}")
    print(f"Outputs: {result.outputs}")
    if result.error:
        print(f"Error: {result.error}")
