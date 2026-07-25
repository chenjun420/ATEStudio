"""Unit tests for example instrument drivers (DMM and PSU) — HAL + MAL layers."""

from unittest.mock import MagicMock

import pytest

from ate_platform.drivers import DriverRegistry
from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.base_mal import BaseAbstraction
from ate_platform.drivers.examples.dmm import (
    DMM_DRIVER_NAME,
    DMMHALDriver,
    DMMAbstraction,
    DMMDriver,
    MockDMMDriver,
)
from ate_platform.drivers.examples.psu import (
    MOCK_PSU_DRIVER_NAME,
    PSU_DRIVER_NAME,
    PSUHALDriver,
    PSUAbstraction,
    PSUDriver,
    MockPSUDriver,
)


# ---------------------------------------------------------------------------
# DMM HAL + MAL tests
# ---------------------------------------------------------------------------


class TestDMMHALDriver:
    """Tests for DMMHALDriver (HAL layer)."""

    def test_is_base_driver_subclass(self) -> None:
        """DMMHALDriver should be a BaseDriver subclass."""
        assert issubclass(DMMHALDriver, BaseDriver)

    def test_backward_compat_alias(self) -> None:
        """DMMDriver should be an alias for DMMHALDriver."""
        assert DMMDriver is DMMHALDriver


class TestDMMAbstraction:
    """Tests for DMMAbstraction (MAL layer)."""

    def test_is_base_abstraction_subclass(self) -> None:
        """DMMAbstraction should be a BaseAbstraction subclass."""
        assert issubclass(DMMAbstraction, BaseAbstraction)

    def test_measure_voltage_calls_driver_query(self) -> None:
        """measure_voltage should call self._driver.query with MEAS:VOLT:DC?."""
        mock_driver = MagicMock(spec=BaseDriver)
        mock_driver.query.return_value = "5.123"
        abstraction = DMMAbstraction(driver=mock_driver)

        result = abstraction.measure_voltage()

        mock_driver.query.assert_called_once_with("MEAS:VOLT:DC?")
        assert result == 5.123

    def test_measure_voltage_with_range(self) -> None:
        """measure_voltage with range should send CONF then MEAS."""
        mock_driver = MagicMock(spec=BaseDriver)
        mock_driver.query.return_value = "12.5"
        abstraction = DMMAbstraction(driver=mock_driver)

        result = abstraction.measure_voltage(range="100")

        mock_driver.write.assert_called_once_with("CONF:VOLT:DC 100")
        mock_driver.query.assert_called_once_with("MEAS:VOLT:DC?")
        assert result == 12.5

    def test_measure_current_calls_driver_query(self) -> None:
        """measure_current should call self._driver.query with MEAS:CURR:DC?."""
        mock_driver = MagicMock(spec=BaseDriver)
        mock_driver.query.return_value = "0.5"
        abstraction = DMMAbstraction(driver=mock_driver)

        result = abstraction.measure_current()

        mock_driver.query.assert_called_once_with("MEAS:CURR:DC?")
        assert result == 0.5

    def test_measure_current_with_range(self) -> None:
        """measure_current with range should send CONF then MEAS."""
        mock_driver = MagicMock(spec=BaseDriver)
        mock_driver.query.return_value = "1.5"
        abstraction = DMMAbstraction(driver=mock_driver)

        result = abstraction.measure_current(range="3")

        mock_driver.write.assert_called_once_with("CONF:CURR:DC 3")
        mock_driver.query.assert_called_once_with("MEAS:CURR:DC?")
        assert result == 1.5

    def test_measure_resistance_calls_driver_query(self) -> None:
        """measure_resistance should call self._driver.query with MEAS:RES?."""
        mock_driver = MagicMock(spec=BaseDriver)
        mock_driver.query.return_value = "1000.0"
        abstraction = DMMAbstraction(driver=mock_driver)

        result = abstraction.measure_resistance()

        mock_driver.query.assert_called_once_with("MEAS:RES?")
        assert result == 1000.0

    def test_measure_resistance_with_range(self) -> None:
        """measure_resistance with range should send CONF then MEAS."""
        mock_driver = MagicMock(spec=BaseDriver)
        mock_driver.query.return_value = "4700.0"
        abstraction = DMMAbstraction(driver=mock_driver)

        result = abstraction.measure_resistance(range="10K")

        mock_driver.write.assert_called_once_with("CONF:RES 10K")
        mock_driver.query.assert_called_once_with("MEAS:RES?")
        assert result == 4700.0


# ---------------------------------------------------------------------------
# PSU HAL + MAL tests
# ---------------------------------------------------------------------------


class TestPSUHALDriver:
    """Tests for PSUHALDriver (HAL layer)."""

    def test_is_base_driver_subclass(self) -> None:
        """PSUHALDriver should be a BaseDriver subclass."""
        assert issubclass(PSUHALDriver, BaseDriver)

    def test_backward_compat_alias(self) -> None:
        """PSUDriver should be an alias for PSUHALDriver."""
        assert PSUDriver is PSUHALDriver


class TestPSUAbstraction:
    """Tests for PSUAbstraction (MAL layer)."""

    def test_is_base_abstraction_subclass(self) -> None:
        """PSUAbstraction should be a BaseAbstraction subclass."""
        assert issubclass(PSUAbstraction, BaseAbstraction)

    def test_set_voltage_calls_driver_write(self) -> None:
        """set_voltage should call self._driver.write with VOLT command."""
        mock_driver = MagicMock(spec=BaseDriver)
        abstraction = PSUAbstraction(driver=mock_driver)

        abstraction.set_voltage(5.0)

        mock_driver.write.assert_called_once_with("VOLT 5.0")

    def test_set_current_calls_driver_write(self) -> None:
        """set_current should call self._driver.write with CURR command."""
        mock_driver = MagicMock(spec=BaseDriver)
        abstraction = PSUAbstraction(driver=mock_driver)

        abstraction.set_current(2.5)

        mock_driver.write.assert_called_once_with("CURR 2.5")

    def test_enable_output_on(self) -> None:
        """enable_output(True) should call self._driver.write with OUTP ON."""
        mock_driver = MagicMock(spec=BaseDriver)
        abstraction = PSUAbstraction(driver=mock_driver)

        abstraction.enable_output(True)

        mock_driver.write.assert_called_once_with("OUTP ON")

    def test_enable_output_off(self) -> None:
        """enable_output(False) should call self._driver.write with OUTP OFF."""
        mock_driver = MagicMock(spec=BaseDriver)
        abstraction = PSUAbstraction(driver=mock_driver)

        abstraction.enable_output(False)

        mock_driver.write.assert_called_once_with("OUTP OFF")

    def test_enable_output_default_on(self) -> None:
        """enable_output() default should enable output."""
        mock_driver = MagicMock(spec=BaseDriver)
        abstraction = PSUAbstraction(driver=mock_driver)

        abstraction.enable_output()

        mock_driver.write.assert_called_once_with("OUTP ON")

    def test_measure_output_returns_tuple(self) -> None:
        """measure_output should query MEAS:VOLT? and MEAS:CURR? and return tuple."""
        mock_driver = MagicMock(spec=BaseDriver)
        mock_driver.query.side_effect = ["12.0", "0.5"]
        abstraction = PSUAbstraction(driver=mock_driver)

        voltage, current = abstraction.measure_output()

        assert mock_driver.query.call_count == 2
        mock_driver.query.assert_any_call("MEAS:VOLT?")
        mock_driver.query.assert_any_call("MEAS:CURR?")
        assert voltage == 12.0
        assert current == 0.5


# ---------------------------------------------------------------------------
# Mock DMM driver tests (kept for backward compat)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Mock PSU driver tests (kept for backward compat)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# DriverRegistry integration tests
# ---------------------------------------------------------------------------


class TestDriverRegistry:
    """Tests for DriverRegistry with example drivers."""

    def setup_method(self) -> None:
        """Re-register drivers in case prior tests cleared the registry."""
        # Module-level registrations only run once at import time.
        # If test_base.py cleared the registry, we must re-register here.
        current = DriverRegistry.list_drivers()
        if DMM_DRIVER_NAME not in current:
            DriverRegistry.register(DMM_DRIVER_NAME, hal_cls=DMMHALDriver, mal_cls=DMMAbstraction)
        if PSU_DRIVER_NAME not in current:
            DriverRegistry.register(PSU_DRIVER_NAME, hal_cls=PSUHALDriver, mal_cls=PSUAbstraction)
        if MOCK_PSU_DRIVER_NAME not in current:
            DriverRegistry.register_driver(MOCK_PSU_DRIVER_NAME, MockPSUDriver)
        if f"mock_{DMM_DRIVER_NAME}" not in current:
            DriverRegistry.register_driver(f"mock_{DMM_DRIVER_NAME}", MockDMMDriver)

    def test_list_drivers(self) -> None:
        """Test listing registered drivers."""
        drivers = DriverRegistry.list_drivers()
        assert DMM_DRIVER_NAME in drivers
        assert PSU_DRIVER_NAME in drivers
        assert MOCK_PSU_DRIVER_NAME in drivers

    def test_get_dmm_driver_mal(self) -> None:
        """Test retrieving DMM driver returns DMMAbstraction by default."""
        driver_class = DriverRegistry.get_driver(DMM_DRIVER_NAME)
        assert driver_class is DMMAbstraction

    def test_get_dmm_driver_hal(self) -> None:
        """Test retrieving DMM HAL driver with layer='hal'."""
        driver_class = DriverRegistry.get_driver(DMM_DRIVER_NAME, layer="hal")
        assert driver_class is DMMHALDriver

    def test_get_psu_driver_mal(self) -> None:
        """Test retrieving PSU driver returns PSUAbstraction by default."""
        driver_class = DriverRegistry.get_driver(PSU_DRIVER_NAME)
        assert driver_class is PSUAbstraction

    def test_get_psu_driver_hal(self) -> None:
        """Test retrieving PSU HAL driver with layer='hal'."""
        driver_class = DriverRegistry.get_driver(PSU_DRIVER_NAME, layer="hal")
        assert driver_class is PSUHALDriver

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


# ---------------------------------------------------------------------------
# Signature tests (without real hardware)
# ---------------------------------------------------------------------------


class TestDMMHALDriverSignature:
    """Test DMMHALDriver has correct HAL method signatures."""

    def test_hal_methods(self) -> None:
        """Verify HAL methods exist."""
        assert hasattr(DMMHALDriver, "connect")
        assert hasattr(DMMHALDriver, "disconnect")
        assert hasattr(DMMHALDriver, "write")
        assert hasattr(DMMHALDriver, "query")
        assert hasattr(DMMHALDriver, "read")
        assert hasattr(DMMHALDriver, "reset")


class TestDMMAbstractionSignature:
    """Test DMMAbstraction has correct MAL method signatures."""

    def test_mal_methods(self) -> None:
        """Verify MAL methods exist."""
        assert hasattr(DMMAbstraction, "measure_voltage")
        assert hasattr(DMMAbstraction, "measure_current")
        assert hasattr(DMMAbstraction, "measure_resistance")


class TestPSUHALDriverSignature:
    """Test PSUHALDriver has correct HAL method signatures."""

    def test_hal_methods(self) -> None:
        """Verify HAL methods exist."""
        assert hasattr(PSUHALDriver, "connect")
        assert hasattr(PSUHALDriver, "disconnect")
        assert hasattr(PSUHALDriver, "write")
        assert hasattr(PSUHALDriver, "query")
        assert hasattr(PSUHALDriver, "read")
        assert hasattr(PSUHALDriver, "reset")


class TestPSUAbstractionSignature:
    """Test PSUAbstraction has correct MAL method signatures."""

    def test_mal_methods(self) -> None:
        """Verify MAL methods exist."""
        assert hasattr(PSUAbstraction, "set_voltage")
        assert hasattr(PSUAbstraction, "set_current")
        assert hasattr(PSUAbstraction, "enable_output")
        assert hasattr(PSUAbstraction, "measure_output")