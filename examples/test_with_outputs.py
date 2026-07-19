"""Example test script with multiple outputs.

This script demonstrates a test that produces multiple output values.
It can be executed by ProcessExecutor in an isolated process.

Usage:
    Executed via ProcessExecutor.execute() with params.
    
Expected behavior:
    - Sets multiple output values
    - Returns PASSED status with outputs dict
"""

# Access parameters passed from executor
voltage_input = params.get("voltage", 3.3)  # noqa: F821
current_input = params.get("current", 0.5)  # noqa: F821

# Calculate results
power = voltage_input * current_input
resistance = voltage_input / current_input if current_input > 0 else 0

# Set outputs (these will be captured by ProcessExecutor)
measured_voltage = voltage_input
measured_current = current_input
calculated_power = power
calculated_resistance = resistance

# Verify
assert power > 0, f"Power should be positive, got {power}"
