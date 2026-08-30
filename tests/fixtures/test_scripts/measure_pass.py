"""Measure step - always passes."""

# Simulate measurement
measured_voltage = 3.31
measured_current = 0.5

# Set a result
result = {
    "status": "PASSED",
    "outputs": {
        "voltage": measured_voltage,
        "current": measured_current,
        "channel": params.get("channel", 1),  # noqa: F821
    },
}
