"""Validate step - always passes."""

# Get threshold from params
threshold = params.get("threshold", 3.0)  # noqa: F821

# Simulate validation - always pass for integration test
result = {
    "status": "PASSED",
    "outputs": {
        "validation_passed": True,
        "threshold": threshold,
    },
}