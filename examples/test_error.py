"""Example test script that errors.

This script demonstrates a test that encounters an unexpected error.
It can be executed by ProcessExecutor in an isolated process.

Usage:
    Executed via ProcessExecutor.execute() with params.
    
Expected behavior:
    - Raises RuntimeError
    - Returns ERROR status
"""

# Access parameters passed from executor
simulate_error = params.get("simulate_error", True)  # noqa: F821

if simulate_error:
    raise RuntimeError("Simulated error in test script")

# This line never executes
measured_value = 0
