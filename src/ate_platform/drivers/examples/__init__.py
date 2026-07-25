"""Example instrument drivers for DMM and PSU — HAL + MAL layers."""

from ate_platform.drivers import DriverRegistry
from ate_platform.drivers.examples.dmm import (
    DMM_DRIVER_NAME,
    DMMHALDriver,
    DMMAbstraction,
    DMMDriver,
    MockDMMDriver,
)
from ate_platform.drivers.examples.psu import (
    MOCK_PSU_DRIVER_NAME,
    PSU_DRIVER_NAME,
    PSUHALDriver,
    PSUAbstraction,
    PSUDriver,
    MockPSUDriver,
)

# Register drivers
DriverRegistry.register(DMM_DRIVER_NAME, hal_cls=DMMHALDriver, mal_cls=DMMAbstraction)
DriverRegistry.register(PSU_DRIVER_NAME, hal_cls=PSUHALDriver, mal_cls=PSUAbstraction)
DriverRegistry.register_driver(MOCK_PSU_DRIVER_NAME, MockPSUDriver)
DriverRegistry.register_driver(f"mock_{DMM_DRIVER_NAME}", MockDMMDriver)

__all__ = [
    # DMM
    "DMMHALDriver",
    "DMMAbstraction",
    "DMMDriver",
    "MockDMMDriver",
    "DMM_DRIVER_NAME",
    # PSU
    "PSUHALDriver",
    "PSUAbstraction",
    "PSUDriver",
    "MockPSUDriver",
    "PSU_DRIVER_NAME",
    "MOCK_PSU_DRIVER_NAME",
]
