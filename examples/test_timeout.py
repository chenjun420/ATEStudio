"""Example test script that times out.

This script demonstrates a test that runs longer than allowed timeout.
It can be executed by ProcessExecutor in an isolated process.

Usage:
    Executed via ProcessExecutor.execute() with params and short timeout.
    
Expected behavior:
    - Sleeps for extended duration
    - Should trigger timeout in executor
"""

import time

# Access parameters passed from executor
sleep_duration = params.get("sleep_duration", 30)  # noqa: F821

# Sleep for a long time (longer than typical timeout)
time.sleep(sleep_duration)

# This line may not execute if timeout occurs
measured_value = 0
