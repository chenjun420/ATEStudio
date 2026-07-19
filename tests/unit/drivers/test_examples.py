"""Unit tests for example instrument drivers (DMM and PSU)."""

import pytest

from ate_platform.drivers import DriverRegistry
from ate_platform.drivers.examples.dmm import DMM_DRIVER_NAME, DMMDriver, MockDMMDriver
from ate_platform.drivers.examples.psu import MOCK_PSU_DRIVER_NAME, PSU_DRIVER_NAME, MockPSUDriver, PSUDriver


class TestMockDMMDriver:
    """Tests for MockDMMDriver."""

    def test_connect_and_disconnect(self) -> None:
        """Test connection lifecycle."""
        driver = MockDMMDriver()
        assert not driver.is_connected
        
        driver.connect("MOCK::DMM::INSTR")
        assert driver.is_connected
        assert driver.address == "MOCK::DMM::INSTR"
        
        driver.disconnect()
        assert not driver.is_connected
        assert driver.address == ""

    def test_measure_voltage_returns_float(self) -> None:
        """Test voltage measurement returns valid float."""
        driver = MockDMMDriver()
        driver.connect("MOCK::DMM::INSTR")
        
        voltage = driver.measure_voltage()
        assert isinstance(voltage, float)
        # Voltage should be one of the typical values with variation
        assert 3.0 <= voltage <= 25.0

    def test_measure_current_returns_float(self) -> None:
        """Test current measurement returns valid float."""
        driver = MockDMMDriver()
        driver.connect("MOCK::DMM::INSTR")
        
        current = driver.measure_current()
        assert isinstance(current, float)
        assert 0.05 <= current <= 2.5

    def test_measure_resistance_returns_float(self) -> None:
        """Test resistance measurement returns valid float."""
        driver = MockDMMDriver()
        driver.connect("MOCK::DMM::INSTR")
        
        resistance = driver.measure_resistance()
        assert isinstance(resistance, float)
        assert 50.0 <= resistance <= 15000.0

    def test_measure_with_channel(self) -> None:
        """Test measurements with channel parameter."""
        driver = MockDMMDriver()
        driver.connect("MOCK::DMM::INSTR")
        
        # Channel 2 should work
        voltage = driver.measure_voltage(channel=2)
        assert isinstance(voltage, float)
        
        current = driver.measure_current(channel=2)
        assert isinstance(current, float)

    def test_operations_fail_when_not_connected(self) -> None:
        """Test that operations fail when not connected."""
        driver = MockDMMDriver()
        
        with pytest.raises(RuntimeError, match="Not connected"):
            driver.measure_voltage()
        
        with pytest.raises(RuntimeError, match="Not connected"):
            driver.query("MEAS:VOLT:DC?")

    def test_context_manager(self) -> None:
        """Test context manager usage."""
        with MockDMMDriver() as driver:
            driver.connect("MOCK::DMM::INSTR")
            assert driver.is_connected
        
        # Should be disconnected after exiting context
        assert not driver.is_connected


class TestMockPSUDriver:
    """Tests for MockPSUDriver."""

    def test_connect_and_disconnect(self) -> None:
        """Test connection lifecycle."""
        driver = MockPSUDriver()
        assert not driver.is_connected
        
        driver.connect("MOCK::PSU::INSTR")
        assert driver.is_connected
        
        driver.disconnect()
        assert not driver.is_connected

    def test_set_voltage(self) -> None:
        """Test setting voltage."""
        driver = MockPSUDriver()
        driver.connect("MOCK::PSU::INSTR")
        
        driver.set_voltage(channel=1, voltage=5.0)
        state = driver.get_channel_state(1)
        assert state["voltage"] == 5.0

    def test_set_current_limit(self) -> None:
        """Test setting current limit."""
        driver = MockPSUDriver()
        driver.connect("MOCK::PSU::INSTR")
        
        driver.set_current_limit(channel=1, current=2.5)
        state = driver.get_channel_state(1)
        assert state["current_limit"] == 2.5

    def test_output_on_off(self) -> None:
        """Test output control."""
        driver = MockPSUDriver()
        driver.connect("MOCK::PSU::INSTR")
        
        # Output should start off
        state = driver.get_channel_state(1)
        assert state["output_on"] is False
        
        # Turn on
        driver.output_on(channel=1)
        state = driver.get_channel_state(1)
        assert state["output_on"] is True
        
        # Turn off
        driver.output_off(channel=1)
        state = driver.get_channel_state(1)
        assert state["output_on"] is False

    def test_measure_current_when_off(self) -> None:
        """Test current measurement when output is off."""
        driver = MockPSUDriver()
        driver.connect("MOCK::PSU::INSTR")
        driver.set_voltage(channel=1, voltage=5.0)
        driver.output_off(channel=1)
        
        current = driver.measure_current(channel=1)
        assert current == 0.0

    def test_measure_current_when_on(self) -> None:
        """Test current measurement when output is on."""
        driver = MockPSUDriver()
        driver.connect("MOCK::PSU::INSTR")
        driver.set_voltage(channel=1, voltage=5.0)
        driver.output_on(channel=1)
        
        current = driver.measure_current(channel=1)
        assert isinstance(current, float)
        # Should draw some current when on
        assert 0.0 <= current <= 1.0

    def test_measure_voltage_when_off(self) -> None:
        """Test voltage measurement when output is off."""
        driver = MockPSUDriver()
        driver.connect("MOCK::PSU::INSTR")
        driver.set_voltage(channel=1, voltage=12.0)
        driver.output_off(channel=1)
        
        voltage = driver.measure_voltage(channel=1)
        assert voltage == 0.0

    def test_measure_voltage_when_on(self) -> None:
        """Test voltage measurement when output is on."""
        driver = MockPSUDriver()
        driver.connect("MOCK::PSU::INSTR")
        driver.set_voltage(channel=1, voltage=12.0)
        driver.output_on(channel=1)
        
        voltage = driver.measure_voltage(channel=1)
        assert isinstance(voltage, float)
        # Should be near set voltage with small variation
        assert 11.5 <= voltage <= 12.5

    def test_multiple_channels(self) -> None:
        """Test independent channel control."""
        driver = MockPSUDriver()
        driver.connect("MOCK::PSU::INSTR")
        
        # Set different voltages on different channels
        driver.set_voltage(channel=1, voltage=5.0)
        driver.set_voltage(channel=2, voltage=12.0)
        
        state1 = driver.get_channel_state(1)
        state2 = driver.get_channel_state(2)
        
        assert state1["voltage"] == 5.0
        assert state2["voltage"] == 12.0
        
        # Turn on only channel 1
        driver.output_on(channel=1)
        assert driver.get_channel_state(1)["output_on"] is True
        assert driver.get_channel_state(2)["output_on"] is False

    def test_operations_fail_when_not_connected(self) -> None:
        """Test that operations fail when not connected."""
        driver = MockPSUDriver()
        
        with pytest.raises(RuntimeError, match="Not connected"):
            driver.set_voltage(channel=1, voltage=5.0)
        
        with pytest.raises(RuntimeError, match="Not connected"):
            driver.output_on()


class TestDriverRegistry:
    """Tests for DriverRegistry with example drivers."""

    def test_list_drivers(self) -> None:
        """Test listing registered drivers."""
        drivers = DriverRegistry.list_drivers()
        assert DMM_DRIVER_NAME in drivers
        assert PSU_DRIVER_NAME in drivers
        assert MOCK_PSU_DRIVER_NAME in drivers

    def test_get_dmm_driver(self) -> None:
        """Test retrieving DMM driver class."""
        driver_class = DriverRegistry.get_driver(DMM_DRIVER_NAME)
        assert driver_class is DMMDriver

    def test_get_psu_driver(self) -> None:
        """Test retrieving PSU driver class."""
        driver_class = DriverRegistry.get_driver(PSU_DRIVER_NAME)
        assert driver_class is PSUDriver

    def test_get_mock_psu_driver(self) -> None:
        """Test retrieving mock PSU driver class."""
        driver_class = DriverRegistry.get_driver(MOCK_PSU_DRIVER_NAME)
        assert driver_class is MockPSUDriver

    def test_get_mock_dmm_driver(self) -> None:
        """Test retrieving mock DMM driver class."""
        driver_class = DriverRegistry.get_driver(f"mock_{DMM_DRIVER_NAME}")
        assert driver_class is MockDMMDriver

    def test_get_unknown_driver_raises(self) -> None:
        """Test that unknown driver raises KeyError."""
        with pytest.raises(KeyError, match="No driver registered"):
            DriverRegistry.get_driver("unknown_driver")


class TestDMMDriverSignature:
    """Test DMMDriver has correct method signatures (without real hardware)."""

    def test_method_signatures(self) -> None:
        """Verify method signatures exist."""
        # Just verify the methods exist and have correct signatures
        assert hasattr(DMMDriver, "measure_voltage")
        assert hasattr(DMMDriver, "measure_current")
        assert hasattr(DMMDriver, "measure_resistance")


class TestPSUDriverSignature:
    """Test PSUDriver has correct method signatures (without real hardware)."""

    def test_method_signatures(self) -> None:
        """Verify method signatures exist."""
        assert hasattr(PSUDriver, "set_voltage")
        assert hasattr(PSUDriver, "set_current_limit")
        assert hasattr(PSUDriver, "output_on")
        assert hasattr(PSUDriver, "output_off")
        assert hasattr(PSUDriver, "measure_current")
        assert hasattr(PSUDriver, "measure_voltage")