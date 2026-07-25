"""Base classes for PyVISA instrument drivers.

Provides DriverRegistry for HAL/MAL driver registration and lookup.
BaseDriver (HAL) and BaseAbstraction (MAL) live in base_hal.py and base_mal.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.base_mal import BaseAbstraction

if TYPE_CHECKING:
    pass


# Backward-compatible alias — deprecated: use BaseDriver from base_hal.py
InstrumentDriver = BaseDriver


class DriverRegistry:
    """Registry for instrument driver classes (HAL + MAL).

    Supports both legacy single-class registration (register_driver)
    and new HAL/MAL paired registration (register).
    """

    _drivers: dict[str, type[BaseDriver]] = {}
    _hal_mal: dict[str, tuple[type[BaseDriver], type[BaseAbstraction]]] = {}

    @classmethod
    def register(cls, name: str, hal_cls: type[BaseDriver], mal_cls: type[BaseAbstraction]) -> None:
        """Register a HAL/MAL driver pair.

        Args:
            name: Name to register the driver under.
            hal_cls: HAL driver class (must be a BaseDriver subclass).
            mal_cls: MAL abstraction class (must be a BaseAbstraction subclass).

        Raises:
            TypeError: If hal_cls or mal_cls are not valid subclasses.
        """
        if not (isinstance(hal_cls, type) and issubclass(hal_cls, BaseDriver)):
            msg = f"HAL class must be a subclass of BaseDriver, got {hal_cls!r}"
            raise TypeError(msg)
        if not (isinstance(mal_cls, type) and issubclass(mal_cls, BaseAbstraction)):
            msg = f"MAL class must be a subclass of BaseAbstraction, got {mal_cls!r}"
            raise TypeError(msg)
        cls._hal_mal[name] = (hal_cls, mal_cls)
        # Also register HAL in legacy dict for backward compat
        cls._drivers[name] = hal_cls

    @classmethod
    def register_driver(cls, name: str, driver_class: type[BaseDriver]) -> None:
        """Register a driver class (legacy API).

        Args:
            name: Name to register the driver under.
            driver_class: Driver class to register (must be a BaseDriver subclass).

        Raises:
            TypeError: If driver_class is not a subclass of BaseDriver or BaseAbstraction.
        """
        if isinstance(driver_class, type) and issubclass(driver_class, BaseDriver):
            cls._drivers[name] = driver_class
        elif isinstance(driver_class, type) and issubclass(driver_class, BaseAbstraction):
            # Allow registering MAL classes too for backward compat
            cls._drivers[name] = driver_class  # type: ignore[assignment]
        else:
            msg = f"Driver class must be a subclass of BaseDriver or BaseAbstraction, got {driver_class!r}"
            raise TypeError(msg)

    @classmethod
    def get_driver(cls, name: str, layer: str = "mal") -> type[BaseDriver] | type[BaseAbstraction]:
        """Get a registered driver class.

        Args:
            name: Name of the driver to retrieve.
            layer: "mal" to get the MAL abstraction (default), "hal" to get the HAL driver.

        Returns:
            The registered driver class (MAL by default, HAL if layer="hal").

        Raises:
            KeyError: If no driver is registered under the given name.
        """
        if layer == "hal" and name in cls._hal_mal:
            return cls._hal_mal[name][0]
        if layer == "mal" and name in cls._hal_mal:
            return cls._hal_mal[name][1]
        # Fallback to legacy registry
        if name in cls._drivers:
            return cls._drivers[name]
        msg = f"No driver registered with name '{name}'. Available: {list(cls._drivers.keys())}"
        raise KeyError(msg)

    @classmethod
    def list_drivers(cls) -> list[str]:
        """List all registered driver names.

        Returns:
            List of registered driver names.
        """
        return list({*cls._drivers.keys(), *cls._hal_mal.keys()})

    @classmethod
    def clear(cls) -> None:
        """Clear all registered drivers. Useful for testing."""
        cls._drivers.clear()
        cls._hal_mal.clear()
