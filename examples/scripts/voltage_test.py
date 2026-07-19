"""Voltage measurement test script using DMM driver.

This script demonstrates a voltage measurement test using the MockDMMDriver.
It measures DC voltage and validates against expected range.

Usage:
    python -m examples.run_test voltage_test --mock
    
Expected behavior:
    - Connects to mock DMM
    - Measures voltage
    - Validates voltage is within acceptable range
    - Returns PASSED/FAILED status
"""

from ate_platform.executor.context_proxy import ContextProxy, measure
from ate_platform.drivers.examples.dmm import MockDMMDriver
from ate_platform.types import StepResult, StepStatus


@measure("voltage", "voltage_pass")
def main(context: ContextProxy) -> StepResult:
    """Execute voltage measurement test.
    
    Args:
        context: Execution context providing variable access and resources.
        
    Returns:
        StepResult with status and measured voltage.
    """
    # Get expected voltage from context (with default)
    expected_voltage = context["scope.expected_voltage"]
    if expected_voltage is None:
        expected_voltage = 3.3
    tolerance = context["scope.tolerance"]
    if tolerance is None:
        tolerance = 0.1
    
    context.log("info", f"Starting voltage test, expected: {expected_voltage}V")
    
    # Create and connect to mock DMM
    dmm = MockDMMDriver()
    dmm.connect("MOCK::DMM")
    
    try:
        # Measure voltage
        measured_voltage = dmm.measure_voltage(channel=1)
        context.log("info", f"Measured voltage: {measured_voltage:.4f}V")
        
        # Validate against expected range
        lower_bound = expected_voltage - tolerance
        upper_bound = expected_voltage + tolerance
        
        if lower_bound <= measured_voltage <= upper_bound:
            context["voltage"] = measured_voltage
            context["voltage_pass"] = True
            context.log("info", f"Voltage within range [{lower_bound}, {upper_bound}]")
            return StepResult(status=StepStatus.PASSED, outputs={"voltage": measured_voltage, "voltage_pass": True})
        else:
            context["voltage"] = measured_voltage
            context["voltage_pass"] = False
            context.log("warning", f"Voltage {measured_voltage:.4f}V outside range [{lower_bound}, {upper_bound}]")
            return StepResult(
                status=StepStatus.FAILED,
                outputs={"voltage": measured_voltage, "voltage_pass": False},
                error=f"Voltage {measured_voltage:.4f}V outside expected range"
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
        _step_id="voltage_test"
    )
    
    result = main(proxy)
    print(f"Status: {result.status.value}")
    print(f"Outputs: {result.outputs}")
    if result.error:
        print(f"Error: {result.error}")
