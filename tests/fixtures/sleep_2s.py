"""Fixture script that sleeps for 2 seconds (used for pool saturation tests)."""

import time

time.sleep(2.0)

# Output a result to satisfy the step execution protocol
result_status = "passed"