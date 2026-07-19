"""Unit tests for PyVISA driver base classes."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from ate_platform.drivers import DriverRegistry, InstrumentDriver

if TYPE_CHECKING:
    from pyvisa.resources import Resource


class MockInstrumentDriver(InstrumentDriver):
    """Mock driver for testing purposes."""

    def get_idn(self) -> str:
        """Get instrument identification."""
        return self.query("*IDN?")


class TestInstrumentDriver:
    """Tests for InstrumentDriver base class."""

    def test_init_with_default_resource_manager(self) -> None:
        """Test initialization with default ResourceManager."""
        with patch("pyvisa.ResourceManager") as mock_rm_class:
            mock_rm = MagicMock()
            mock_rm_class.return_value = mock_rm

            driver = MockInstrumentDriver()

            mock_rm_class.assert_called_once_with("@py")
            assert driver._resource_manager == mock_rm
            assert driver._instrument is None
            assert driver.address == ""
            assert not driver.is_connected

    def test_init_with_custom_resource_manager(self) -> None:
        """Test initialization with custom ResourceManager."""
        mock_rm = MagicMock()
        driver = MockInstrumentDriver(resource_manager=mock_rm)

        assert driver._resource_manager == mock_rm
        assert driver._instrument is None

    def test_connect_success(self) -> None:
        """Test successful connection."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockInstrumentDriver(resource_manager=mock_rm)
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

        driver = MockInstrumentDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")
        driver.connect("TCPIP0::192.168.1.1::INSTR")  # Should not raise

        mock_rm.open_resource.assert_called_once()

    def test_connect_different_address_raises(self) -> None:
        """Test connecting to different address raises ValueError."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockInstrumentDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")

        with pytest.raises(ValueError, match="Already connected"):
            driver.connect("TCPIP0::192.168.1.2::INSTR")

    def test_disconnect(self) -> None:
        """Test disconnecting from instrument."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockInstrumentDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")
        driver.disconnect()

        mock_instrument.close.assert_called_once()
        assert driver._instrument is None
        assert driver.address == ""
        assert not driver.is_connected

    def test_disconnect_not_connected(self) -> None:
        """Test disconnecting when not connected doesn't raise."""
        mock_rm = MagicMock()
        driver = MockInstrumentDriver(resource_manager=mock_rm)
        driver.disconnect()  # Should not raise

    def test_write_success(self) -> None:
        """Test writing command to instrument."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockInstrumentDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")
        driver.write("*RST")

        mock_instrument.write.assert_called_once_with("*RST")

    def test_write_not_connected_raises(self) -> None:
        """Test writing when not connected raises RuntimeError."""
        mock_rm = MagicMock()
        driver = MockInstrumentDriver(resource_manager=mock_rm)

        with pytest.raises(RuntimeError, match="Not connected"):
            driver.write("*RST")

    def test_query_success(self) -> None:
        """Test querying instrument."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_instrument.query.return_value = "Keithley,2450,12345,1.0"
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockInstrumentDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")
        result = driver.query("*IDN?")

        mock_instrument.query.assert_called_once_with("*IDN?", delay=0.1)
        assert result == "Keithley,2450,12345,1.0"

    def test_query_with_custom_delay(self) -> None:
        """Test querying with custom delay."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_instrument.query.return_value = "OK"
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockInstrumentDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")
        driver.query("*IDN?", delay=0.5)

        mock_instrument.query.assert_called_once_with("*IDN?", delay=0.5)

    def test_query_not_connected_raises(self) -> None:
        """Test querying when not connected raises RuntimeError."""
        mock_rm = MagicMock()
        driver = MockInstrumentDriver(resource_manager=mock_rm)

        with pytest.raises(RuntimeError, match="Not connected"):
            driver.query("*IDN?")

    def test_read_success(self) -> None:
        """Test reading from instrument."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_instrument.read.return_value = "OK"
        mock_rm.open_resource.return_value = mock_instrument

        driver = MockInstrumentDriver(resource_manager=mock_rm)
        driver.connect("TCPIP0::192.168.1.1::INSTR")
        result = driver.read()

        mock_instrument.read.assert_called_once()
        assert result == "OK"

    def test_read_not_connected_raises(self) -> None:
        """Test reading when not connected raises RuntimeError."""
        mock_rm = MagicMock()
        driver = MockInstrumentDriver(resource_manager=mock_rm)

        with pytest.raises(RuntimeError, match="Not connected"):
            driver.read()

    def test_context_manager(self) -> None:
        """Test using driver as context manager."""
        mock_rm = MagicMock()
        mock_instrument = MagicMock()
        mock_rm.open_resource.return_value = mock_instrument

        with MockInstrumentDriver(resource_manager=mock_rm) as driver:
            driver.connect("TCPIP0::192.168.1.1::INSTR")
            assert driver.is_connected

        mock_instrument.close.assert_called_once()
        assert not driver.is_connected


class TestDriverRegistry:
    """Tests for DriverRegistry class."""

    def setup_method(self) -> None:
        """Clear registry before each test."""
        DriverRegistry.clear()

    def test_register_driver(self) -> None:
        """Test registering a driver class."""
        DriverRegistry.register_driver("mock", MockInstrumentDriver)
        assert "mock" in DriverRegistry.list_drivers()

    def test_register_non_driver_raises(self) -> None:
        """Test registering non-InstrumentDriver class raises TypeError."""

        class NotADriver:
            pass

        with pytest.raises(TypeError, match="must be a subclass of InstrumentDriver"):
            DriverRegistry.register_driver("invalid", NotADriver)  # type: ignore[arg-type]

    def test_get_driver(self) -> None:
        """Test getting a registered driver."""
        DriverRegistry.register_driver("mock", MockInstrumentDriver)
        driver_class = DriverRegistry.get_driver("mock")
        assert driver_class == MockInstrumentDriver

    def test_get_driver_not_found_raises(self) -> None:
        """Test getting unregistered driver raises KeyError."""
        with pytest.raises(KeyError, match="No driver registered"):
            DriverRegistry.get_driver("nonexistent")

    def test_list_drivers(self) -> None:
        """Test listing registered drivers."""
        DriverRegistry.register_driver("driver1", MockInstrumentDriver)
        DriverRegistry.register_driver("driver2", MockInstrumentDriver)

        drivers = DriverRegistry.list_drivers()
        assert set(drivers) == {"driver1", "driver2"}

    def test_clear(self) -> None:
        """Test clearing registry."""
        DriverRegistry.register_driver("mock", MockInstrumentDriver)
        DriverRegistry.clear()
        assert DriverRegistry.list_drivers() == []