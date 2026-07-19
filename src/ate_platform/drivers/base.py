"""Base classes for PyVISA instrument drivers."""

from __future__ import annotations

import threading
from abc import ABC
from typing import TYPE_CHECKING

import pyvisa

if TYPE_CHECKING:
    from pyvisa.resources import Resource


class InstrumentDriver(ABC):
    """Abstract base class for instrument drivers using PyVISA.

    Provides thread-safe communication with instruments via PyVISA.
    Subclasses should implement instrument-specific commands.
    """

    def __init__(self, resource_manager: pyvisa.ResourceManager | None = None) -> None:
        """Initialize the instrument driver.

        Args:
            resource_manager: Optional PyVISA ResourceManager. If None, creates one with '@py' backend.
        """
        self._resource_manager: pyvisa.ResourceManager = resource_manager or pyvisa.ResourceManager("@py")
        self._instrument: Resource | None = None
        self._address: str = ""
        self._lock: threading.Lock = threading.Lock()

    @property
    def is_connected(self) -> bool:
        """Check if instrument is connected."""
        return self._instrument is not None

    @property
    def address(self) -> str:
        """Get the instrument address."""
        return self._address

    def connect(self, address: str) -> None:
        """Connect to the instrument at the specified address.

        Args:
            address: VISA resource address (e.g., 'TCPIP0::192.168.1.1::INSTR').

        Raises:
            pyvisa.VisaIOError: If connection fails.
            ValueError: If already connected to a different address.
        """
        with self._lock:
            if self._instrument is not None:
                if self._address == address:
                    return  # Already connected to same address
                msg = f"Already connected to {self._address}. Disconnect first."
                raise ValueError(msg)

            self._instrument = self._resource_manager.open_resource(address)
            self._address = address

    def disconnect(self) -> None:
        """Disconnect from the instrument."""
        with self._lock:
            if self._instrument is not None:
                self._instrument.close()
                self._instrument = None
            self._address = ""

    def write(self, command: str) -> None:
        """Send a command to the instrument.

        Args:
            command: SCPI command to send.

        Raises:
            RuntimeError: If not connected to an instrument.
        """
        with self._lock:
            if self._instrument is None:
                msg = "Not connected to any instrument. Call connect() first."
                raise RuntimeError(msg)
            self._instrument.write(command)

    def query(self, command: str, delay: float = 0.1) -> str:
        """Send a query command and read the response.

        Args:
            command: SCPI query command to send.
            delay: Delay in seconds between write and read.

        Returns:
            Response string from the instrument.

        Raises:
            RuntimeError: If not connected to an instrument.
        """
        with self._lock:
            if self._instrument is None:
                msg = "Not connected to any instrument. Call connect() first."
                raise RuntimeError(msg)
            return self._instrument.query(command, delay=delay)

    def read(self) -> str:
        """Read a response from the instrument.

        Returns:
            Response string from the instrument.

        Raises:
            RuntimeError: If not connected to an instrument.
        """
        with self._lock:
            if self._instrument is None:
                msg = "Not connected to any instrument. Call connect() first."
                raise RuntimeError(msg)
            return self._instrument.read()

    def __enter__(self) -> InstrumentDriver:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Context manager exit - ensures disconnect."""
        self.disconnect()


class DriverRegistry:
    """Registry for instrument driver classes.

    Allows registration and retrieval of driver classes by name.
    """

    _drivers: dict[str, type[InstrumentDriver]] = {}

    @classmethod
    def register_driver(cls, name: str, driver_class: type[InstrumentDriver]) -> None:
        """Register a driver class.

        Args:
            name: Name to register the driver under.
            driver_class: Driver class to register.

        Raises:
            TypeError: If driver_class is not a subclass of InstrumentDriver.
        """
        if not (isinstance(driver_class, type) and issubclass(driver_class, InstrumentDriver)):
            msg = f"Driver class must be a subclass of InstrumentDriver, got {driver_class!r}"
            raise TypeError(msg)
        cls._drivers[name] = driver_class

    @classmethod
    def get_driver(cls, name: str) -> type[InstrumentDriver]:
        """Get a registered driver class.

        Args:
            name: Name of the driver to retrieve.

        Returns:
            The registered driver class.

        Raises:
            KeyError: If no driver is registered under the given name.
        """
        if name not in cls._drivers:
            msg = f"No driver registered with name '{name}'. Available: {list(cls._drivers.keys())}"
            raise KeyError(msg)
        return cls._drivers[name]

    @classmethod
    def list_drivers(cls) -> list[str]:
        """List all registered driver names.

        Returns:
            List of registered driver names.
        """
        return list(cls._drivers.keys())

    @classmethod
    def clear(cls) -> None:
        """Clear all registered drivers. Useful for testing."""
        cls._drivers.clear()
