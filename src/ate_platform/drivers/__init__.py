"""PyVISA driver module for instrument control."""

from ate_platform.drivers.base import DriverRegistry, InstrumentDriver
from ate_platform.drivers.examples import (
    DMM_DRIVER_NAME,
    MOCK_PSU_DRIVER_NAME,
    PSU_DRIVER_NAME,
    DMMDriver,
    MockDMMDriver,
    MockPSUDriver,
    PSUDriver,
)

# Register example drivers with the registry
DriverRegistry.register_driver(DMM_DRIVER_NAME, DMMDriver)
DriverRegistry.register_driver(f"mock_{DMM_DRIVER_NAME}", MockDMMDriver)
DriverRegistry.register_driver(PSU_DRIVER_NAME, PSUDriver)
DriverRegistry.register_driver(MOCK_PSU_DRIVER_NAME, MockPSUDriver)

__all__ = [
    "InstrumentDriver",
    "DriverRegistry",
    "DMMDriver",
    "MockDMMDriver",
    "DMM_DRIVER_NAME",
    "PSUDriver",
    "MockPSUDriver",
    "PSU_DRIVER_NAME",
    "MOCK_PSU_DRIVER_NAME",
]
