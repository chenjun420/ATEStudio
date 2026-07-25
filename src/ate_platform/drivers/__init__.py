"""PyVISA driver module for instrument control — HAL/MAL architecture."""

from ate_platform.drivers.base import DriverRegistry, InstrumentDriver
from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.base_mal import BaseAbstraction
from ate_platform.drivers.capabilities import DMMCapabilities, PSUCapabilities
from ate_platform.drivers.examples import (
    DMM_DRIVER_NAME,
    DMMHALDriver,
    DMMAbstraction,
    PSU_DRIVER_NAME,
    PSUHALDriver,
    PSUAbstraction,
)
from ate_platform.drivers.mock_factory import (
    MockDriverFactory,
    _MockDMMDriver,
    _MockPSUDriver,
)

# Backward-compatible aliases — deprecated: use DMMHALDriver + DMMAbstraction
DMMDriver = DMMHALDriver
PSUDriver = PSUHALDriver

# Register example drivers with the registry
DriverRegistry.register(DMM_DRIVER_NAME, hal_cls=DMMHALDriver, mal_cls=DMMAbstraction)
DriverRegistry.register(PSU_DRIVER_NAME, hal_cls=PSUHALDriver, mal_cls=PSUAbstraction)

# Register built-in mock drivers
MockDriverFactory.register_mock(DMMAbstraction, _MockDMMDriver)
MockDriverFactory.register_mock(PSUAbstraction, _MockPSUDriver)

__all__ = [
    # HAL/MAL base classes
    "BaseDriver",
    "BaseAbstraction",
    "InstrumentDriver",
    "DriverRegistry",
    # Capabilities
    "DMMCapabilities",
    "PSUCapabilities",
    # Mock factory
    "MockDriverFactory",
    # DMM
    "DMMHALDriver",
    "DMMAbstraction",
    "DMMDriver",
    "DMM_DRIVER_NAME",
    # PSU
    "PSUHALDriver",
    "PSUAbstraction",
    "PSUDriver",
    "PSU_DRIVER_NAME",
]