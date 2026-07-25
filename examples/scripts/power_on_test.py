"""Power-on test script using PSU driver.

This script demonstrates a power-on test using the MockDriverFactory.
It sets voltage/current limits, turns on the output, and validates
the output is stable.

Usage:
    python -m examples.run_test power_on_test --mock
    
Expected behavior:
    - Connects to mock PSU via MockDriverFactory
    - Sets voltage and current limit
    - Turns on output
    - Verifies voltage/current readings
    - Returns PASSED/FAILED status
"""

from ate_platform.executor.context_proxy import ContextProxy, measure
from ate_platform.drivers.examples.psu import PSUAbstraction
from ate_platform.drivers.mock_factory import MockDriverFactory
from ate_platform.types import StepResult, StepStatus


@measure("output_voltage", "output_current", "power_on_pass")
def main(context: ContextProxy) -> StepResult:
    """Execute power-on test.
    
    Args:
        context: Execution context providing variable access and resources.
        
    Returns:
        StepResult with status and measured values.
    """
    # Get configuration from context (with defaults)
    target_voltage = context["scope.target_voltage"]
    if target_voltage is None:
        target_voltage = 5.0
    current_limit = context["scope.current_limit"]
    if current_limit is None:
        current_limit = 1.0
    channel = context["scope.channel"]
    if channel is None:
        channel = 1
    voltage_tolerance = 0.05  # 5% tolerance
    
    context.log("info", f"Starting power-on test: {target_voltage}V @ {current_limit}A max")
    
    # Create and connect to mock PSU via factory
    psu = MockDriverFactory.create_mock(PSUAbstraction)
    psu.connect("MOCK::PSU")
    
    try:
        # Configure PSU
        psu.set_voltage(target_voltage)
        psu.set_current(current_limit)
        context.log("info", f"Configured: {target_voltage}V, {current_limit}A limit")
        
        # Turn on output
        psu.enable_output(True)
        context.log("info", "Output enabled")
        
        # Measure
        measured_voltage, measured_current = psu.measure_output()
        
        context.log("info", f"Measured: {measured_voltage:.4f}V, {measured_current:.4f}A")
        
        # Validate voltage is close to target
        voltage_error = abs(measured_voltage - target_voltage)
        max_voltage_error = target_voltage * voltage_tolerance
        
        if voltage_error <= max_voltage_error and measured_current >= 0:
            context["output_voltage"] = measured_voltage
            context["output_current"] = measured_current
            context["power_on_pass"] = True
            context.log("info", f"Power-on successful: voltage error {voltage_error:.4f}V within tolerance")
            return StepResult(
                status=StepStatus.PASSED,
                outputs={
                    "output_voltage": measured_voltage,
                    "output_current": measured_current,
                    "power_on_pass": True
                }
            )
        else:
            context["output_voltage"] = measured_voltage
            context["output_current"] = measured_current
            context["power_on_pass"] = False
            error_msg = f"Voltage error {voltage_error:.4f}V exceeds tolerance {max_voltage_error:.4f}V" if voltage_error > max_voltage_error else "Invalid current reading"
            context.log("warning", error_msg)
            return StepResult(
                status=StepStatus.FAILED,
                outputs={
                    "output_voltage": measured_voltage,
                    "output_current": measured_current,
                    "power_on_pass": False
                },
                error=error_msg
            )
            
    finally:
        # Always turn off output and disconnect
        psu.enable_output(False)
        psu.disconnect()
        context.log("info", "Output disabled and disconnected")


if __name__ == "__main__":
    # Standalone execution for testing
    from ate_platform.scheduler.variable_space import VariableSpace
    from ate_platform.scheduler.resource_manager import ResourceManager
    
    vs = VariableSpace()
    rm = ResourceManager()
    proxy = ContextProxy(
        _variable_space=vs,
        _resource_manager=rm,
        _step_id="power_on_test"
    )
    
    result = main(proxy)
    print(f"Status: {result.status.value}")
    print(f"Outputs: {result.outputs}")
    if result.error:
        print(f"Error: {result.error}")