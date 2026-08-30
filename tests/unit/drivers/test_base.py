"""Unit tests for PyVISA driver base classes (HAL + MAL)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from ate_platform.drivers import DriverRegistry, InstrumentDriver
from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.base_mal import BaseAbstraction

if TYPE_CHECKING:
    pass


class MockHALDriver(BaseDriver):
    """Mock HAL driver for testing purposes."""

    def get_idn(self) -> str:
        """Get instrument identification."""
        return self.query("*IDN?")


class MockMALAbstraction(BaseAbstraction):
    """Mock MAL abstraction for testing purposes."""

    def get_idn(self) -> str:
        """Get instrument identification via HAL."""
        return self._driver.query("*IDN?")


# Backward compat: InstrumentDriver is now BaseDriver
MockInstrumentDriver = MockHALDriver


class TestBaseDriver:
    """Tests for BaseDriver (HAL) base class."""

    def test_init_with_default_resource_manager(self) -> None:
        """Test initialization with default ResourceManager."""
        with patch("pyvisa.ResourceManager") as mock_rm_class:
            mock_rm = MagicMock()
            mock_rm_class.return_value = mock_rm

            driver = MockHALDriver()

            mock_rm_class.assert_called_once_with("@py")
            assert driver._resource_manager == mock_rm
            assert driver._instrument is None
            assert driver.address == ""
            assert not driver.is_connected

    def test_init_with_custom_resource_manager(self) -> None:
        """Test initialization with custom ResourceManager."""
        mock_rm = MagicMock()
        driver = MockHALDriver(resource_manager=mock_rm)

        assert driver._resource_manager == mock_rm
        assert driver._instrument is None

    def test_connect_success(self) -> None:
        """Test successful connection."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockHALDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")

        mock_rm.open_resource.assert_called_once_with("TCPIP0::192.168.1.1::INSTR")
        assert driver._instrument == mock_instrument
        assert driver.address == "TCPIP0::192.168.1.1::INSTR"
        assert driver.is_connected

    def test_connect_same_address_no_error(self) -> None:
        """Test connecting to same address twice doesn't raise."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockHALDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")
        driver.connect("TCPIP0::192.168.1.1::INSTR")  # Should not raise

        mock_rm.open_resource.assert_called_once()

    def test_connect_different_address_raises(self) -> None:
        """Test connecting to different address raises ValueError."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockHALDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")

        with pytest.raises(ValueError, match="Already connected"):
            driver.connect("TCPIP0::192.168.1.2::INSTR")

    def test_disconnect(self) -> None:
        """Test disconnecting from instrument."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockHALDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")
        driver.disconnect()

        mock_instrument.close.assert_called_once()
        assert driver._instrument is None
        assert driver.address == ""
        assert not driver.is_connected

    def test_disconnect_not_connected(self) -> None:
        """Test disconnecting when not connected doesn't raise."""
        mock_rm = MagicMock()
        driver = MockHALDriver(resource_manager=mock_rm)
        driver.disconnect()  # Should not raise

    def test_write_success(self) -> None:
        """Test writing command to instrument."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockHALDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")
        driver.write("*RST")

        mock_instrument.write.assert_called_once_with("*RST")

    def test_write_not_connected_raises(self) -> None:
        """Test writing when not connected raises RuntimeError."""
        mock_rm = MagicMock()
        driver = MockHALDriver(resource_manager=mock_rm)

        with pytest.raises(RuntimeError, match="Not connected"):
            driver.write("*RST")

    def test_query_success(self) -> None:
        """Test querying instrument."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_instrument.query.return_value = "Keithley,2450,12345,1.0"
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockHALDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")
        result = driver.query("*IDN?")

        mock_instrument.query.assert_called_once_with("*IDN?")
        assert result == "Keithley,2450,12345,1.0"

    def test_query_with_custom_delay(self) -> None:
        """Test querying with custom delay."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_instrument.query.return_value = "OK"
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockHALDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")
        driver.query("*IDN?", delay=0.5)

        mock_instrument.query.assert_called_once_with("*IDN?", delay=0.5)

    def test_query_not_connected_raises(self) -> None:
        """Test querying when not connected raises RuntimeError."""
        mock_rm = MagicMock()
        driver = MockHALDriver(resource_manager=mock_rm)

        with pytest.raises(RuntimeError, match="Not connected"):
            driver.query("*IDN?")

    def test_read_success(self) -> None:
        """Test reading from instrument."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_instrument.read.return_value = "OK"
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockHALDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")
        result = driver.read()

        mock_instrument.read.assert_called_once()
        assert result == "OK"

    def test_read_not_connected_raises(self) -> None:
        """Test reading when not connected raises RuntimeError."""
        mock_rm = MagicMock()
        driver = MockHALDriver(resource_manager=mock_rm)

        with pytest.raises(RuntimeError, match="Not connected"):
            driver.read()

    def test_reset(self) -> None:
        """Test reset sends *RST command."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockHALDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")
        driver.reset()

        mock_instrument.write.assert_called_once_with("*RST")

    def test_context_manager(self) -> None:
        """Test using driver as context manager."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_rm.open_resource.return_value = mock_instrument

        with MockHALDriver(resource_manager=mock_rm) as driver:
            driver.connect("TCPIP0::192.168.1.1::INSTR")
            assert driver.is_connected

        mock_instrument.close.assert_called_once()
        assert not driver.is_connected


class TestBaseAbstraction:
    """Tests for BaseAbstraction (MAL) base class."""

    def test_takes_driver_via_constructor(self) -> None:
        """Test that abstraction stores the HAL driver."""
        mock_driver = MagicMock(spec=BaseDriver)
        abstraction = MockMALAbstraction(driver=mock_driver)
        assert abstraction._driver is mock_driver

    def test_driver_property(self) -> None:
        """Test driver property exposes HAL driver."""
        mock_driver = MagicMock(spec=BaseDriver)
        abstraction = MockMALAbstraction(driver=mock_driver)
        assert abstraction.driver is mock_driver

    def test_connect_delegates_to_driver(self) -> None:
        """Test connect delegates to HAL driver."""
        mock_driver = MagicMock(spec=BaseDriver)
        abstraction = MockMALAbstraction(driver=mock_driver)
        abstraction.connect("TCPIP0::192.168.1.1::INSTR")
        mock_driver.connect.assert_called_once_with("TCPIP0::192.168.1.1::INSTR")

    def test_disconnect_delegates_to_driver(self) -> None:
        """Test disconnect delegates to HAL driver."""
        mock_driver = MagicMock(spec=BaseDriver)
        abstraction = MockMALAbstraction(driver=mock_driver)
        abstraction.disconnect()
        mock_driver.disconnect.assert_called_once()

    def test_is_connected_delegates_to_driver(self) -> None:
        """Test is_connected delegates to HAL driver."""
        mock_driver = MagicMock(spec=BaseDriver)
        mock_driver.is_connected = True
        abstraction = MockMALAbstraction(driver=mock_driver)
        assert abstraction.is_connected is True

    def test_address_delegates_to_driver(self) -> None:
        """Test address delegates to HAL driver."""
        mock_driver = MagicMock(spec=BaseDriver)
        mock_driver.address = "TCPIP0::1.2.3.4::INSTR"
        abstraction = MockMALAbstraction(driver=mock_driver)
        assert abstraction.address == "TCPIP0::1.2.3.4::INSTR"

    def test_get_capabilities_returns_none_by_default(self) -> None:
        """Test get_capabilities returns None when no capabilities model defined."""
        mock_driver = MagicMock(spec=BaseDriver)
        abstraction = MockMALAbstraction(driver=mock_driver)
        assert abstraction.get_capabilities() is None

    def test_context_manager(self) -> None:
        """Test using abstraction as context manager."""
        mock_driver = MagicMock(spec=BaseDriver)
        with MockMALAbstraction(driver=mock_driver) as abstraction:
            abstraction.connect("TCPIP0::192.168.1.1::INSTR")
        mock_driver.disconnect.assert_called_once()


class TestInstrumentDriverAlias:
    """Test that InstrumentDriver is a backward-compatible alias for BaseDriver."""

    def test_alias(self) -> None:
        """InstrumentDriver should be the same class as BaseDriver."""
        assert InstrumentDriver is BaseDriver


class TestDriverRegistry:
    """Tests for DriverRegistry class."""

    def setup_method(self) -> None:
        """Clear registry before each test."""
        DriverRegistry.clear()

    def test_register_hal_mal(self) -> None:
        """Test registering a HAL/MAL pair."""
        DriverRegistry.register("mock", hal_cls=MockHALDriver, mal_cls=MockMALAbstraction)
        assert "mock" in DriverRegistry.list_drivers()

    def test_register_hal_mal_invalid_hal_raises(self) -> None:
        """Test registering with invalid HAL class raises TypeError."""

        class NotADriver:
            pass

        with pytest.raises(TypeError, match="must be a subclass of BaseDriver"):
            DriverRegistry.register("invalid", hal_cls=NotADriver, mal_cls=MockMALAbstraction)  # type: ignore[arg-type]

    def test_register_hal_mal_invalid_mal_raises(self) -> None:
        """Test registering with invalid MAL class raises TypeError."""

        class NotAnAbstraction:
            pass

        with pytest.raises(TypeError, match="must be a subclass of BaseAbstraction"):
            DriverRegistry.register("invalid", hal_cls=MockHALDriver, mal_cls=NotAnAbstraction)  # type: ignore[arg-type]

    def test_register_driver_legacy(self) -> None:
        """Test legacy register_driver still works."""
        DriverRegistry.register_driver("mock", MockHALDriver)
        assert "mock" in DriverRegistry.list_drivers()

    def test_register_non_driver_raises(self) -> None:
        """Test registering non-BaseDriver class raises TypeError."""

        class NotADriver:
            pass

        with pytest.raises(TypeError, match="must be a subclass of BaseDriver or BaseAbstraction"):
            DriverRegistry.register_driver("invalid", NotADriver)  # type: ignore[arg-type]

    def test_get_driver_mal_default(self) -> None:
        """Test get_driver returns MAL by default."""
        DriverRegistry.register("mock", hal_cls=MockHALDriver, mal_cls=MockMALAbstraction)
        driver_class = DriverRegistry.get_driver("mock")
        assert driver_class == MockMALAbstraction

    def test_get_driver_hal_layer(self) -> None:
        """Test get_driver returns HAL when layer='hal'."""
        DriverRegistry.register("mock", hal_cls=MockHALDriver, mal_cls=MockMALAbstraction)
        driver_class = DriverRegistry.get_driver("mock", layer="hal")
        assert driver_class == MockHALDriver

    def test_get_driver_legacy_fallback(self) -> None:
        """Test get_driver falls back to legacy registry."""
        DriverRegistry.register_driver("mock", MockHALDriver)
        driver_class = DriverRegistry.get_driver("mock")
        assert driver_class == MockHALDriver

    def test_get_driver_not_found_raises(self) -> None:
        """Test getting unregistered driver raises KeyError."""
        with pytest.raises(KeyError, match="No driver registered"):
            DriverRegistry.get_driver("nonexistent")

    def test_list_drivers(self) -> None:
        """Test listing registered drivers."""
        DriverRegistry.register("driver1", hal_cls=MockHALDriver, mal_cls=MockMALAbstraction)
        DriverRegistry.register_driver("driver2", MockHALDriver)

        drivers = DriverRegistry.list_drivers()
        assert set(drivers) == {"driver1", "driver2"}

    def test_clear(self) -> None:
        """Test clearing registry."""
        DriverRegistry.register("mock", hal_cls=MockHALDriver, mal_cls=MockMALAbstraction)
        DriverRegistry.clear()
        assert DriverRegistry.list_drivers() == []
