"""Digital Multimeter (DMM) driver implementation — HAL + MAL layers.

DMMHALDriver: SCPI/VISA communication layer only.
DMMAbstraction: Semantic measurement methods that delegate to DMMHALDriver.
MockDMMDriver: Mock driver for testing without real hardware (kept for Task 5 migration).
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from ate_platform.drivers import DriverRegistry
from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.base_mal import BaseAbstraction

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


# ---------------------------------------------------------------------------
# Mock driver — kept for Task 5 migration to auto-mock factory
# ---------------------------------------------------------------------------


class MockDMMDriver(BaseDriver):
    """Mock DMM driver for testing without real hardware.

    Simulates realistic measurement values with small random variations.
    """

    def __init__(self, resource_manager: pyvisa.ResourceManager | None = None) -> None:
        """Initialize mock DMM driver.

        Args:
            resource_manager: Ignored for mock driver (for API compatibility).
        """
        # Don't call super().__init__ to avoid creating real ResourceManager
        self._instrument: object = None
        self._address: str = ""
        self._mock_connected: bool = False

    def connect(self, address: str) -> None:  # noqa: PLW0221
        """Mock connect - simulates connection without real hardware.

        Args:
            address: Any address is accepted for mock.
        """
        self._address = address
        self._mock_connected = True

    def disconnect(self) -> None:  # noqa: PLW0221
        """Mock disconnect."""
        self._mock_connected = False
        self._address = ""

    @property
    def is_connected(self) -> bool:
        """Check if mock is connected."""
        return self._mock_connected

    def _check_connected(self) -> None:
        """Ensure mock is connected before operations."""
        if not self._mock_connected:
            msg = "Not connected to any instrument. Call connect() first."
            raise RuntimeError(msg)

    def query(self, command: str, delay: float | None = None) -> str:  # noqa: PLW0221
        """Mock query - returns simulated responses.

        Args:
            command: SCPI command (parsed to determine response).
            delay: Ignored for mock.

        Returns:
            Simulated response string.
        """
        self._check_connected()
        return self._mock_response(command)

    def write(self, command: str) -> None:  # noqa: PLW0221
        """Mock write - does nothing for read-only measurements."""
        self._check_connected()

    def read(self) -> str:  # noqa: PLW0221
        """Mock read - returns a random measurement."""
        self._check_connected()
        return str(round(random.uniform(1.0, 10.0), 6))

    def _mock_response(self, command: str) -> str:
        """Generate mock response based on SCPI command.

        Args:
            command: SCPI command string.

        Returns:
            Simulated measurement value.
        """
        command_upper = command.upper().strip()

        # Parse command type
        if "VOLT" in command_upper:
            # Typical voltage: 3.3V or 5V with small variation
            base = random.choice([3.3, 5.0, 12.0, 24.0])
            value = base + random.uniform(-0.1, 0.1)
        elif "CURR" in command_upper:
            # Typical current: 0.1A to 2A
            value = random.uniform(0.1, 2.0)
        elif "RES" in command_upper:
            # Typical resistance: 100 ohms to 10k ohms
            value = random.uniform(100.0, 10000.0)
        else:
            value = random.uniform(1.0, 10.0)

        return f"{value:.6E}"

    def measure_voltage(self, channel: int = 1) -> float:
        """Measure mock DC voltage.

        Args:
            channel: Measurement channel (ignored in mock).

        Returns:
            Simulated voltage reading.
        """
        if channel > 1:
            response = self.query(f"MEAS:VOLT:DC? (@{channel})")
        else:
            response = self.query("MEAS:VOLT:DC?")
        return float(response.strip())

    def measure_current(self, channel: int = 1) -> float:
        """Measure mock DC current.

        Args:
            channel: Measurement channel (ignored in mock).

        Returns:
            Simulated current reading.
        """
        if channel > 1:
            response = self.query(f"MEAS:CURR:DC? (@{channel})")
        else:
            response = self.query("MEAS:CURR:DC?")
        return float(response.strip())

    def measure_resistance(self, channel: int = 1) -> float:
        """Measure mock resistance.

        Args:
            channel: Measurement channel (ignored in mock).

        Returns:
            Simulated resistance reading.
        """
        if channel > 1:
            response = self.query(f"MEAS:RES? (@{channel})")
        else:
            response = self.query("MEAS:RES?")
        return float(response.strip())


# Register drivers when module is imported
DriverRegistry.register(DMM_DRIVER_NAME, hal_cls=DMMHALDriver, mal_cls=DMMAbstraction)
DriverRegistry.register_driver(f"mock_{DMM_DRIVER_NAME}", MockDMMDriver)
