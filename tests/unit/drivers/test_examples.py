"""Unit tests for example instrument drivers (DMM and PSU) — HAL + MAL layers."""

from unittest.mock import MagicMock

import pytest

from ate_platform.drivers import DriverRegistry
from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.base_mal import BaseAbstraction
from ate_platform.drivers.capabilities import DMMCapabilities, PSUCapabilities
from ate_platform.drivers.examples.dmm import (
    DMM_DRIVER_NAME,
    DMMAbstraction,
    DMMDriver,
    DMMHALDriver,
)
from ate_platform.drivers.examples.psu import (
    PSU_DRIVER_NAME,
    PSUAbstraction,
    PSUDriver,
    PSUHALDriver,
)
from ate_platform.drivers.mock_factory import MockDriverFactory

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

    def test_capabilities_classvar(self) -> None:
        """DMMAbstraction should have DMMCapabilities as capabilities ClassVar."""
        assert DMMAbstraction.capabilities is DMMCapabilities

    def test_get_capabilities_returns_model(self) -> None:
        """get_capabilities should return a DMMCapabilities instance."""
        mock_driver = MagicMock(spec=BaseDriver)
        abstraction = DMMAbstraction(driver=mock_driver)
        caps = abstraction.get_capabilities()
        assert isinstance(caps, DMMCapabilities)
        assert caps.channels == 1
        assert caps.max_voltage == 1000.0
        assert caps.resolution_digits == 6.5

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

    def test_capabilities_classvar(self) -> None:
        """PSUAbstraction should have PSUCapabilities as capabilities ClassVar."""
        assert PSUAbstraction.capabilities is PSUCapabilities

    def test_get_capabilities_returns_model(self) -> None:
        """get_capabilities should return a PSUCapabilities instance."""
        mock_driver = MagicMock(spec=BaseDriver)
        abstraction = PSUAbstraction(driver=mock_driver)
        caps = abstraction.get_capabilities()
        assert isinstance(caps, PSUCapabilities)
        assert caps.channels == 1
        assert caps.max_voltage == 30.0
        assert caps.has_remote_sense is False

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
# Mock driver tests via MockDriverFactory
# ---------------------------------------------------------------------------


class TestMockDMMViaFactory:
    """Tests for auto-generated DMM mock driver via MockDriverFactory."""

    def test_connect_and_disconnect(self) -> None:
        """Test connection lifecycle."""
        dmm = MockDriverFactory.create_mock(DMMAbstraction)
        assert not dmm.is_connected

        dmm.connect("MOCK::DMM::INSTR")
        assert dmm.is_connected
        assert dmm.address == "MOCK::DMM::INSTR"

        dmm.disconnect()
        assert not dmm.is_connected
        assert dmm.address == ""

    def test_measure_voltage_returns_float(self) -> None:
        """Test voltage measurement returns valid float."""
        dmm = MockDriverFactory.create_mock(DMMAbstraction)
        dmm.connect("MOCK::DMM::INSTR")

        voltage = dmm.measure_voltage()
        assert isinstance(voltage, float)
        # Voltage should be one of the typical values with variation
        assert 3.0 <= voltage <= 25.0

    def test_measure_current_returns_float(self) -> None:
        """Test current measurement returns valid float."""
        dmm = MockDriverFactory.create_mock(DMMAbstraction)
        dmm.connect("MOCK::DMM::INSTR")

        current = dmm.measure_current()
        assert isinstance(current, float)
        assert 0.05 <= current <= 2.5

    def test_measure_resistance_returns_float(self) -> None:
        """Test resistance measurement returns valid float."""
        dmm = MockDriverFactory.create_mock(DMMAbstraction)
        dmm.connect("MOCK::DMM::INSTR")

        resistance = dmm.measure_resistance()
        assert isinstance(resistance, float)
        assert 50.0 <= resistance <= 15000.0

    def test_operations_fail_when_not_connected(self) -> None:
        """Test that operations fail when not connected."""
        dmm = MockDriverFactory.create_mock(DMMAbstraction)

        with pytest.raises(RuntimeError, match="Not connected"):
            dmm.measure_voltage()

    def test_custom_mock_values(self) -> None:
        """Test that configurable mock values are returned."""
        dmm = MockDriverFactory.create_mock(
            DMMAbstraction,
            mock_values={"MEAS:VOLT:DC?": "3.141592"},
        )
        dmm.connect("MOCK::DMM::INSTR")

        voltage = dmm.measure_voltage()
        assert voltage == 3.141592


class TestMockPSUViaFactory:
    """Tests for auto-generated PSU mock driver via MockDriverFactory."""

    def test_connect_and_disconnect(self) -> None:
        """Test connection lifecycle."""
        psu = MockDriverFactory.create_mock(PSUAbstraction)
        assert not psu.is_connected

        psu.connect("MOCK::PSU::INSTR")
        assert psu.is_connected

        psu.disconnect()
        assert not psu.is_connected

    def test_set_voltage_and_measure(self) -> None:
        """Test setting voltage and measuring output."""
        psu = MockDriverFactory.create_mock(PSUAbstraction)
        psu.connect("MOCK::PSU::INSTR")

        psu.set_voltage(5.0)
        psu.enable_output(True)

        voltage, current = psu.measure_output()
        assert isinstance(voltage, float)
        assert isinstance(current, float)
        # Should be near set voltage with small variation
        assert 4.8 <= voltage <= 5.2

    def test_measure_output_when_off_returns_zero(self) -> None:
        """Test that measurement returns zero when output is off."""
        psu = MockDriverFactory.create_mock(PSUAbstraction)
        psu.connect("MOCK::PSU::INSTR")

        psu.set_voltage(12.0)
        # Output is off by default
        voltage, current = psu.measure_output()
        assert voltage == 0.0
        assert current == 0.0

    def test_set_current_limit(self) -> None:
        """Test setting current limit."""
        psu = MockDriverFactory.create_mock(PSUAbstraction)
        psu.connect("MOCK::PSU::INSTR")

        psu.set_current(2.5)
        # Current limit is tracked but not directly readable through MAL API
        # This test just verifies it doesn't raise

    def test_operations_fail_when_not_connected(self) -> None:
        """Test that operations fail when not connected."""
        psu = MockDriverFactory.create_mock(PSUAbstraction)

        with pytest.raises(RuntimeError, match="Not connected"):
            psu.set_voltage(5.0)

    def test_custom_mock_values(self) -> None:
        """Test that configurable mock values are returned."""
        psu = MockDriverFactory.create_mock(
            PSUAbstraction,
            mock_values={"MEAS:VOLT?": "12.000000", "MEAS:CURR?": "0.500000"},
        )
        psu.connect("MOCK::PSU::INSTR")

        voltage, current = psu.measure_output()
        assert voltage == 12.0
        assert current == 0.5


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

    def test_list_drivers(self) -> None:
        """Test listing registered drivers."""
        drivers = DriverRegistry.list_drivers()
        assert DMM_DRIVER_NAME in drivers
        assert PSU_DRIVER_NAME in drivers

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
