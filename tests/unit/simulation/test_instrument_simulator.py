"""Tests for Tier 1: InstrumentSimulator.

Covers:
- Noise model selection and application (Gaussian, drift, bias, full, none)
- DMM range overflow (saturate and error modes)
- Scope noise floor simulation
- Reproducibility via seed
- Configurable parameters
- simulate_measurement convenience methods
- Statistics and diagnostics
"""

from __future__ import annotations

import statistics

import pytest

from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.simulation.instrument_simulator import (
    InstrumentSimulator,
    NoiseConfig,
    NoiseModel,
)


class _TestSimDriver(BaseDriver):
    """Minimal SIM-mode driver for testing. Returns a fixed value."""

    def __init__(self, value: float = 1.0) -> None:
        super().__init__(mode="SIM", noise_sigma=0.0)
        self._value: float = value

    def query(self, command: str, delay: float | None = None) -> str:  # noqa: PLW0221
        if command.strip().upper() == "*IDN?":
            return "SIM_TEST"
        return str(self._value)

    def read(self) -> str:  # noqa: PLW0221
        return str(self._value)


class _TestSciSimDriver(BaseDriver):
    """SIM driver that returns scientific notation."""

    def __init__(self, value: float = 3.3) -> None:
        super().__init__(mode="SIM", noise_sigma=0.0)
        self._value: float = value

    def query(self, command: str, delay: float | None = None) -> str:  # noqa: PLW0221
        if command.strip().upper() == "*IDN?":
            return "SIM_SCI"
        return f"{self._value:.6E}"

    def read(self) -> str:  # noqa: PLW0221
        return f"{self._value:.6E}"


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------


class TestInstrumentSimulatorInit:
    """Tests for InstrumentSimulator initialization."""

    def test_init_with_defaults(self) -> None:
        """Should initialize with default config and GENERIC type."""
        driver = _TestSimDriver()
        driver.connect("SIM")
        sim = InstrumentSimulator(driver)

        assert sim.instrument_type == "GENERIC"
        assert sim.config.model == NoiseModel.GAUSSIAN
        assert sim.config.noise_sigma == 0.001
        assert sim.config.seed == 42
        assert sim.is_connected is True
        assert sim.query_count == 0

    def test_init_with_custom_type(self) -> None:
        """Should accept custom instrument type (case-insensitive)."""
        driver = _TestSimDriver()
        driver.connect("SIM")
        sim = InstrumentSimulator(driver, instrument_type="dmm")

        assert sim.instrument_type == "DMM"

    def test_init_with_custom_config(self) -> None:
        """Should accept custom NoiseConfig."""
        driver = _TestSimDriver()
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.FULL,
            noise_sigma=0.5,
            drift_rate=0.01,
            bias=0.1,
            seed=123,
        )
        sim = InstrumentSimulator(driver, instrument_type="SCOPE", config=config)

        assert sim.config.model == NoiseModel.FULL
        assert sim.config.noise_sigma == 0.5
        assert sim.config.drift_rate == 0.01
        assert sim.config.bias == 0.1
        assert sim.config.seed == 123

    def test_init_invalid_type_raises(self) -> None:
        """Should raise ValueError for unsupported instrument type."""
        driver = _TestSimDriver()
        with pytest.raises(ValueError, match="Unsupported instrument_type"):
            InstrumentSimulator(driver, instrument_type="UNKNOWN")

    def test_init_strips_and_uppercases_type(self) -> None:
        """Should normalize instrument type to uppercase."""
        driver = _TestSimDriver()
        sim = InstrumentSimulator(driver, instrument_type="  scope  ")
        assert sim.instrument_type == "SCOPE"


# ---------------------------------------------------------------------------
# Noise model tests
# ---------------------------------------------------------------------------


class TestNoiseModels:
    """Tests for noise model application."""

    def test_none_model_passes_through(self) -> None:
        """NONE model should return the raw value unchanged."""
        driver = _TestSimDriver(value=5.0)
        driver.connect("SIM")
        config = NoiseConfig(model=NoiseModel.NONE, seed=42)
        sim = InstrumentSimulator(driver, config=config)

        result = sim.query("MEAS:VOLT:DC?")
        assert float(result) == 5.0

    def test_gaussian_adds_noise(self) -> None:
        """GAUSSIAN model should add noise (value differs from raw)."""
        driver = _TestSimDriver(value=5.0)
        driver.connect("SIM")
        config = NoiseConfig(model=NoiseModel.GAUSSIAN, noise_sigma=1.0, seed=42)
        sim = InstrumentSimulator(driver, config=config)

        results = [float(sim.query("MEAS?")) for _ in range(100)]

        # The mean should be close to 5.0 (within 3 sigma)
        mean = statistics.mean(results)
        assert abs(mean - 5.0) < 1.0

        # There should be variance (noise was added)
        stdev = statistics.stdev(results)
        assert stdev > 0.1

    def test_gaussian_bias_adds_constant_offset(self) -> None:
        """GAUSSIAN_BIAS model should add bias to the mean."""
        driver = _TestSimDriver(value=0.0)
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.GAUSSIAN_BIAS,
            noise_sigma=0.001,
            bias=2.0,
            seed=42,
        )
        sim = InstrumentSimulator(driver, config=config)

        results = [float(sim.query("MEAS?")) for _ in range(1000)]
        mean = statistics.mean(results)
        # Mean should be close to 0.0 + 2.0 bias
        assert abs(mean - 2.0) < 0.1

    def test_gaussian_drift_increases_over_time(self) -> None:
        """GAUSSIAN_DRIFT model should show increasing values over time."""
        driver = _TestSimDriver(value=0.0)
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.GAUSSIAN_DRIFT,
            noise_sigma=0.001,
            drift_rate=100.0,  # Large drift to make effect visible
            seed=42,
        )
        sim = InstrumentSimulator(driver, config=config)

        # First measurement
        first = sim.simulate_measurement(0.0)

        # Sleep briefly to let drift accumulate
        import time
        time.sleep(0.05)

        # Reset query_count to avoid count interference
        sim._state.query_count = 0
        later = sim.simulate_measurement(0.0)

        # Later value should be higher due to drift
        assert later > first

    def test_full_model_combines_all(self) -> None:
        """FULL model should apply noise + drift + bias."""
        driver = _TestSimDriver(value=1.0)
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.FULL,
            noise_sigma=0.01,
            drift_rate=0.0,  # No drift for deterministic check
            bias=5.0,
            seed=42,
        )
        sim = InstrumentSimulator(driver, config=config)

        results = [float(sim.query("MEAS?")) for _ in range(1000)]
        mean = statistics.mean(results)
        # 1.0 (raw) + 5.0 (bias) = 6.0
        assert abs(mean - 6.0) < 0.1

    def test_non_numeric_response_passthrough(self) -> None:
        """Non-numeric responses (like *IDN?) should pass through unchanged."""
        driver = _TestSimDriver()
        driver.connect("SIM")
        config = NoiseConfig(model=NoiseModel.FULL, noise_sigma=10.0, seed=42)
        sim = InstrumentSimulator(driver, config=config)

        result = sim.query("*IDN?")
        assert result == "SIM_TEST"

    def test_scientific_notation_preserved(self) -> None:
        """Should preserve scientific notation format from driver."""
        driver = _TestSciSimDriver(value=3.3)
        driver.connect("SIM")
        config = NoiseConfig(model=NoiseModel.NONE, seed=42)
        sim = InstrumentSimulator(driver, config=config)

        result = sim.query("MEAS:VOLT:DC?")
        assert "E" in result.upper()


# ---------------------------------------------------------------------------
# DMM overflow tests
# ---------------------------------------------------------------------------


class TestDMMOverflow:
    """Tests for DMM range overflow simulation."""

    def test_saturate_mode_clamps_value(self) -> None:
        """Saturate mode should clamp values to the threshold."""
        driver = _TestSimDriver(value=15.0)
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.NONE,
            dmm_overflow_threshold=10.0,
            dmm_overflow_behavior="saturate",
            seed=42,
        )
        sim = InstrumentSimulator(driver, instrument_type="DMM", config=config)

        result = float(sim.query("MEAS:VOLT:DC?"))
        assert result == 10.0

    def test_saturate_preserves_negative_sign(self) -> None:
        """Saturate should clamp negative values to -threshold."""
        driver = _TestSimDriver(value=-15.0)
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.NONE,
            dmm_overflow_threshold=10.0,
            dmm_overflow_behavior="saturate",
            seed=42,
        )
        sim = InstrumentSimulator(driver, instrument_type="DMM", config=config)

        result = float(sim.query("MEAS:VOLT:DC?"))
        assert result == -10.0

    def test_error_mode_raises_overflow(self) -> None:
        """Error mode should raise OverflowError when threshold exceeded."""
        driver = _TestSimDriver(value=15.0)
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.NONE,
            dmm_overflow_threshold=10.0,
            dmm_overflow_behavior="error",
            seed=42,
        )
        sim = InstrumentSimulator(driver, instrument_type="DMM", config=config)

        with pytest.raises(OverflowError, match="DMM range overflow"):
            sim.query("MEAS:VOLT:DC?")

    def test_no_overflow_when_within_range(self) -> None:
        """Values within threshold should not be affected."""
        driver = _TestSimDriver(value=5.0)
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.NONE,
            dmm_overflow_threshold=10.0,
            dmm_overflow_behavior="error",
            seed=42,
        )
        sim = InstrumentSimulator(driver, instrument_type="DMM", config=config)

        result = float(sim.query("MEAS:VOLT:DC?"))
        assert result == 5.0

    def test_no_threshold_disables_overflow(self) -> None:
        """None threshold should disable overflow simulation."""
        driver = _TestSimDriver(value=1000.0)
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.NONE,
            dmm_overflow_threshold=None,
            seed=42,
        )
        sim = InstrumentSimulator(driver, instrument_type="DMM", config=config)

        result = float(sim.query("MEAS:VOLT:DC?"))
        assert result == 1000.0

    def test_overflow_only_applies_to_dmm(self) -> None:
        """Overflow should NOT apply to non-DMM instruments."""
        driver = _TestSimDriver(value=15.0)
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.NONE,
            dmm_overflow_threshold=10.0,
            dmm_overflow_behavior="error",
            seed=42,
        )
        sim = InstrumentSimulator(driver, instrument_type="SCOPE", config=config)

        # Should NOT raise - overflow only for DMM
        result = float(sim.query("MEAS?"))
        assert result == 15.0


# ---------------------------------------------------------------------------
# Scope noise floor tests
# ---------------------------------------------------------------------------


class TestScopeNoiseFloor:
    """Tests for oscilloscope noise floor simulation."""

    def test_below_floor_replaced(self) -> None:
        """Values below noise floor should be replaced with floor-level value."""
        driver = _TestSimDriver(value=0.001)
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.NONE,
            scope_noise_floor=0.01,
            seed=42,
        )
        sim = InstrumentSimulator(driver, instrument_type="SCOPE", config=config)

        result = float(sim.query("MEAS?"))
        # Should be at least 0.5 * floor = 0.005
        assert abs(result) >= 0.005

    def test_above_floor_unchanged(self) -> None:
        """Values above noise floor should not be affected."""
        driver = _TestSimDriver(value=5.0)
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.NONE,
            scope_noise_floor=0.01,
            seed=42,
        )
        sim = InstrumentSimulator(driver, instrument_type="SCOPE", config=config)

        result = float(sim.query("MEAS?"))
        assert result == 5.0

    def test_negative_below_floor_preserves_sign(self) -> None:
        """Negative values below floor should preserve sign."""
        driver = _TestSimDriver(value=-0.001)
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.NONE,
            scope_noise_floor=0.01,
            seed=42,
        )
        sim = InstrumentSimulator(driver, instrument_type="SCOPE", config=config)

        result = float(sim.query("MEAS?"))
        assert result < 0  # Sign preserved
        assert abs(result) >= 0.005  # At floor level

    def test_no_floor_disables_simulation(self) -> None:
        """None floor should disable noise floor simulation."""
        driver = _TestSimDriver(value=0.001)
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.NONE,
            scope_noise_floor=None,
            seed=42,
        )
        sim = InstrumentSimulator(driver, instrument_type="SCOPE", config=config)

        result = float(sim.query("MEAS?"))
        assert result == 0.001

    def test_floor_only_applies_to_scope(self) -> None:
        """Noise floor should NOT apply to non-SCOPE instruments."""
        driver = _TestSimDriver(value=0.001)
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.NONE,
            scope_noise_floor=0.01,
            seed=42,
        )
        sim = InstrumentSimulator(driver, instrument_type="DMM", config=config)

        result = float(sim.query("MEAS?"))
        assert result == 0.001


# ---------------------------------------------------------------------------
# Reproducibility tests
# ---------------------------------------------------------------------------


class TestReproducibility:
    """Tests for seed-based reproducibility."""

    def test_same_seed_produces_same_results(self) -> None:
        """Same seed should produce identical noise values."""
        driver1 = _TestSimDriver(value=5.0)
        driver1.connect("SIM")
        driver2 = _TestSimDriver(value=5.0)
        driver2.connect("SIM")

        config = NoiseConfig(model=NoiseModel.GAUSSIAN, noise_sigma=1.0, seed=99)
        sim1 = InstrumentSimulator(driver1, config=config)
        sim2 = InstrumentSimulator(driver2, config=config)

        results1 = [float(sim1.query("MEAS?")) for _ in range(10)]
        results2 = [float(sim2.query("MEAS?")) for _ in range(10)]

        assert results1 == results2

    def test_different_seeds_produce_different_results(self) -> None:
        """Different seeds should produce different noise values."""
        driver1 = _TestSimDriver(value=5.0)
        driver1.connect("SIM")
        driver2 = _TestSimDriver(value=5.0)
        driver2.connect("SIM")

        config1 = NoiseConfig(model=NoiseModel.GAUSSIAN, noise_sigma=1.0, seed=1)
        config2 = NoiseConfig(model=NoiseModel.GAUSSIAN, noise_sigma=1.0, seed=2)
        sim1 = InstrumentSimulator(driver1, config=config1)
        sim2 = InstrumentSimulator(driver2, config=config2)

        results1 = [float(sim1.query("MEAS?")) for _ in range(10)]
        results2 = [float(sim2.query("MEAS?")) for _ in range(10)]

        assert results1 != results2

    def test_reset_restores_reproducibility(self) -> None:
        """reset() should restore the RNG to initial state."""
        driver = _TestSimDriver(value=5.0)
        driver.connect("SIM")
        config = NoiseConfig(model=NoiseModel.GAUSSIAN, noise_sigma=1.0, seed=42)
        sim = InstrumentSimulator(driver, config=config)

        first_batch = [float(sim.query("MEAS?")) for _ in range(5)]

        sim.reset()

        second_batch = [float(sim.query("MEAS?")) for _ in range(5)]

        assert first_batch == second_batch


# ---------------------------------------------------------------------------
# Convenience methods tests
# ---------------------------------------------------------------------------


class TestConvenienceMethods:
    """Tests for simulate_measurement and simulate_measurements."""

    def test_simulate_measurement_applies_noise(self) -> None:
        """simulate_measurement should apply noise to a true value."""
        driver = _TestSimDriver()
        driver.connect("SIM")
        config = NoiseConfig(model=NoiseModel.GAUSSIAN, noise_sigma=0.5, seed=42)
        sim = InstrumentSimulator(driver, config=config)

        result = sim.simulate_measurement(10.0)

        # Should be close to 10.0 but not exactly
        assert abs(result - 10.0) < 2.0
        assert result != 10.0  # Noise was added

    def test_simulate_measurement_dmm_overflow(self) -> None:
        """simulate_measurement should apply DMM overflow for DMM type."""
        driver = _TestSimDriver()
        driver.connect("SIM")
        config = NoiseConfig(
            model=NoiseModel.NONE,
            dmm_overflow_threshold=10.0,
            dmm_overflow_behavior="saturate",
            seed=42,
        )
        sim = InstrumentSimulator(driver, instrument_type="DMM", config=config)

        result = sim.simulate_measurement(15.0)
        assert result == 10.0

    def test_simulate_measurements_batch(self) -> None:
        """simulate_measurements should process a list of values."""
        driver = _TestSimDriver()
        driver.connect("SIM")
        config = NoiseConfig(model=NoiseModel.GAUSSIAN, noise_sigma=0.1, seed=42)
        sim = InstrumentSimulator(driver, config=config)

        true_values = [1.0, 2.0, 3.0, 4.0, 5.0]
        results = sim.simulate_measurements(true_values)

        assert len(results) == 5
        # Each result should be near its true value
        for true, sim_val in zip(true_values, results, strict=True):
            assert abs(sim_val - true) < 0.5

    def test_get_statistics(self) -> None:
        """get_statistics should return diagnostic info."""
        driver = _TestSimDriver()
        driver.connect("SIM")
        config = NoiseConfig(model=NoiseModel.FULL, noise_sigma=0.1, seed=42)
        sim = InstrumentSimulator(driver, instrument_type="DMM", config=config)

        sim.query("MEAS?")
        sim.query("MEAS?")
        sim.query("MEAS?")

        stats = sim.get_statistics()
        assert stats["instrument_type"] == "DMM"
        assert stats["noise_model"] == "FULL"
        assert stats["noise_sigma"] == 0.1
        assert stats["seed"] == 42
        assert stats["query_count"] == 3
        assert "elapsed_time_s" in stats

    def test_repr(self) -> None:
        """__repr__ should produce a concise representation."""
        driver = _TestSimDriver()
        driver.connect("SIM")
        sim = InstrumentSimulator(driver, instrument_type="SCOPE")

        repr_str = repr(sim)
        assert "InstrumentSimulator" in repr_str
        assert "SCOPE" in repr_str


# ---------------------------------------------------------------------------
# Delegation tests
# ---------------------------------------------------------------------------


class TestDelegation:
    """Tests that the simulator delegates to the underlying driver."""

    def test_write_delegates(self) -> None:
        """write() should delegate to the driver."""
        driver = _TestSimDriver()
        driver.connect("SIM")
        sim = InstrumentSimulator(driver)

        # Should not raise
        sim.write("CONF:VOLT:DC 10")

    def test_read_delegates(self) -> None:
        """read() should delegate to the driver."""
        driver = _TestSimDriver(value=7.0)
        driver.connect("SIM")
        sim = InstrumentSimulator(driver)

        result = sim.read()
        assert "7.0" in result

    def test_connect_disconnect(self) -> None:
        """connect/disconnect should delegate to the driver."""
        driver = _TestSimDriver()
        sim = InstrumentSimulator(driver)

        assert not sim.is_connected
        sim.connect("SIM")
        assert sim.is_connected
        sim.disconnect()
        assert not sim.is_connected

    def test_driver_property(self) -> None:
        """driver property should return the underlying driver."""
        driver = _TestSimDriver()
        sim = InstrumentSimulator(driver)

        assert sim.driver is driver
