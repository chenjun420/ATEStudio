"""Init step - always passes."""

# Simulate initialization
init_complete = True

# Set a result
result = {
    "status": "PASSED",
    "outputs": {"initialized": True, "voltage_set": params.get("voltage", 3.3)},  # noqa: F821
}
