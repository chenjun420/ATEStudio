"""Pytest configuration and fixtures."""

import asyncio
from collections.abc import Generator

import pytest


@pytest.fixture
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def sample_test_data() -> dict[str, int]:
    """Provide sample test data for unit tests."""
    return {
        "test_name": "sample_test",
        "test_value": 42,
    }


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Hook to register example drivers before each test.

    This runs after setup_method, ensuring drivers are registered
    even if other tests clear the registry.
    """
    # Only register for test_examples.py::TestDriverRegistry tests
    if "test_examples" in str(item.fspath):
        # Check if this is a TestDriverRegistry test by looking at the nodeid
        if "TestDriverRegistry" in item.nodeid:
            from ate_platform.drivers import DriverRegistry
            from ate_platform.drivers.examples.dmm import (
                DMM_DRIVER_NAME,
                DMMHALDriver,
                DMMAbstraction,
            )
            from ate_platform.drivers.examples.psu import (
                PSU_DRIVER_NAME,
                PSUHALDriver,
                PSUAbstraction,
            )

            DriverRegistry.register(DMM_DRIVER_NAME, hal_cls=DMMHALDriver, mal_cls=DMMAbstraction)
            DriverRegistry.register(PSU_DRIVER_NAME, hal_cls=PSUHALDriver, mal_cls=PSUAbstraction)