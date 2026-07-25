"""Programmable Power Supply Unit (PSU) driver implementation — HAL + MAL layers.

PSUHALDriver: SCPI/VISA communication layer only.
PSUAbstraction: Semantic control methods that delegate to PSUHALDriver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ate_platform.drivers import DriverRegistry
from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.base_mal import BaseAbstraction
from ate_platform.drivers.capabilities import PSUCapabilities

if TYPE_CHECKING:
    import pyvisa


PSU_DRIVER_NAME = "psu"


# ---------------------------------------------------------------------------
# HAL Layer — SCPI commands only
# ---------------------------------------------------------------------------


class PSUHALDriver(BaseDriver):
    """PSU HAL driver — pure SCPI/VISA communication.

    Routes SCPI commands to the instrument. No semantic methods.
    """


# ---------------------------------------------------------------------------
# MAL Layer — Semantic control methods
# ---------------------------------------------------------------------------


class PSUAbstraction(BaseAbstraction):
    """PSU MAL abstraction — semantic control methods.

    Translates high-level control requests into SCPI commands
    via the injected PSUHALDriver.
    """

    capabilities = PSUCapabilities

    def set_voltage(self, voltage: float) -> None:
        """Set output voltage.

        Args:
            voltage: Target voltage in volts.
        """
        self._driver.write(f"VOLT {voltage}")

    def set_current(self, current: float) -> None:
        """Set current limit.

        Args:
            current: Current limit in amperes.
        """
        self._driver.write(f"CURR {current}")

    def enable_output(self, enable: bool = True) -> None:
        """Enable or disable output.

        Args:
            enable: True to enable, False to disable.
        """
        self._driver.write(f"OUTP {'ON' if enable else 'OFF'}")

    def measure_output(self) -> tuple[float, float]:
        """Measure output voltage and current.

        Returns:
            Tuple of (voltage, current).
        """
        voltage = float(self._driver.query("MEAS:VOLT?").strip())
        current = float(self._driver.query("MEAS:CURR?").strip())
        return voltage, current


# ---------------------------------------------------------------------------
# Backward-compatible alias — deprecated, use PSUHALDriver + PSUAbstraction
# ---------------------------------------------------------------------------

# PSUDriver kept as backward-compatible alias for PSUHALDriver.
# New code should use PSUHALDriver (HAL) + PSUAbstraction (MAL) separately.
PSUDriver = PSUHALDriver


# Register drivers when module is imported
DriverRegistry.register(PSU_DRIVER_NAME, hal_cls=PSUHALDriver, mal_cls=PSUAbstraction)
