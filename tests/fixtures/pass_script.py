"""Minimal test script that always passes.

Used as a fixture for executor tests. Sets an explicit result dict
so the executor can extract status and outputs.
"""

result = {"status": "PASSED"}
