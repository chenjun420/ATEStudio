"""Example instrument drivers (DMM / PSU / Chroma 电子负载) — HAL + MAL layers."""

from ate_platform.drivers import DriverRegistry
from ate_platform.drivers.examples.chroma_eload import (
    ELOAD_DRIVER_NAME,
    ChromaEloadHALDriver,
    ChromaEloadAbstraction,
)
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
DriverRegistry.register(ELOAD_DRIVER_NAME, hal_cls=ChromaEloadHALDriver, mal_cls=ChromaEloadAbstraction)

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
    # Chroma 电子负载
    "ChromaEloadHALDriver",
    "ChromaEloadAbstraction",
    "ELOAD_DRIVER_NAME",
]