"""Auto-mock factory for instrument drivers.

MockDriverFactory creates working mock HAL drivers that respond to SCPI
queries with sensible defaults. The factory takes a MAL abstraction class
(e.g., DMMAbstraction), generates a mock BaseDriver subclass that handles
the SCPI commands the abstraction emits, and returns an instance of the
abstraction wrapping the mock driver.

Usage:
    from ate_platform.drivers.mock_factory import MockDriverFactory
    from ate_platform.drivers.examples.dmm import DMMAbstraction

    dmm = MockDriverFactory.create_mock(DMMAbstraction)
    dmm.connect("MOCK::DMM")
    voltage = dmm.measure_voltage()  # Returns a sensible float

Configurable mock values:
    dmm = MockDriverFactory.create_mock(
        DMMAbstraction,
        mock_values={"MEAS:VOLT:DC?": "5.000000"},
    )
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.base_mal import BaseAbstraction

if TYPE_CHECKING:
    pass


# Default SCPI response map — keyed by uppercase command pattern.
# The factory merges these with user-provided mock_values.
_DEFAULT_DMM_RESPONSES: dict[str, str] = {
    "MEAS:VOLT:DC?": "5.000000E+00",
    "MEAS:CURR:DC?": "5.000000E-01",
    "MEAS:RES?": "1.000000E+03",
}

_DEFAULT_PSU_RESPONSES: dict[str, str] = {
    "MEAS:VOLT?": "5.000000E+00",
    "MEAS:CURR?": "5.000000E-01",
}


def _generate_dmm_response(command: str) -> str:
    """Generate a realistic DMM SCPI response based on command pattern.

    Args:
        command: SCPI query command string.

    Returns:
        Simulated measurement value in scientific notation.
    """
    command_upper = command.upper().strip()
    if "VOLT" in command_upper:
        base = random.choice([3.3, 5.0, 12.0, 24.0])
        value = base + random.uniform(-0.1, 0.1)
    elif "CURR" in command_upper:
        value = random.uniform(0.1, 2.0)
    elif "RES" in command_upper:
        value = random.uniform(100.0, 10000.0)
    else:
        value = random.uniform(1.0, 10.0)
    return f"{value:.6E}"


def _generate_psu_response(command: str, state: dict[str, float | bool]) -> str:
    """Generate a realistic PSU SCPI response based on command and internal state.

    Args:
        command: SCPI query command string.
        state: Internal PSU state dict with voltage, current_limit, output_on.

    Returns:
        Simulated measurement value in scientific notation.
    """
    command_upper = command.upper().strip()
    if "VOLT" in command_upper:
        base = float(state.get("voltage", 0.0))
        if base > 0 and state.get("output_on", False):
            value = base + random.uniform(-0.02, 0.02)
        else:
            value = 0.0
    elif "CURR" in command_upper:
        if state.get("output_on", False):
            value = random.uniform(0.1, 0.8)
        else:
            value = 0.0
    else:
        value = random.uniform(0.0, 5.0)
    return f"{value:.6E}"


class _MockBaseDriver(BaseDriver):
    """Base mock driver that simulates SCPI communication without real hardware.

    Subclasses override `_generate_response` for instrument-specific behavior.
    Supports configurable mock values via `_mock_values` dict — user-provided
    values take precedence over generated responses.
    """

    def __init__(self, mock_values: dict[str, str] | None = None) -> None:
        """Initialize mock driver.

        Args:
            mock_values: Optional dict mapping SCPI commands (uppercase) to
                         fixed response strings. Takes precedence over generated
                         responses.
        """
        # Bypass BaseDriver.__init__ to avoid creating a real ResourceManager
        self._instrument: object = None
        self._address: str = ""
        self._mock_connected: bool = False
        self._mock_values: dict[str, str] = {
            k.upper(): v for k, v in (mock_values or {}).items()
        }

    def connect(self, address: str) -> None:  # noqa: PLW0221
        """Mock connect — simulates connection without real hardware.

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
    def is_connected(self) -> bool:  # type: ignore[override]
        """Check if mock is connected."""
        return self._mock_connected

    def _check_connected(self) -> None:
        """Ensure mock is connected before operations."""
        if not self._mock_connected:
            msg = "Not connected to any instrument. Call connect() first."
            raise RuntimeError(msg)

    def query(self, command: str, delay: float | None = None) -> str:  # noqa: PLW0221
        """Mock query — returns simulated or configured responses.

        Checks _mock_values first for exact command match (case-insensitive).
        Falls back to _generate_response for dynamic responses.

        Args:
            command: SCPI query command.
            delay: Ignored for mock.

        Returns:
            Simulated response string.
        """
        self._check_connected()
        command_upper = command.upper().strip()
        if command_upper in self._mock_values:
            return self._mock_values[command_upper]
        return self._generate_response(command)

    def write(self, command: str) -> None:  # noqa: PLW0221
        """Mock write — no-op by default. Subclasses may override for state tracking.

        Args:
            command: SCPI command (ignored by default).
        """
        self._check_connected()

    def read(self) -> str:  # noqa: PLW0221
        """Mock read — returns a random measurement value.

        Returns:
            Simulated measurement string.
        """
        self._check_connected()
        return str(round(random.uniform(1.0, 10.0), 6))

    def _generate_response(self, command: str) -> str:
        """Generate a mock response for the given SCPI command.

        Override in subclasses for instrument-specific behavior.

        Args:
            command: SCPI query command string.

        Returns:
            Simulated response string.
        """
        return f"{random.uniform(1.0, 10.0):.6E}"


class _MockDMMDriver(_MockBaseDriver):
    """Mock DMM driver with SCPI-aware response generation.

    Responds to VOLT, CURR, and RES queries with realistic values.
    """

    def _generate_response(self, command: str) -> str:
        return _generate_dmm_response(command)


class _MockPSUDriver(_MockBaseDriver):
    """Mock PSU driver with state-tracking SCPI response generation.

    Tracks voltage, current_limit, and output_on state per channel.
    Write commands update state; query commands reflect it.
    """

    def __init__(self, mock_values: dict[str, str] | None = None) -> None:
        super().__init__(mock_values=mock_values)
        self._channel_states: dict[int, dict[str, float | bool]] = {
            1: {"voltage": 0.0, "current_limit": 1.0, "output_on": False},
            2: {"voltage": 0.0, "current_limit": 1.0, "output_on": False},
            3: {"voltage": 0.0, "current_limit": 1.0, "output_on": False},
        }
        self._selected_channel: int = 1

    def write(self, command: str) -> None:  # noqa: PLW0221
        """Process SCPI write commands and update internal state.

        Args:
            command: SCPI command to process.
        """
        self._check_connected()
        parts = command.upper().strip().split()
        if not parts:
            return

        cmd = parts[0]
        channel = self._selected_channel

        if cmd == "INST:NSEL" and len(parts) >= 2:
            new_channel = int(parts[1])
            if new_channel in self._channel_states:
                self._selected_channel = new_channel
        elif cmd == "VOLT" and len(parts) >= 2:
            voltage = float(parts[1])
            if channel in self._channel_states:
                self._channel_states[channel]["voltage"] = voltage
        elif cmd == "CURR" and len(parts) >= 2:
            current = float(parts[1])
            if channel in self._channel_states:
                self._channel_states[channel]["current_limit"] = current
        elif cmd == "OUTP" and len(parts) >= 2:
            state = parts[1] == "ON"
            if channel in self._channel_states:
                self._channel_states[channel]["output_on"] = state

    def _generate_response(self, command: str) -> str:
        return _generate_psu_response(command, self._channel_states.get(self._selected_channel, self._channel_states[1]))


class MockDriverFactory:
    """Factory for creating mock instrument driver instances.

    Creates a working mock HAL driver and wraps it in the given MAL
    abstraction class. The mock driver responds to SCPI queries with
    sensible defaults, and supports configurable mock values.
    """

    # Map from abstraction class to mock driver class
    _MOCK_DRIVER_MAP: dict[type, type[_MockBaseDriver]] = {}

    @classmethod
    def create_mock(
        cls,
        abstraction_cls: type[BaseAbstraction],
        mock_values: dict[str, str] | None = None,
    ) -> BaseAbstraction:
        """Create a mock instrument instance for the given abstraction class.

        Args:
            abstraction_cls: The MAL abstraction class (e.g., DMMAbstraction).
            mock_values: Optional dict mapping SCPI commands to fixed responses.
                         Keys are matched case-insensitively.

        Returns:
            An instance of abstraction_cls wrapping a mock HAL driver.

        Raises:
            TypeError: If abstraction_cls is not a BaseAbstraction subclass.
            ValueError: If no mock driver is registered for the abstraction class.
        """
        if not (isinstance(abstraction_cls, type) and issubclass(abstraction_cls, BaseAbstraction)):
            msg = f"Expected a BaseAbstraction subclass, got {abstraction_cls!r}"
            raise TypeError(msg)

        mock_driver_cls = cls._MOCK_DRIVER_MAP.get(abstraction_cls)
        if mock_driver_cls is None:
            msg = (
                f"No mock driver registered for {abstraction_cls.__name__}. "
                f"Register with MockDriverFactory.register_mock()."
            )
            raise ValueError(msg)

        mock_driver = mock_driver_cls(mock_values=mock_values)
        return abstraction_cls(driver=mock_driver)

    @classmethod
    def register_mock(
        cls,
        abstraction_cls: type[BaseAbstraction],
        mock_driver_cls: type[_MockBaseDriver],
    ) -> None:
        """Register a mock driver class for an abstraction class.

        Args:
            abstraction_cls: The MAL abstraction class.
            mock_driver_cls: The mock HAL driver class to use for that abstraction.
        """
        cls._MOCK_DRIVER_MAP[abstraction_cls] = mock_driver_cls

    @classmethod
    def clear_registrations(cls) -> None:
        """Clear all mock driver registrations. Useful for testing."""
        cls._MOCK_DRIVER_MAP.clear()
