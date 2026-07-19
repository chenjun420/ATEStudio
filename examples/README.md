# ATE Platform Examples

This directory contains example test scripts demonstrating the ATE Platform's capabilities.

## Overview

The examples show how to:
- Use the `ContextProxy` for variable and resource access
- Use `@measure` decorator to declare output variables
- Work with mock instrument drivers (DMM, PSU)
- Structure test scripts for execution

## Directory Structure

```
examples/
├── scripts/              # Test scripts using ContextProxy API
│   ├── voltage_test.py   # Voltage measurement test (DMM)
│   ├── current_test.py   # Current measurement test (DMM)
│   └── power_on_test.py  # Power-on test (PSU)
├── run_test.py           # Test runner script
├── test_pass.py          # Simple passing test (legacy)
├── test_fail.py          # Simple failing test (legacy)
├── test_error.py         # Error test (legacy)
├── test_timeout.py       # Timeout test (legacy)
└── test_with_outputs.py  # Multiple outputs test (legacy)
```

## Running Example Scripts

### Using the Test Runner

Run any example script with the test runner:

```bash
# Run voltage test (mock mode by default)
python -m examples.run_test voltage_test

# Run current test
python -m examples.run_test current_test

# Run power-on test
python -m examples.run_test power_on_test
```

### Command Line Options

```
usage: run_test.py [-h] [--mock] [--real] script

positional arguments:
  script      Name of the test script (e.g., voltage_test)

optional arguments:
  -h, --help  show this help message and exit
  --mock      Run in mock mode (default: True)
  --real      Run with real hardware (overrides --mock)
```

## Script Structure

All test scripts follow a consistent pattern:

### 1. Imports

```python
from ate_platform.executor.context_proxy import ContextProxy, measure
from ate_platform.drivers.examples.dmm import MockDMMDriver
from ate_platform.types import StepResult, StepStatus
```

### 2. Decorator and Function

```python
@measure("output1", "output2")  # Declare outputs
def main(context: ContextProxy) -> StepResult:
    """Execute test."""
    ...
```

### 3. Execution Logic

```python
# Access variables
voltage = context.get("scope.voltage", 3.3)

# Create driver and connect
dmm = MockDMMDriver()
dmm.connect("MOCK::DMM")

try:
    # Perform measurement
    measured = dmm.measure_voltage()
    
    # Write outputs
    context["voltage"] = measured
    
    # Return result
    return StepResult(status=StepStatus.PASSED, outputs={"voltage": measured})
finally:
    dmm.disconnect()
```

## Example Scripts

### voltage_test.py

Measures DC voltage using a DMM (Digital Multimeter).

- **Purpose**: Demonstrate voltage measurement
- **Driver**: `MockDMMDriver`
- **Outputs**: `voltage`, `voltage_pass`
- **Validation**: Checks if voltage is within tolerance

### current_test.py

Measures DC current using a DMM.

- **Purpose**: Demonstrate current measurement
- **Driver**: `MockDMMDriver`
- **Outputs**: `current`, `current_pass`
- **Validation**: Checks if current is within tolerance

### power_on_test.py

Power-on test using a PSU (Power Supply Unit).

- **Purpose**: Demonstrate power control
- **Driver**: `MockPSUDriver`
- **Outputs**: `output_voltage`, `output_current`, `power_on_pass`
- **Operations**: Set voltage/current, enable output, verify readings

## Mock Drivers

The examples use mock drivers that simulate instrument behavior:

- **MockDMMDriver**: Returns random but realistic measurement values
  - Voltage: 3.3V, 5V, 12V, or 24V with small variation
  - Current: 0.1A to 2A
  - Resistance: 100Ω to 10kΩ

- **MockPSUDriver**: Tracks channel state and simulates output
  - Supports 3 channels
  - Tracks voltage/current settings
  - Returns measured values based on output state

## ContextProxy API

The `ContextProxy` provides safe access to execution context:

### Variable Access

```python
# Read variable
value = context["scope.voltage"]

# Write to step outputs
context["result"] = 3.3
```

### Resource Access

```python
# Get resource proxy
resource = context.resource("DMM_CH1")

# Acquire/release
if resource.acquire(timeout=5.0):
    # Use resource
    resource.release()
```

### Logging

```python
context.log("info", "Measurement complete")
context.log("warning", "Value out of range")
context.log("error", "Connection failed")
```

## Creating New Test Scripts

To create a new test script:

1. Create file in `examples/scripts/`
2. Import required modules
3. Add `@measure` decorator with output names
4. Define `main(context: ContextProxy) -> StepResult`
5. Implement test logic
6. Return `StepResult` with status

## Testing

Run the scripts to verify they work:

```bash
# Run all example scripts
python -m examples.run_test voltage_test
python -m examples.run_test current_test
python -m examples.run_test power_on_test

# Check exit codes (0 = pass, 1 = fail)
echo $?  # Linux/Mac
echo %ERRORLEVEL%  # Windows
```

## Notes

- All examples use mock mode by default
- Mock mode requires no real hardware
- Scripts can be run standalone or via executor
- Output validation uses realistic tolerance values