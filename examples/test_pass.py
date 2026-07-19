"""Example test script that passes.

This script demonstrates a simple passing test.
It can be executed by ProcessExecutor in an isolated process.

Usage:
    Executed via ProcessExecutor.execute() with params.
    
Expected behavior:
    - Sets 'measured_value' output
    - Returns PASSED status
"""

# Access parameters passed from executor
test_value = params.get("value", 42)  # noqa: F821

# Simple test logic
measured_value = test_value * 2

# Verify result
assert measured_value == test_value * 2, f"Expected {test_value * 2}, got {measured_value}"

# The script passes if no exception is raised
