"""Example test script that fails.

This script demonstrates a failing test with assertion error.
It can be executed by ProcessExecutor in an isolated process.

Usage:
    Executed via ProcessExecutor.execute() with params.
    
Expected behavior:
    - Raises AssertionError
    - Returns FAILED status
"""

# Access parameters passed from executor
expected = params.get("expected", 100)  # noqa: F821
actual = params.get("actual", 50)  # noqa: F821

# This will fail
assert actual == expected, f"Expected {expected}, got {actual}"
