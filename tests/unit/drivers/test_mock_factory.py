"""Unit tests for MockDriverFactory auto-mock generation."""

from unittest.mock import MagicMock

import pytest

from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.base_mal import BaseAbstraction
from ate_platform.drivers.capabilities import DMMCapabilities, PSUCapabilities
from ate_platform.drivers.examples.dmm import DMMAbstraction
from ate_platform.drivers.examples.psu import PSUAbstraction
from ate_platform.drivers.mock_factory import (
    MockDriverFactory,
    _MockBaseDriver,
    _MockDMMDriver,
    _MockPSUDriver,
)


class TestMockDriverFactoryBasics:
    """Tests for MockDriverFactory core behavior."""

    def test_create_mock_dmm_returns_abstraction(self) -> None:
        """create_mock should return a DMMAbstraction instance."""
        dmm = MockDriverFactory.create_mock(DMMAbstraction)
        assert isinstance(dmm, DMMAbstraction)

    def test_create_mock_psu_returns_abstraction(self) -> None:
        """create_mock should return a PSUAbstraction instance."""
        psu = MockDriverFactory.create_mock(PSUAbstraction)
        assert isinstance(psu, PSUAbstraction)

    def test_create_mock_rejects_non_abstraction(self) -> None:
        """create_mock should raise TypeError for non-BaseAbstraction classes."""

        class NotAnAbstraction:
            pass

        with pytest.raises(TypeError, match="Expected a BaseAbstraction subclass"):
            MockDriverFactory.create_mock(NotAnAbstraction)  # type: ignore[arg-type]

    def test_create_mock_rejects_unregistered_abstraction(self) -> None:
        """create_mock should raise ValueError for unregistered abstraction."""

        class UnregisteredAbstraction(BaseAbstraction):
            pass

        with pytest.raises(ValueError, match="No mock driver registered"):
            MockDriverFactory.create_mock(UnregisteredAbstraction)

    def test_custom_mock_values_dmm(self) -> None:
        """Configurable mock values should be returned for DMM."""
        dmm = MockDriverFactory.create_mock(
            DMMAbstraction,
            mock_values={"MEAS:VOLT:DC?": "3.141592"},
        )
        dmm.connect("MOCK::DMM")

        voltage = dmm.measure_voltage()
        assert voltage == 3.141592

    def test_custom_mock_values_psu(self) -> None:
        """Configurable mock values should be returned for PSU."""
        psu = MockDriverFactory.create_mock(
            PSUAbstraction,
            mock_values={"MEAS:VOLT?": "12.000000", "MEAS:CURR?": "0.500000"},
        )
        psu.connect("MOCK::PSU")

        voltage, current = psu.measure_output()
        assert voltage == 12.0
        assert current == 0.5

    def test_custom_mock_values_case_insensitive(self) -> None:
        """Mock values should be matched case-insensitively."""
        dmm = MockDriverFactory.create_mock(
            DMMAbstraction,
            mock_values={"meas:volt:dc?": "7.777777"},
        )
        dmm.connect("MOCK::DMM")

        voltage = dmm.measure_voltage()
        assert voltage == 7.777777

    def test_create_mock_abstraction_has_capabilities(self) -> None:
        """Mock abstraction should report capabilities from the abstraction class."""
        dmm = MockDriverFactory.create_mock(DMMAbstraction)
        caps = dmm.get_capabilities()
        assert isinstance(caps, DMMCapabilities)
        assert caps.channels == 1


class TestMockDriverFactoryRegistration:
    """Tests for MockDriverFactory registration API."""

    def test_register_and_create_custom(self) -> None:
        """Custom abstraction + mock pair should work after registration."""

        class CustomAbstraction(BaseAbstraction):
            def get_value(self) -> float:
                return float(self._driver.query("MEAS?").strip())

        class CustomMockDriver(_MockBaseDriver):
            def _generate_response(self, command: str) -> str:
                return "42.0"

        MockDriverFactory.register_mock(CustomAbstraction, CustomMockDriver)

        instance = MockDriverFactory.create_mock(CustomAbstraction)
        instance.connect("MOCK::CUSTOM")

        result = instance.get_value()
        assert result == 42.0

        # Clean up
        MockDriverFactory._MOCK_DRIVER_MAP.pop(CustomAbstraction, None)

    def test_clear_registrations(self) -> None:
        """clear_registrations should remove all registrations."""
        original = dict(MockDriverFactory._MOCK_DRIVER_MAP)
        try:
            MockDriverFactory.clear_registrations()
            assert MockDriverFactory._MOCK_DRIVER_MAP == {}
        finally:
            # Restore
            MockDriverFactory._MOCK_DRIVER_MAP.clear()
            MockDriverFactory._MOCK_DRIVER_MAP.update(original)


class TestMockDMMDriverInternals:
    """Tests for _MockDMMDriver internal behavior."""

    def test_query_not_connected_raises(self) -> None:
        """Querying when not connected should raise RuntimeError."""
        driver = _MockDMMDriver()

        with pytest.raises(RuntimeError, match="Not connected"):
            driver.query("MEAS:VOLT:DC?")

    def test_write_not_connected_raises(self) -> None:
        """Writing when not connected should raise RuntimeError."""
        driver = _MockDMMDriver()

        with pytest.raises(RuntimeError, match="Not connected"):
            driver.write("CONF:VOLT:DC 10")

    def test_voltage_query_returns_float_parseable(self) -> None:
        """Voltage query should return a string parseable as float."""
        driver = _MockDMMDriver()
        driver.connect("MOCK::DMM")

        response = driver.query("MEAS:VOLT:DC?")
        value = float(response.strip())
        assert isinstance(value, float)
        assert 3.0 <= value <= 25.0

    def test_current_query_returns_float_parseable(self) -> None:
        """Current query should return a string parseable as float."""
        driver = _MockDMMDriver()
        driver.connect("MOCK::DMM")

        response = driver.query("MEAS:CURR:DC?")
        value = float(response.strip())
        assert isinstance(value, float)
        assert 0.05 <= value <= 2.5

    def test_resistance_query_returns_float_parseable(self) -> None:
        """Resistance query should return a string parseable as float."""
        driver = _MockDMMDriver()
        driver.connect("MOCK::DMM")

        response = driver.query("MEAS:RES?")
        value = float(response.strip())
        assert isinstance(value, float)
        assert 50.0 <= value <= 15000.0

    def test_is_connected_property(self) -> None:
        """is_connected should reflect connection state."""
        driver = _MockDMMDriver()
        assert not driver.is_connected

        driver.connect("MOCK::DMM")
        assert driver.is_connected

        driver.disconnect()
        assert not driver.is_connected


class TestMockPSUDriverInternals:
    """Tests for _MockPSUDriver internal behavior."""

    def test_query_not_connected_raises(self) -> None:
        """Querying when not connected should raise RuntimeError."""
        driver = _MockPSUDriver()

        with pytest.raises(RuntimeError, match="Not connected"):
            driver.query("MEAS:VOLT?")

    def test_write_updates_state(self) -> None:
        """Writing VOLT command should update internal voltage state."""
        driver = _MockPSUDriver()
        driver.connect("MOCK::PSU")

        driver.write("VOLT 5.0")
        assert driver._channel_states[1]["voltage"] == 5.0

    def test_write_outp_on_updates_state(self) -> None:
        """Writing OUTP ON should update output state."""
        driver = _MockPSUDriver()
        driver.connect("MOCK::PSU")

        driver.write("OUTP ON")
        assert driver._channel_states[1]["output_on"] is True

    def test_write_outp_off_updates_state(self) -> None:
        """Writing OUTP OFF should update output state."""
        driver = _MockPSUDriver()
        driver.connect("MOCK::PSU")

        driver.write("OUTP ON")  # Turn on first
        driver.write("OUTP OFF")
        assert driver._channel_states[1]["output_on"] is False

    def test_write_curr_updates_state(self) -> None:
        """Writing CURR command should update current limit state."""
        driver = _MockPSUDriver()
        driver.connect("MOCK::PSU")

        driver.write("CURR 2.5")
        assert driver._channel_states[1]["current_limit"] == 2.5

    def test_write_inst_nsel_selects_channel(self) -> None:
        """Writing INST:NSEL should change selected channel."""
        driver = _MockPSUDriver()
        driver.connect("MOCK::PSU")

        driver.write("INST:NSEL 2")
        assert driver._selected_channel == 2

    def test_voltage_query_when_output_off_returns_zero(self) -> None:
        """Voltage query should return zero when output is off."""
        driver = _MockPSUDriver()
        driver.connect("MOCK::PSU")

        driver.write("VOLT 12.0")
        # Output is off by default
        response = driver.query("MEAS:VOLT?")
        value = float(response.strip())
        assert value == 0.0

    def test_voltage_query_when_output_on_returns_set_value(self) -> None:
        """Voltage query should return near-set value when output is on."""
        driver = _MockPSUDriver()
        driver.connect("MOCK::PSU")

        driver.write("VOLT 12.0")
        driver.write("OUTP ON")

        response = driver.query("MEAS:VOLT?")
        value = float(response.strip())
        assert 11.8 <= value <= 12.2

    def test_current_query_when_output_off_returns_zero(self) -> None:
        """Current query should return zero when output is off."""
        driver = _MockPSUDriver()
        driver.connect("MOCK::PSU")

        response = driver.query("MEAS:CURR?")
        value = float(response.strip())
        assert value == 0.0

    def test_current_query_when_output_on_returns_positive(self) -> None:
        """Current query should return positive value when output is on."""
        driver = _MockPSUDriver()
        driver.connect("MOCK::PSU")

        driver.write("OUTP ON")

        response = driver.query("MEAS:CURR?")
        value = float(response.strip())
        assert 0.05 <= value <= 0.85