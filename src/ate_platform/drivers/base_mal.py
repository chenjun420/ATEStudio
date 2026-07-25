"""MAL layer — Measurement Abstraction Layer for semantic instrument control.

BaseAbstraction provides semantic methods that delegate to a BaseDriver (HAL).
Each concrete abstraction translates high-level operations into SCPI commands
via the injected driver.
"""

from __future__ import annotations

from abc import ABC
from typing import ClassVar

from pydantic import BaseModel

from ate_platform.drivers.base_hal import BaseDriver


class BaseAbstraction(ABC):
    """Abstract base class for instrument MAL abstractions.

    Wraps a BaseDriver (HAL) and provides semantic measurement/control methods.
    Subclasses MUST override abstract methods with instrument-specific logic.

    Attributes:
        capabilities: Optional Pydantic model class describing instrument capabilities.
    """

    capabilities: ClassVar[type[BaseModel] | None] = None

    def __init__(self, driver: BaseDriver) -> None:
        """Initialize the abstraction with a HAL driver.

        Args:
            driver: The HAL driver instance to delegate SCPI communication to.
        """
        self._driver = driver

    @property
    def driver(self) -> BaseDriver:
        """Access the underlying HAL driver."""
        return self._driver

    def get_capabilities(self) -> BaseModel | None:
        """Get instrument capabilities model instance.

        Returns:
            An instance of the capabilities model if defined, else None.
        """
        if self.capabilities is None:
            return None
        return self.capabilities()

    def connect(self, address: str) -> None:
        """Connect the underlying driver to an instrument.

        Args:
            address: VISA resource address.
        """
        self._driver.connect(address)

    def disconnect(self) -> None:
        """Disconnect the underlying driver."""
        self._driver.disconnect()

    @property
    def is_connected(self) -> bool:
        """Check if the underlying driver is connected."""
        return self._driver.is_connected

    @property
    def address(self) -> str:
        """Get the instrument address from the underlying driver."""
        return self._driver.address

    def __enter__(self) -> BaseAbstraction:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Context manager exit - ensures disconnect."""
        self.disconnect()
