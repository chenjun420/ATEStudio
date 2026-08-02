"""Tier 1: Instrument Simulator with physics-aware noise models.

Provides realistic instrument measurement simulation by injecting:
- Gaussian noise (random measurement jitter)
- Linear drift (time-dependent offset from warmup/temperature)
- Constant bias (systematic calibration offset)
- DMM range overflow (out-of-range measurements saturate or error)
- Scope noise floor (minimum detectable signal threshold)

The simulator wraps a BaseDriver (HAL) in SIM mode and applies noise
transforms to raw query responses. It does NOT replace real drivers -
it enhances SIM-mode drivers with realistic measurement imperfections.

Reproducibility:
    All randomness is driven by a configurable seed. Same seed + same
    inputs produce identical outputs across runs.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from ate_platform.drivers.base_hal import BaseDriver


class NoiseModel(Enum):
    """Noise model type selector for instrument simulation.

    Attributes:
        GAUSSIAN: Pure Gaussian noise (sigma from NoiseConfig.noise_sigma)
        GAUSSIAN_DRIFT: Gaussian noise + linear time drift
        GAUSSIAN_BIAS: Gaussian noise + constant bias offset
        FULL: Gaussian noise + drift + bias (most realistic)
        NONE: No noise injection (pass-through)
    """

    GAUSSIAN = "GAUSSIAN"
    GAUSSIAN_DRIFT = "GAUSSIAN_DRIFT"
    GAUSSIAN_BIAS = "GAUSSIAN_BIAS"
    FULL = "FULL"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class NoiseConfig:
    """Configuration for instrument noise simulation.

    Attributes:
        model: Noise model type to apply.
        noise_sigma: Gaussian noise standard deviation (same units as measurement).
        drift_rate: Linear drift per second (units/second). Positive = increasing.
        bias: Constant systematic offset added to every measurement.
        seed: Random seed for reproducibility. None = non-deterministic.
        dmm_overflow_threshold: Above this absolute value, DMM reports overflow.
            None disables overflow simulation.
        dmm_overflow_behavior: "saturate" clamps to threshold, "error" raises.
        scope_noise_floor: Minimum detectable signal magnitude. Values below
            this are replaced with a noise-floor-level random value. None disables.
    """

    model: NoiseModel = NoiseModel.GAUSSIAN
    noise_sigma: float = 0.001
    drift_rate: float = 0.0
    bias: float = 0.0
    seed: int | None = 42
    dmm_overflow_threshold: float | None = None
    dmm_overflow_behavior: Literal["saturate", "error"] = "saturate"
    scope_noise_floor: float | None = None


@dataclass(slots=True)
class _InstrumentState:
    """Mutable per-instrument runtime state for noise simulation.

    Attributes:
        rng: Random instance seeded from NoiseConfig.
        start_time: Monotonic timestamp when simulation started (for drift).
        query_count: Number of queries made (for diagnostics).
    """

    rng: random.Random
    start_time: float
    query_count: int = 0


class InstrumentSimulator:
    """Wraps a SIM-mode BaseDriver with physics-aware noise injection.

    The simulator does NOT replace the driver - it decorates it. Every
    query() call flows through the driver's SIM-mode response generation,
    then the simulator applies the configured noise model.

    Instrument type is inferred from the driver class name or configured
    explicitly. Each type gets specific fault behaviors:
    - DMM: range overflow simulation (saturate or error)
    - Scope: noise floor simulation (sub-threshold signals lost in noise)
    - PSU/Generic: Gaussian/drift/bias only

    Thread Safety:
        The simulator is NOT thread-safe. Each instrument should have its
        own InstrumentSimulator instance. The underlying BaseDriver has
        its own threading.Lock.

    Example:
        >>> from ate_platform.drivers.examples.dmm import DMMHALDriver
        >>> driver = DMMHALDriver(mode="SIM", noise_sigma=0.0)
        >>> driver.connect("SIM")
        >>> config = NoiseConfig(model=NoiseModel.FULL, noise_sigma=0.01,
        ...                      drift_rate=0.001, bias=0.05)
        >>> sim = InstrumentSimulator(driver, instrument_type="DMM", config=config)
        >>> value = sim.query("MEAS:VOLT:DC?")
    """

    # Known instrument types for specialized fault simulation
    SUPPORTED_TYPES: tuple[str, ...] = ("DMM", "SCOPE", "PSU", "GENERIC")

    def __init__(
        self,
        driver: BaseDriver,
        instrument_type: str = "GENERIC",
        config: NoiseConfig | None = None,
    ) -> None:
        """Initialize the instrument simulator.

        Args:
            driver: A BaseDriver instance in SIM mode. The simulator reads
                query() responses and applies noise transforms.
            instrument_type: Instrument type string. Determines which fault
                models are active. Case-insensitive. One of SUPPORTED_TYPES.
            config: Noise configuration. Defaults to NoiseConfig() (Gaussian,
                sigma=0.001, seed=42) if not provided.

        Raises:
            ValueError: If instrument_type is not in SUPPORTED_TYPES.
        """
        normalized_type = instrument_type.upper().strip()
        if normalized_type not in self.SUPPORTED_TYPES:
            msg = (
                f"Unsupported instrument_type '{instrument_type}'. "
                f"Supported types: {self.SUPPORTED_TYPES}"
            )
            raise ValueError(msg)

        self._driver: BaseDriver = driver
        self._instrument_type: str = normalized_type
        self._config: NoiseConfig = config or NoiseConfig()

        # Initialize RNG with seed for reproducibility
        rng = random.Random(self._config.seed)
        self._state: _InstrumentState = _InstrumentState(
            rng=rng,
            start_time=time.monotonic(),
        )

    @property
    def driver(self) -> BaseDriver:
        """Access the underlying HAL driver."""
        return self._driver

    @property
    def instrument_type(self) -> str:
        """Get the instrument type string (uppercase)."""
        return self._instrument_type

    @property
    def config(self) -> NoiseConfig:
        """Get the noise configuration."""
        return self._config

    @property
    def query_count(self) -> int:
        """Number of queries processed since simulation start."""
        return self._state.query_count

    def reset(self) -> None:
        """Reset simulation state (RNG seed and start time).

        Restores the simulator to its initial state for reproducible
        re-runs. Does NOT affect the underlying driver's connection state.
        """
        self._state = _InstrumentState(
            rng=random.Random(self._config.seed),
            start_time=time.monotonic(),
        )

    def query(self, command: str, delay: float | None = None) -> str:
        """Execute a SCPI query with noise injection applied.

        Flow:
        1. Call the underlying driver's query() to get the raw SIM value
        2. Parse the response as a float
        3. Apply the configured noise model (Gaussian + drift + bias)
        4. Apply instrument-specific fault simulation (DMM overflow, scope floor)
        5. Return the result as a formatted string

        Non-numeric responses (e.g., "*IDN?" -> "SIM") are passed through
        without modification.

        Args:
            command: SCPI query command string.
            delay: Optional delay parameter passed to the driver.

        Returns:
            Simulated response string with noise applied.

        Raises:
            RuntimeError: If not connected to the instrument.
            OverflowError: If DMM overflow behavior is "error" and the
                measured value exceeds the overflow threshold.
        """
        self._state.query_count += 1

        # Get raw response from the SIM-mode driver
        raw_response = self._driver.query(command, delay=delay)

        # Non-numeric responses pass through unchanged
        try:
            raw_value = float(raw_response)
        except ValueError:
            return raw_response

        # Apply noise model
        noisy_value = self._apply_noise_model(raw_value)

        # Apply instrument-specific fault simulation
        if self._instrument_type == "DMM":
            noisy_value = self._apply_dmm_overflow(noisy_value, command)
        elif self._instrument_type == "SCOPE":
            noisy_value = self._apply_scope_noise_floor(noisy_value)

        # Format response matching the original precision pattern
        # Preserve scientific notation if the original used it
        if "E" in raw_response.upper():
            return f"{noisy_value:.6E}"
        return str(noisy_value)

    def write(self, command: str) -> None:
        """Delegate write to the underlying driver.

        Args:
            command: SCPI command string.
        """
        self._driver.write(command)

    def read(self) -> str:
        """Delegate read to the underlying driver."""
        return self._driver.read()

    def connect(self, address: str) -> None:
        """Delegate connect to the underlying driver.

        Args:
            address: VISA resource address (SIM accepts any).
        """
        self._driver.connect(address)

    def disconnect(self) -> None:
        """Delegate disconnect to the underlying driver."""
        self._driver.disconnect()

    @property
    def is_connected(self) -> bool:
        """Check if the underlying driver is connected."""
        return self._driver.is_connected

    # ------------------------------------------------------------------
    # Noise model application
    # ------------------------------------------------------------------

    def _apply_noise_model(self, value: float) -> float:
        """Apply the configured noise model to a raw measurement value.

        Args:
            value: The raw measurement value from the SIM driver.

        Returns:
            The value with noise, drift, and/or bias applied.
        """
        if self._config.model == NoiseModel.NONE:
            return value

        result = value

        # Gaussian noise (applied for all non-NONE models)
        if self._config.noise_sigma > 0.0:
            noise = self._state.rng.gauss(0.0, self._config.noise_sigma)
            result += noise

        # Drift (time-dependent)
        if self._config.model in (NoiseModel.GAUSSIAN_DRIFT, NoiseModel.FULL):
            if self._config.drift_rate != 0.0:
                elapsed = time.monotonic() - self._state.start_time
                result += self._config.drift_rate * elapsed

        # Bias (constant offset)
        if self._config.model in (NoiseModel.GAUSSIAN_BIAS, NoiseModel.FULL):
            result += self._config.bias

        return result

    def _apply_dmm_overflow(self, value: float, command: str) -> float:
        """Simulate DMM range overflow behavior.

        When the measured value exceeds the overflow threshold:
        - "saturate": clamp to the threshold value
        - "error": raise OverflowError

        Args:
            value: The noisy measurement value.
            command: The original SCPI command (for error messages).

        Returns:
            The value, possibly clamped to the overflow threshold.

        Raises:
            OverflowError: If overflow behavior is "error" and value exceeds threshold.
        """
        threshold = self._config.dmm_overflow_threshold
        if threshold is None:
            return value

        abs_value = abs(value)
        if abs_value <= threshold:
            return value

        # Overflow detected
        if self._config.dmm_overflow_behavior == "error":
            msg = (
                f"DMM range overflow: measured {value:.6E} exceeds "
                f"threshold {threshold:.6E} (command: {command})"
            )
            raise OverflowError(msg)

        # Saturate: clamp to +/- threshold, preserving sign
        if value > 0:
            return threshold
        return -threshold

    def _apply_scope_noise_floor(self, value: float) -> float:
        """Simulate oscilloscope noise floor behavior.

        Signals below the noise floor magnitude are indistinguishable
        from noise. The simulator replaces them with a random value
        near the noise floor level (simulating noise pickup).

        Args:
            value: The noisy measurement value.

        Returns:
            The original value if above noise floor, else a noise-floor-level value.
        """
        floor = self._config.scope_noise_floor
        if floor is None:
            return value

        abs_value = abs(value)
        if abs_value >= floor:
            return value

        # Below noise floor: replace with noise-floor-level random value
        # Sign is preserved; magnitude is randomized around the floor level
        sign = 1.0 if value >= 0 else -1.0
        # Random magnitude between 0.5x and 1.0x of the floor
        random_factor = self._state.rng.uniform(0.5, 1.0)
        return sign * floor * random_factor

    # ------------------------------------------------------------------
    # Convenience methods for direct measurement simulation
    # ------------------------------------------------------------------

    def simulate_measurement(self, true_value: float) -> float:
        """Apply the full noise pipeline to a known true value.

        This bypasses the driver entirely and applies noise + instrument
        faults directly. Useful for testing noise models in isolation
        without setting up a SIM driver.

        Args:
            true_value: The ideal measurement value.

        Returns:
            The simulated measurement with noise and faults applied.

        Raises:
            OverflowError: If DMM overflow behavior is "error" and the
                noisy value exceeds the threshold.
        """
        self._state.query_count += 1
        noisy = self._apply_noise_model(true_value)

        if self._instrument_type == "DMM":
            noisy = self._apply_dmm_overflow(noisy, "simulate_measurement")
        elif self._instrument_type == "SCOPE":
            noisy = self._apply_scope_noise_floor(noisy)

        return noisy

    def simulate_measurements(self, true_values: list[float]) -> list[float]:
        """Apply noise pipeline to a batch of known true values.

        Args:
            true_values: List of ideal measurement values.

        Returns:
            List of simulated measurements with noise applied.
        """
        return [self.simulate_measurement(v) for v in true_values]

    def get_statistics(self) -> dict[str, float | int | str]:
        """Get simulation statistics for diagnostics.

        Returns:
            Dict with instrument_type, noise_model, query_count,
            elapsed_time, and config parameters.
        """
        elapsed = time.monotonic() - self._state.start_time
        return {
            "instrument_type": self._instrument_type,
            "noise_model": self._config.model.value,
            "noise_sigma": self._config.noise_sigma,
            "drift_rate": self._config.drift_rate,
            "bias": self._config.bias,
            "seed": self._config.seed if self._config.seed is not None else -1,
            "query_count": self._state.query_count,
            "elapsed_time_s": round(elapsed, 6),
        }

    def __repr__(self) -> str:
        """Return a concise representation for diagnostics."""
        return (
            f"InstrumentSimulator(type={self._instrument_type}, "
            f"model={self._config.model.value}, "
            f"sigma={self._config.noise_sigma}, "
            f"queries={self._state.query_count})"
        )
