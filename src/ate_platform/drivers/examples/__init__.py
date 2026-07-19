"""Example instrument drivers for DMM and PSU."""

from ate_platform.drivers import DriverRegistry
from ate_platform.drivers.examples.dmm import DMM_DRIVER_NAME, DMMDriver, MockDMMDriver
from ate_platform.drivers.examples.psu import MOCK_PSU_DRIVER_NAME, PSU_DRIVER_NAME, MockPSUDriver, PSUDriver

# Register drivers
DriverRegistry.register_driver(DMM_DRIVER_NAME, DMMDriver)
DriverRegistry.register_driver(PSU_DRIVER_NAME, PSUDriver)
DriverRegistry.register_driver(MOCK_PSU_DRIVER_NAME, MockPSUDriver)
DriverRegistry.register_driver(f"mock_{DMM_DRIVER_NAME}", MockDMMDriver)

__all__ = [
    "DMMDriver",
    "MockDMMDriver",
    "DMM_DRIVER_NAME",
    "PSUDriver",
    "MockPSUDriver",
    "PSU_DRIVER_NAME",
    "MOCK_PSU_DRIVER_NAME",
]
