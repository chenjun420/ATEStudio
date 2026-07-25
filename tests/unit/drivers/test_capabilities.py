"""Unit tests for instrument capability models."""

import pytest
from pydantic import ValidationError

from ate_platform.drivers.capabilities import DMMCapabilities, PSUCapabilities


class TestDMMCapabilities:
    """Tests for DMMCapabilities Pydantic model."""

    def test_defaults(self) -> None:
        """DMMCapabilities should have the specified defaults."""
        caps = DMMCapabilities()
        assert caps.channels == 1
        assert caps.max_voltage == 1000.0
        assert caps.max_current == 3.0
        assert caps.can_measure_resistance is True
        assert caps.can_measure_current is True
        assert caps.resolution_digits == 6.5

    def test_custom_values(self) -> None:
        """DMMCapabilities should accept custom values."""
        caps = DMMCapabilities(
            channels=2,
            max_voltage=600.0,
            max_current=10.0,
            can_measure_resistance=True,
            can_measure_current=True,
            resolution_digits=7.5,
        )
        assert caps.channels == 2
        assert caps.max_voltage == 600.0
        assert caps.max_current == 10.0
        assert caps.can_measure_resistance is True
        assert caps.can_measure_current is True
        assert caps.resolution_digits == 7.5

    def test_is_frozen(self) -> None:
        """DMMCapabilities should be immutable after creation."""
        caps = DMMCapabilities()
        with pytest.raises(ValidationError):
            caps.channels = 2  # type: ignore[misc]

    def test_invalid_channels_negative(self) -> None:
        """DMMCapabilities should reject negative channels."""
        with pytest.raises(ValidationError):
            DMMCapabilities(channels=-1)

    def test_invalid_channels_zero(self) -> None:
        """DMMCapabilities should reject zero channels."""
        with pytest.raises(ValidationError):
            DMMCapabilities(channels=0)

    def test_invalid_max_voltage_zero(self) -> None:
        """DMMCapabilities should reject zero max_voltage."""
        with pytest.raises(ValidationError):
            DMMCapabilities(max_voltage=0.0)

    def test_invalid_max_voltage_negative(self) -> None:
        """DMMCapabilities should reject negative max_voltage."""
        with pytest.raises(ValidationError):
            DMMCapabilities(max_voltage=-500.0)

    def test_invalid_max_current_zero(self) -> None:
        """DMMCapabilities should reject zero max_current."""
        with pytest.raises(ValidationError):
            DMMCapabilities(max_current=0.0)

    def test_invalid_resolution_digits_zero(self) -> None:
        """DMMCapabilities should reject zero resolution_digits."""
        with pytest.raises(ValidationError):
            DMMCapabilities(resolution_digits=0.0)


class TestPSUCapabilities:
    """Tests for PSUCapabilities Pydantic model."""

    def test_defaults(self) -> None:
        """PSUCapabilities should have the specified defaults."""
        caps = PSUCapabilities()
        assert caps.channels == 1
        assert caps.max_voltage == 30.0
        assert caps.max_current == 3.0
        assert caps.has_remote_sense is False

    def test_custom_values(self) -> None:
        """PSUCapabilities should accept custom values."""
        caps = PSUCapabilities(
            channels=3,
            max_voltage=60.0,
            max_current=5.0,
            has_remote_sense=True,
        )
        assert caps.channels == 3
        assert caps.max_voltage == 60.0
        assert caps.max_current == 5.0
        assert caps.has_remote_sense is True

    def test_is_frozen(self) -> None:
        """PSUCapabilities should be immutable after creation."""
        caps = PSUCapabilities()
        with pytest.raises(ValidationError):
            caps.channels = 2  # type: ignore[misc]

    def test_invalid_channels_negative(self) -> None:
        """PSUCapabilities should reject negative channels."""
        with pytest.raises(ValidationError):
            PSUCapabilities(channels=-1)

    def test_invalid_channels_zero(self) -> None:
        """PSUCapabilities should reject zero channels."""
        with pytest.raises(ValidationError):
            PSUCapabilities(channels=0)

    def test_invalid_max_voltage_zero(self) -> None:
        """PSUCapabilities should reject zero max_voltage."""
        with pytest.raises(ValidationError):
            PSUCapabilities(max_voltage=0.0)

    def test_invalid_max_current_zero(self) -> None:
        """PSUCapabilities should reject zero max_current."""
        with pytest.raises(ValidationError):
            PSUCapabilities(max_current=0.0)
