"""Tests for Tier 3: FullChainSimulator.

Covers:
- End-to-end simulation combining instrument noise + scheduling
- Instrument type inference from script names
- Simulated measurement generation with noise
- FullChainResult aggregation
- Instrument statistics collection
- Reset and reproducibility
"""

from __future__ import annotations

import pytest

from ate_platform.simulation.full_chain_simulator import (
    FullChainResult,
    FullChainSimulator,
)
from ate_platform.simulation.instrument_simulator import NoiseConfig, NoiseModel
from shared.dsl import LoopType, YamlLoop, YamlPlan, YamlStep

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_step(
    step_id: str,
    script: str = "test.py",
    params: dict | None = None,
    preconditions: list[str] | None = None,
    skip_if: str | None = None,
) -> YamlStep:
    """Create a YamlStep with sensible defaults."""
    return YamlStep(
        id=step_id,
        script=script,
        params=params or {},
        preconditions=preconditions or [],
        skip_if=skip_if,
    )


def _make_plan(steps: list[YamlStep | YamlLoop]) -> YamlPlan:
    """Create a YamlPlan."""
    return YamlPlan(name="chain_test", version="1.0", steps=steps)


# ---------------------------------------------------------------------------
# Basic run tests
# ---------------------------------------------------------------------------


class TestFullChainBasic:
    """Tests for basic FullChainSimulator runs."""

    def test_run_single_step(self) -> None:
        """A single step should produce one measurement."""
        plan = _make_plan([
            _make_step("s1", script="dmm_measure.py", params={"expected_value": 3.3}),
        ])
        sim = FullChainSimulator()
        result = sim.run(plan)

        assert result.dry_run_result.passed == 1
        assert len(result.measurements) == 1
        assert result.measurements[0].step_id == "s1"
        assert result.measurements[0].true_value == 3.3

    def test_run_multiple_steps(self) -> None:
        """Multiple steps should produce multiple measurements."""
        plan = _make_plan([
            _make_step("dmm1", script="dmm_measure.py", params={"expected_value": 5.0}),
            _make_step("scope1", script="scope_capture.py", params={"expected_value": 0.5}),
        ])
        sim = FullChainSimulator()
        result = sim.run(plan)

        assert len(result.measurements) == 2
        assert result.measurements[0].instrument_type == "DMM"
        assert result.measurements[1].instrument_type == "SCOPE"

    def test_run_empty_plan(self) -> None:
        """An empty plan should produce zero measurements."""
        plan = _make_plan([])
        sim = FullChainSimulator()
        result = sim.run(plan)

        assert len(result.measurements) == 0
        assert result.dry_run_result.total_steps == 0

    def test_run_returns_full_chain_result(self) -> None:
        """run() should return a FullChainResult instance."""
        plan = _make_plan([_make_step("s1")])
        sim = FullChainSimulator()
        result = sim.run(plan)

        assert isinstance(result, FullChainResult)
        assert result.dry_run_result.plan_name == "chain_test"


# ---------------------------------------------------------------------------
# Instrument type inference tests
# ---------------------------------------------------------------------------


class TestInstrumentInference:
    """Tests for instrument type inference from script names."""

    @pytest.mark.parametrize("script,expected_type", [
        ("dmm_measure.py", "DMM"),
        ("multimeter_check.py", "DMM"),
        ("scope_capture.py", "SCOPE"),
        ("oscilloscope_test.py", "SCOPE"),
        ("psu_power_on.py", "PSU"),
        ("power_supply.py", "PSU"),
        ("generic_test.py", "GENERIC"),
        ("calibration.py", "GENERIC"),
    ])
    def test_infer_instrument_type(self, script: str, expected_type: str) -> None:
        """Should correctly infer instrument type from script name."""
        inferred = FullChainSimulator._infer_instrument_type(script)
        assert inferred == expected_type

    def test_simulators_created_for_each_type(self) -> None:
        """Should create a simulator for each instrument type in the plan."""
        plan = _make_plan([
            _make_step("dmm1", script="dmm_measure.py"),
            _make_step("scope1", script="scope_capture.py"),
            _make_step("generic1", script="custom_test.py"),
        ])
        sim = FullChainSimulator()
        sim.run(plan)

        simulators = sim.instrument_simulators
        assert "DMM" in simulators
        assert "SCOPE" in simulators
        assert "GENERIC" in simulators

    def test_generic_always_created(self) -> None:
        """GENERIC simulator should always be created as fallback."""
        plan = _make_plan([_make_step("s1", script="custom.py")])
        sim = FullChainSimulator()
        sim.run(plan)

        assert "GENERIC" in sim.instrument_simulators


# ---------------------------------------------------------------------------
# Measurement generation tests
# ---------------------------------------------------------------------------


class TestMeasurementGeneration:
    """Tests for simulated measurement generation."""

    def test_measurement_has_noise_applied(self) -> None:
        """Measurements should differ from true values (noise added)."""
        config = NoiseConfig(model=NoiseModel.GAUSSIAN, noise_sigma=1.0, seed=42)
        plan = _make_plan([
            _make_step("s1", script="dmm_measure.py", params={"expected_value": 5.0}),
        ])
        sim = FullChainSimulator(noise_config=config)
        result = sim.run(plan)

        measurement = result.measurements[0]
        # With sigma=1.0, the value should differ from 5.0
        assert measurement.simulated_value != 5.0
        assert measurement.noise_error != 0.0

    def test_measurement_uses_default_value(self) -> None:
        """Steps without expected_value should use DEFAULT_TRUE_VALUE."""
        plan = _make_plan([
            _make_step("s1", script="dmm_measure.py"),  # No params
        ])
        sim = FullChainSimulator(
            noise_config=NoiseConfig(model=NoiseModel.NONE, seed=42),
        )
        result = sim.run(plan)

        measurement = result.measurements[0]
        assert measurement.true_value == FullChainSimulator.DEFAULT_TRUE_VALUE

    def test_measurement_fields_populated(self) -> None:
        """SimulatedMeasurement should have all fields populated."""
        plan = _make_plan([
            _make_step("s1", script="dmm_measure.py", params={"expected_value": 3.3}),
        ])
        sim = FullChainSimulator()
        result = sim.run(plan)

        m = result.measurements[0]
        assert m.step_id == "s1"
        assert m.instrument_type == "DMM"
        assert m.true_value == 3.3
        assert isinstance(m.simulated_value, float)
        assert isinstance(m.noise_applied, str)
        assert m.timestamp > 0

    def test_noise_error_property(self) -> None:
        """noise_error should be simulated - true."""
        plan = _make_plan([
            _make_step("s1", script="dmm_measure.py", params={"expected_value": 5.0}),
        ])
        config = NoiseConfig(model=NoiseModel.GAUSSIAN, noise_sigma=0.5, seed=42)
        sim = FullChainSimulator(noise_config=config)
        result = sim.run(plan)

        m = result.measurements[0]
        assert m.noise_error == m.simulated_value - m.true_value

    def test_skipped_steps_produce_no_measurement(self) -> None:
        """Skipped steps should not produce measurements."""
        plan = _make_plan([
            _make_step("s1", script="dmm_measure.py", skip_if="True"),
            _make_step("s2", script="dmm_measure.py"),
        ])
        sim = FullChainSimulator()
        result = sim.run(plan)

        # Only s2 (passing) should have a measurement
        assert len(result.measurements) == 1
        assert result.measurements[0].step_id == "s2"


# ---------------------------------------------------------------------------
# Result aggregation tests
# ---------------------------------------------------------------------------


class TestFullChainResult:
    """Tests for FullChainResult aggregation."""

    def test_summary_string(self) -> None:
        """summary should contain dry-run and measurement info."""
        plan = _make_plan([_make_step("s1", script="dmm_measure.py")])
        sim = FullChainSimulator()
        result = sim.run(plan)

        summary = result.summary
        assert "FullChain" in summary
        assert "measurements" in summary

    def test_all_passed_true_when_no_failures(self) -> None:
        """all_passed should be True when dry run passes."""
        plan = _make_plan([_make_step("s1", script="dmm_measure.py")])
        sim = FullChainSimulator()
        result = sim.run(plan)

        assert result.all_passed is True

    def test_all_passed_false_when_blocked(self) -> None:
        """all_passed should be False when dry run has blocked steps."""
        plan = _make_plan([
            _make_step("s1", script="dmm_measure.py", skip_if="True"),
            _make_step("s2", script="dmm_measure.py", preconditions=["s1"]),
        ])
        sim = FullChainSimulator()
        result = sim.run(plan)

        assert result.all_passed is False

    def test_instrument_stats_populated(self) -> None:
        """instrument_stats should contain stats for each simulator."""
        plan = _make_plan([
            _make_step("dmm1", script="dmm_measure.py"),
            _make_step("scope1", script="scope_capture.py"),
        ])
        sim = FullChainSimulator()
        result = sim.run(plan)

        assert len(result.instrument_stats) >= 2
        types_in_stats = [s["instrument_type"] for s in result.instrument_stats]
        assert "DMM" in types_in_stats
        assert "SCOPE" in types_in_stats

    def test_total_duration_positive(self) -> None:
        """total_duration_s should be a non-negative number."""
        plan = _make_plan([_make_step("s1", script="dmm_measure.py")])
        sim = FullChainSimulator()
        result = sim.run(plan)

        assert result.total_duration_s >= 0.0


# ---------------------------------------------------------------------------
# Noise configuration tests
# ---------------------------------------------------------------------------


class TestNoiseConfiguration:
    """Tests for noise configuration propagation."""

    def test_custom_config_applied_to_all_simulators(self) -> None:
        """Custom noise config should be used by all instrument simulators."""
        config = NoiseConfig(
            model=NoiseModel.GAUSSIAN_BIAS,
            noise_sigma=0.01,
            bias=2.0,
            seed=123,
        )
        plan = _make_plan([
            _make_step("dmm1", script="dmm_measure.py", params={"expected_value": 1.0}),
            _make_step("scope1", script="scope_capture.py", params={"expected_value": 1.0}),
        ])
        sim = FullChainSimulator(noise_config=config)
        result = sim.run(plan)

        # Both measurements should have bias applied (mean ~ 1.0 + 2.0 = 3.0)
        for m in result.measurements:
            # With bias=2.0, simulated should be near 3.0
            assert abs(m.simulated_value - 3.0) < 1.0

    def test_none_model_produces_exact_values(self) -> None:
        """NONE model should produce measurements equal to true values."""
        config = NoiseConfig(model=NoiseModel.NONE, seed=42)
        plan = _make_plan([
            _make_step("s1", script="dmm_measure.py", params={"expected_value": 5.0}),
        ])
        sim = FullChainSimulator(noise_config=config)
        result = sim.run(plan)

        assert result.measurements[0].simulated_value == 5.0
        assert result.measurements[0].noise_error == 0.0


# ---------------------------------------------------------------------------
# Reproducibility tests
# ---------------------------------------------------------------------------


class TestFullChainReproducibility:
    """Tests for reproducibility of full-chain simulation."""

    def test_same_config_produces_same_measurements(self) -> None:
        """Same noise config should produce identical measurements."""
        config = NoiseConfig(model=NoiseModel.GAUSSIAN, noise_sigma=0.5, seed=42)

        plan = _make_plan([
            _make_step("s1", script="dmm_measure.py", params={"expected_value": 5.0}),
        ])

        sim1 = FullChainSimulator(noise_config=config)
        result1 = sim1.run(plan)

        sim2 = FullChainSimulator(noise_config=config)
        result2 = sim2.run(plan)

        assert result1.measurements[0].simulated_value == result2.measurements[0].simulated_value

    def test_reset_restores_reproducibility(self) -> None:
        """reset() should restore simulators to initial state."""
        config = NoiseConfig(model=NoiseModel.GAUSSIAN, noise_sigma=0.5, seed=42)
        plan = _make_plan([
            _make_step("s1", script="dmm_measure.py", params={"expected_value": 5.0}),
        ])

        sim = FullChainSimulator(noise_config=config)
        result1 = sim.run(plan)

        sim.reset()
        result2 = sim.run(plan)

        assert result1.measurements[0].simulated_value == result2.measurements[0].simulated_value


# ---------------------------------------------------------------------------
# Integration with DryRunScheduler tests
# ---------------------------------------------------------------------------


class TestDryRunIntegration:
    """Tests for integration with the DryRunScheduler."""

    def test_custom_dry_run_scheduler(self) -> None:
        """Should accept a pre-configured DryRunScheduler."""
        from ate_platform.scheduler.variable_space import VariableSpace
        from ate_platform.simulation.dry_run_scheduler import DryRunScheduler

        vs = VariableSpace()
        vs.set("scope.test_var", True)

        dry_run = DryRunScheduler(variable_space=vs)
        sim = FullChainSimulator(dry_run_scheduler=dry_run)

        assert sim.dry_run_scheduler is dry_run

    def test_loop_steps_generate_measurements(self) -> None:
        """Steps inside loops should also generate measurements."""
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.FOR,
            count=3,
            steps=[
                _make_step("measure", script="dmm_measure.py", params={"expected_value": 5.0}),
            ],
        )
        plan = _make_plan([loop])
        sim = FullChainSimulator()
        result = sim.run(plan)

        assert len(result.measurements) == 3
        # Each measurement should be from a loop iteration
        for m in result.measurements:
            assert "loop1" in m.step_id
            assert m.instrument_type == "DMM"

    def test_plan_with_dependencies(self) -> None:
        """A plan with step dependencies should traverse correctly."""
        plan = _make_plan([
            _make_step("setup", script="psu_power_on.py", params={"expected_value": 12.0}),
            _make_step("measure", script="dmm_measure.py", params={"expected_value": 3.3},
                       preconditions=["setup"]),
        ])
        sim = FullChainSimulator()
        result = sim.run(plan)

        assert result.dry_run_result.passed == 2
        assert len(result.measurements) == 2
        assert result.measurements[0].instrument_type == "PSU"
        assert result.measurements[1].instrument_type == "DMM"


# ---------------------------------------------------------------------------
# Statistics tests
# ---------------------------------------------------------------------------


class TestStatistics:
    """Tests for instrument statistics collection."""

    def test_stats_contain_query_counts(self) -> None:
        """Instrument stats should show query counts from simulators."""
        plan = _make_plan([
            _make_step("dmm1", script="dmm_measure.py"),
            _make_step("dmm2", script="dmm_measure.py"),
        ])
        sim = FullChainSimulator()
        result = sim.run(plan)

        dmm_stats = [s for s in result.instrument_stats if s["instrument_type"] == "DMM"]
        assert len(dmm_stats) == 1
        # DMM simulator was used for 2 measurements
        assert dmm_stats[0]["query_count"] == 2

    def test_stats_contain_noise_config(self) -> None:
        """Instrument stats should include noise configuration."""
        config = NoiseConfig(model=NoiseModel.FULL, noise_sigma=0.1, seed=99)
        plan = _make_plan([_make_step("s1", script="dmm_measure.py")])
        sim = FullChainSimulator(noise_config=config)
        result = sim.run(plan)

        dmm_stats = [s for s in result.instrument_stats if s["instrument_type"] == "DMM"][0]
        assert dmm_stats["noise_model"] == "FULL"
        assert dmm_stats["noise_sigma"] == 0.1
        assert dmm_stats["seed"] == 99
