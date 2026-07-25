"""Digital Multimeter (DMM) driver implementation — HAL + MAL layers.

DMMHALDriver: SCPI/VISA communication layer only.
DMMAbstraction: Semantic measurement methods that delegate to DMMHALDriver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ate_platform.drivers import DriverRegistry
from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.base_mal import BaseAbstraction
from ate_platform.drivers.capabilities import DMMCapabilities

if TYPE_CHECKING:
    import pyvisa


DMM_DRIVER_NAME = "dmm"


# ---------------------------------------------------------------------------
# HAL Layer — SCPI commands only
# ---------------------------------------------------------------------------


class DMMHALDriver(BaseDriver):
    """DMM HAL driver — pure SCPI/VISA communication.

    Routes SCPI commands to the instrument. No semantic methods.
    """


# ---------------------------------------------------------------------------
# MAL Layer — Semantic measurement methods
# ---------------------------------------------------------------------------


class DMMAbstraction(BaseAbstraction):
    """DMM MAL abstraction — semantic measurement methods.

    Translates high-level measurement requests into SCPI commands
    via the injected DMMHALDriver.
    """

    capabilities = DMMCapabilities

    def measure_voltage(self, range: str | None = None) -> float:
        """Measure DC voltage.

        Args:
            range: Optional range string (e.g., "10", "100"). If provided,
                   sends CONF:VOLT:DC <range> before measuring.

        Returns:
            Measured voltage in volts.
        """
        if range is not None:
            self._driver.write(f"CONF:VOLT:DC {range}")
        response = self._driver.query("MEAS:VOLT:DC?")
        return float(response.strip())

    def measure_current(self, range: str | None = None) -> float:
        """Measure DC current.

        Args:
            range: Optional range string. If provided, sends CONF:CURR:DC <range>
                   before measuring.

        Returns:
            Measured current in amperes.
        """
        if range is not None:
            self._driver.write(f"CONF:CURR:DC {range}")
        response = self._driver.query("MEAS:CURR:DC?")
        return float(response.strip())

    def measure_resistance(self, range: str | None = None) -> float:
        """Measure resistance.

        Args:
            range: Optional range string. If provided, sends CONF:RES <range>
                   before measuring.

        Returns:
            Measured resistance in ohms.
        """
        if range is not None:
            self._driver.write(f"CONF:RES {range}")
        response = self._driver.query("MEAS:RES?")
        return float(response.strip())


# ---------------------------------------------------------------------------
# Backward-compatible alias — deprecated, use DMMHALDriver + DMMAbstraction
# ---------------------------------------------------------------------------

# DMMDriver kept as backward-compatible alias for DMMHALDriver.
# New code should use DMMHALDriver (HAL) + DMMAbstraction (MAL) separately.
DMMDriver = DMMHALDriver


# Register drivers when module is imported
DriverRegistry.register(DMM_DRIVER_NAME, hal_cls=DMMHALDriver, mal_cls=DMMAbstraction)
