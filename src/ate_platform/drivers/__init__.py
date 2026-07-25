"""PyVISA driver module for instrument control — HAL/MAL architecture."""

from ate_platform.drivers.base import DriverRegistry, InstrumentDriver
from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.base_mal import BaseAbstraction
from ate_platform.drivers.examples import (
    DMM_DRIVER_NAME,
    DMMHALDriver,
    DMMAbstraction,
    MOCK_PSU_DRIVER_NAME,
    PSU_DRIVER_NAME,
    PSUHALDriver,
    PSUAbstraction,
    MockDMMDriver,
    MockPSUDriver,
)

# Backward-compatible aliases — deprecated: use DMMHALDriver + DMMAbstraction
DMMDriver = DMMHALDriver
PSUDriver = PSUHALDriver

# Register example drivers with the registry
DriverRegistry.register(DMM_DRIVER_NAME, hal_cls=DMMHALDriver, mal_cls=DMMAbstraction)
DriverRegistry.register(PSU_DRIVER_NAME, hal_cls=PSUHALDriver, mal_cls=PSUAbstraction)
DriverRegistry.register_driver(f"mock_{DMM_DRIVER_NAME}", MockDMMDriver)
DriverRegistry.register_driver(MOCK_PSU_DRIVER_NAME, MockPSUDriver)

__all__ = [
    # HAL/MAL base classes
    "BaseDriver",
    "BaseAbstraction",
    "InstrumentDriver",
    "DriverRegistry",
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
