"""Programmable Power Supply Unit (PSU) driver implementation — HAL + MAL layers.

PSUHALDriver: SCPI/VISA communication layer only.
PSUAbstraction: Semantic control methods that delegate to PSUHALDriver.
MockPSUDriver: Mock driver for testing without real hardware (kept for Task 5 migration).
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from ate_platform.drivers import DriverRegistry
from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.base_mal import BaseAbstraction

if TYPE_CHECKING:
    import pyvisa


PSU_DRIVER_NAME = "psu"
MOCK_PSU_DRIVER_NAME = "mock_psu"


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


# ---------------------------------------------------------------------------
# Mock driver — kept for Task 5 migration to auto-mock factory
# ---------------------------------------------------------------------------


class MockPSUDriver(BaseDriver):
    """Mock PSU driver for testing without real hardware.

    Simulates realistic PSU behavior with voltage/current tracking.
    """

    def __init__(self, resource_manager: pyvisa.ResourceManager | None = None) -> None:
        """Initialize mock PSU driver.

        Args:
            resource_manager: Ignored for mock driver (for API compatibility).
        """
        # Don't call super().__init__ to avoid creating real ResourceManager
        self._instrument: object = None
        self._address: str = ""
        self._mock_connected: bool = False
        # Track state per channel
        self._channel_states: dict[int, dict[str, float | bool]] = {
            1: {"voltage": 0.0, "current_limit": 1.0, "output_on": False},
            2: {"voltage": 0.0, "current_limit": 1.0, "output_on": False},
            3: {"voltage": 0.0, "current_limit": 1.0, "output_on": False},
        }
        self._selected_channel: int = 1

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

    def write(self, command: str) -> None:  # noqa: PLW0221
        """Mock write - updates internal state based on command.

        Args:
            command: SCPI command to process.
        """
        self._check_connected()
        self._process_write_command(command)

    def query(self, command: str, delay: float | None = None) -> str:  # noqa: PLW0221
        """Mock query - returns simulated responses.

        Args:
            command: SCPI command (parsed to determine response).
            delay: Ignored for mock.

        Returns:
            Simulated response string.
        """
        self._check_connected()
        return self._process_query_command(command)

    def read(self) -> str:  # noqa: PLW0221
        """Mock read - returns a measurement value."""
        self._check_connected()
        return str(random.uniform(0.0, 5.0))

    def _process_write_command(self, command: str) -> None:
        """Process SCPI write commands and update state.

        Args:
            command: SCPI command string.
        """
        parts = command.upper().strip().split()
        if not parts:
            return

        cmd = parts[0]
        channel = self._selected_channel

        if cmd == "INST:NSEL" and len(parts) >= 2:
            # Select channel
            new_channel = int(parts[1])
            if new_channel in self._channel_states:
                self._selected_channel = new_channel
        elif cmd == "VOLT" and len(parts) >= 2:
            # Set voltage
            voltage = float(parts[1])
            if channel in self._channel_states:
                self._channel_states[channel]["voltage"] = voltage
        elif cmd == "CURR" and len(parts) >= 2:
            # Set current limit
            current = float(parts[1])
            if channel in self._channel_states:
                self._channel_states[channel]["current_limit"] = current
        elif cmd == "OUTP":
            # Output control
            if len(parts) >= 2:
                state = parts[1] == "ON"
                if channel in self._channel_states:
                    self._channel_states[channel]["output_on"] = state

    def _process_query_command(self, command: str) -> str:
        """Process SCPI query commands and return simulated values.

        Args:
            command: SCPI query string.

        Returns:
            Simulated measurement value.
        """
        command_upper = command.upper().strip()
        channel = self._selected_channel

        # Extract channel from command if present
        if "(@" in command_upper:
            # Parse channel from (@N) format
            start = command_upper.find("(@") + 2
            end = command_upper.find(")", start)
            if start > 1 and end > start:
                try:
                    channel = int(command_upper[start:end])
                except ValueError:
                    pass

        state = self._channel_states.get(channel, self._channel_states[1])

        if "VOLT" in command_upper:
            # Return set voltage with small variation
            base = float(state.get("voltage", 0.0))
            if base > 0 and state.get("output_on", False):
                value = base + random.uniform(-0.02, 0.02)
            else:
                value = 0.0
        elif "CURR" in command_upper:
            # Return simulated current
            if state.get("output_on", False):
                # Simulate load drawing some current
                value = random.uniform(0.1, 0.8)
            else:
                value = 0.0
        else:
            value = random.uniform(0.0, 5.0)

        return f"{value:.6E}"

    def set_voltage(self, channel: int, voltage: float) -> None:
        """Set mock output voltage.

        Args:
            channel: Output channel number.
            voltage: Target voltage in volts.
        """
        self._check_connected()
        if channel in self._channel_states:
            self._channel_states[channel]["voltage"] = voltage

    def set_current_limit(self, channel: int, current: float) -> None:
        """Set mock current limit.

        Args:
            channel: Output channel number.
            current: Current limit in amperes.
        """
        self._check_connected()
        if channel in self._channel_states:
            self._channel_states[channel]["current_limit"] = current

    def output_on(self, channel: int = 1) -> None:
        """Turn on mock output.

        Args:
            channel: Output channel number (default: 1).
        """
        self._check_connected()
        if channel in self._channel_states:
            self._channel_states[channel]["output_on"] = True

    def output_off(self, channel: int = 1) -> None:
        """Turn off mock output.

        Args:
            channel: Output channel number (default: 1).
        """
        self._check_connected()
        if channel in self._channel_states:
            self._channel_states[channel]["output_on"] = False

    def measure_current(self, channel: int = 1) -> float:
        """Measure mock output current.

        Args:
            channel: Output channel number (default: 1).

        Returns:
            Simulated current reading.
        """
        response = self.query(f"MEAS:CURR? (@{channel})")
        return float(response.strip())

    def measure_voltage(self, channel: int = 1) -> float:
        """Measure mock output voltage.

        Args:
            channel: Output channel number (default: 1).

        Returns:
            Simulated voltage reading.
        """
        response = self.query(f"MEAS:VOLT? (@{channel})")
        return float(response.strip())

    def get_channel_state(self, channel: int) -> dict[str, float | bool]:
        """Get the internal state of a channel (for testing).

        Args:
            channel: Channel number.

        Returns:
            Dictionary with voltage, current_limit, and output_on.
        """
        return self._channel_states.get(channel, {}).copy()


# Register drivers when module is imported
DriverRegistry.register(PSU_DRIVER_NAME, hal_cls=PSUHALDriver, mal_cls=PSUAbstraction)
DriverRegistry.register_driver(MOCK_PSU_DRIVER_NAME, MockPSUDriver)
