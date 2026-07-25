"""Example instrument drivers for DMM and PSU — HAL + MAL layers."""

from ate_platform.drivers import DriverRegistry
from ate_platform.drivers.examples.dmm import (
    DMM_DRIVER_NAME,
    DMMHALDriver,
    DMMAbstraction,
    DMMDriver,
)
from ate_platform.drivers.examples.psu import (
    PSU_DRIVER_NAME,
    PSUHALDriver,
    PSUAbstraction,
    PSUDriver,
)

# Register drivers
DriverRegistry.register(DMM_DRIVER_NAME, hal_cls=DMMHALDriver, mal_cls=DMMAbstraction)
DriverRegistry.register(PSU_DRIVER_NAME, hal_cls=PSUHALDriver, mal_cls=PSUAbstraction)

__all__ = [
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