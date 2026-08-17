"""Tier 3: Full-Chain Simulator - end-to-end noise injection.

Combines InstrumentSimulator (Tier 1) and DryRunScheduler (Tier 2) to
provide end-to-end simulation: the dry-run scheduler traverses the plan
graph while instrument simulators inject realistic measurement noise
into the measurement values that would flow through the system.

The FullChainSimulator:
1. Creates InstrumentSimulator instances for each instrument in the plan
2. Runs the DryRunScheduler to traverse the scheduling graph
3. For each step that would produce measurements, injects noise via
   the InstrumentSimulator and records the simulated measurement
4. Aggregates everything into a FullChainResult

This is the highest fidelity simulation tier - it verifies both the
scheduling logic AND the measurement noise impact in a single pass.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from shared.dsl import YamlLoop, YamlPlan, YamlStep

from .dry_run_scheduler import DryRunResult, DryRunScheduler
from .instrument_simulator import InstrumentSimulator, NoiseConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SimulatedMeasurement:
    """A single simulated measurement with noise applied.

    Attributes:
        step_id: The step that produced this measurement.
        instrument_type: Type of instrument (DMM, SCOPE, etc.).
        true_value: The ideal measurement value before noise.
        simulated_value: The value after noise injection.
        noise_applied: Description of the noise model applied.
        timestamp: When the measurement was simulated.
    """

    step_id: str
    instrument_type: str
    true_value: float
    simulated_value: float
    noise_applied: str
    timestamp: float = field(default_factory=time.monotonic)

    @property
    def noise_error(self) -> float:
        """The difference between simulated and true value (measurement error)."""
        return self.simulated_value - self.true_value


@dataclass(slots=True)
class FullChainResult:
    """Aggregate result of a full-chain simulation.

    Attributes:
        dry_run_result: The Tier 2 DryRunResult from the scheduling traversal.
        measurements: List of simulated measurements with noise applied.
        instrument_stats: Per-instrument statistics from the simulators.
        total_duration_s: Total wall-clock duration of the full-chain simulation.
    """

    dry_run_result: DryRunResult
    measurements: list[SimulatedMeasurement]
    instrument_stats: list[dict[str, float | int | str]]
    total_duration_s: float

    @property
    def all_passed(self) -> bool:
        """True if the dry run passed AND no measurement overflowed."""
        return self.dry_run_result.all_passed

    @property
    def summary(self) -> str:
        """Human-readable one-line summary."""
        return (
            f"FullChain: {self.dry_run_result.summary} | "
            f"{len(self.measurements)} measurements simulated | "
            f"{self.total_duration_s:.3f}s total"
        )


class FullChainSimulator:
    """End-to-end simulation combining instrument noise and scheduling.

    The FullChainSimulator orchestrates:
    1. Instrument simulation setup (one InstrumentSimulator per instrument type)
    2. Dry-run scheduling traversal via DryRunScheduler
    3. Measurement noise injection for each step's simulated output

    Instrument assignment:
        Steps declare their instrument type via the `script` field or
        `params` dict. The simulator maps instrument types to simulators:
        - Scripts containing "dmm" -> DMM simulator
        - Scripts containing "scope" -> SCOPE simulator
        - Scripts containing "psu" -> PSU simulator
        - Other -> GENERIC simulator

    Measurement generation:
        For each step that passes the dry-run checks, the simulator
        generates a representative measurement using the step's instrument
        simulator. The true value defaults to a nominal value (1.0) or
        can be provided via the step's params["expected_value"].

    Example:
        >>> plan = YamlPlan(name="test", version="1.0", steps=[
        ...     YamlStep(id="dmm1", script="dmm_measure.py",
        ...             params={"expected_value": 3.3}),
        ... ])
        >>> sim = FullChainSimulator()
        >>> result = sim.run(plan)
        >>> print(result.summary)
        FullChain: DryRun('test'): 1 pass, ... | 1 measurements simulated | ...
    """

    # Default true value when step params don't specify expected_value
    DEFAULT_TRUE_VALUE: float = 1.0

    # Mapping from script name keywords to instrument types
    INSTRUMENT_KEYWORDS: dict[str, str] = {
        "dmm": "DMM",
        "multimeter": "DMM",
        "scope": "SCOPE",
        "oscilloscope": "SCOPE",
        "psu": "PSU",
        "power": "PSU",
    }

    def __init__(
        self,
        noise_config: NoiseConfig | None = None,
        dry_run_scheduler: DryRunScheduler | None = None,
        fault_config: list[dict[str, Any]] | None = None,
    ) -> None:
        """Initialize the full-chain simulator.

        Args:
            noise_config: Noise configuration for all instrument simulators.
                Defaults to NoiseConfig() (Gaussian, sigma=0.001, seed=42).
                Each instrument gets its own copy of this config so they
                can be reset independently.
            dry_run_scheduler: Optional pre-configured DryRunScheduler.
                Defaults to a fresh instance.
            fault_config: 故障注入规则列表（§7.7.2 fault_injection 段）。
                None/空则不启用故障注入。
        """
        self._noise_config: NoiseConfig = noise_config or NoiseConfig()
        self._dry_run_scheduler: DryRunScheduler = (
            dry_run_scheduler or DryRunScheduler()
        )
        self._instrument_simulators: dict[str, InstrumentSimulator] = {}
        self._fault_config: list[dict[str, Any]] | None = fault_config

    @property
    def dry_run_scheduler(self) -> DryRunScheduler:
        """Access the underlying DryRunScheduler."""
        return self._dry_run_scheduler

    @property
    def instrument_simulators(self) -> dict[str, InstrumentSimulator]:
        """Access the instrument simulators dict (keyed by instrument type)."""
        return self._instrument_simulators

    def run(
        self,
        plan: YamlPlan,
        assume_pass: bool = True,
    ) -> FullChainResult:
        """Run a full-chain simulation on the given plan.

        Args:
            plan: The YamlPlan to simulate.
            assume_pass: Whether to assume steps pass after scheduling checks.

        Returns:
            FullChainResult with dry-run decisions and simulated measurements.
        """
        start_time = time.monotonic()

        # Phase 1: Set up instrument simulators for all instrument types
        self._setup_instrument_simulators(plan)

        # Phase 2: Run the dry-run scheduling traversal
        dry_run_result = self._dry_run_scheduler.dry_run(plan, assume_pass=assume_pass)

        # Phase 3: Generate simulated measurements for passing steps
        measurements = self._generate_measurements(plan, dry_run_result)

        # Phase 4: Collect instrument statistics
        instrument_stats = [
            sim.get_statistics()
            for sim in self._instrument_simulators.values()
        ]

        total_duration = time.monotonic() - start_time

        return FullChainResult(
            dry_run_result=dry_run_result,
            measurements=measurements,
            instrument_stats=instrument_stats,
            total_duration_s=total_duration,
        )

    def reset(self) -> None:
        """Reset all instrument simulators to their initial state.

        Restores RNG seeds and clears query counters. Does not affect
        the DryRunScheduler's state.
        """
        for sim in self._instrument_simulators.values():
            sim.reset()

    # ------------------------------------------------------------------
    # Instrument simulator setup
    # ------------------------------------------------------------------

    def _setup_instrument_simulators(self, plan: YamlPlan) -> None:
        """Create InstrumentSimulator instances for each instrument type in the plan.

        Scans all steps in the plan for instrument type keywords and creates
        a simulator for each unique type found. Simulators are cached so
        repeated runs reuse the same instances.

        Args:
            plan: The plan to scan for instrument types.
        """
        # Collect all instrument types needed
        needed_types: set[str] = set()

        def scan_items(items: list[YamlStep | YamlLoop]) -> None:
            for item in items:
                if hasattr(item, "script"):
                    instrument_type = self._infer_instrument_type(item.script)
                    needed_types.add(instrument_type)
                if hasattr(item, "steps"):
                    scan_items(item.steps)

        scan_items(plan.steps)

        # Always include GENERIC as fallback
        needed_types.add("GENERIC")

        # Create simulators for any types not already created
        for inst_type in needed_types:
            if inst_type not in self._instrument_simulators:
                self._instrument_simulators[inst_type] = self._create_simulator(inst_type)

    def _create_simulator(self, instrument_type: str) -> InstrumentSimulator:
        """Create an InstrumentSimulator for the given instrument type.

        Creates a minimal BaseDriver subclass in SIM mode that returns
        a configurable base value. The InstrumentSimulator wraps it and
        applies the noise model.

        Args:
            instrument_type: The instrument type string (DMM, SCOPE, etc.).

        Returns:
            A configured InstrumentSimulator instance.
        """
        from ate_platform.drivers.base_hal import BaseDriver

        # Create a minimal SIM-mode driver that returns a base value.
        # We can't instantiate BaseDriver directly (it's abstract), but
        # we can create a minimal concrete subclass that bypasses the
        # full __init__ (which requires PyVISA ResourceManager).
        class _SimDriver(BaseDriver):
            """Minimal SIM-mode driver for full-chain simulation."""

            def __init__(self) -> None:
                # Bypass BaseDriver.__init__ to avoid creating a real
                # PyVISA ResourceManager (mirrors _MockBaseDriver pattern).
                self._mode = "SIM"
                self._noise_sigma: float = 0.0
                self._instrument = None
                self._resource_manager = None  # 避免 property 引用缺失
                self._address: str = ""
                self._lock = __import__("threading").Lock()
                self._connected: bool = False
                self.command_log: list[str] = []

            def query(self, command: str, delay: float | None = None) -> str:  # noqa: PLW0221
                if command.strip().upper() == "*IDN?":
                    return f"SIM_{instrument_type}"
                # Return a nominal value; InstrumentSimulator applies noise
                return "1.0"

            def connect(self, address: str) -> None:
                # SIM 模式：跳过真实 pyvisa open_resource
                self._address = address
                self._connected = True

            def disconnect(self) -> None:
                self._connected = False

            @property
            def is_connected(self) -> bool:
                return self._connected

        driver = _SimDriver()
        driver.connect("SIM")

        # §7.7 故障注入：为每个模拟器构建独立注入器（多轮重置互不干扰）
        injector = None
        if self._fault_config:
            from ate_platform.simulation.fault_injector import FaultInjector

            injector = FaultInjector()
            injector.load(self._fault_config)

        return InstrumentSimulator(
            driver=driver,
            instrument_type=instrument_type,
            config=self._noise_config,
            injector=injector,
        )

    @classmethod
    def _infer_instrument_type(cls, script_name: str) -> str:
        """Infer instrument type from a script name.

        Args:
            script_name: The script path/name from a YamlStep.

        Returns:
            The instrument type string (DMM, SCOPE, PSU, or GENERIC).
        """
        script_lower = script_name.lower()
        for keyword, inst_type in cls.INSTRUMENT_KEYWORDS.items():
            if keyword in script_lower:
                return inst_type
        return "GENERIC"

    # ------------------------------------------------------------------
    # Measurement generation
    # ------------------------------------------------------------------

    def _generate_measurements(
        self,
        plan: YamlPlan,
        dry_run_result: DryRunResult,
    ) -> list[SimulatedMeasurement]:
        """Generate simulated measurements for steps that passed dry-run.

        For each step with a PASS decision, determines the instrument type,
        extracts the expected true value from step params, and applies
        noise via the corresponding InstrumentSimulator.

        Args:
            plan: The plan that was dry-run.
            dry_run_result: The dry-run result with per-step decisions.

        Returns:
            List of SimulatedMeasurement records.
        """
        measurements: list[SimulatedMeasurement] = []
        pass_decisions = {
            d.step_id: d for d in dry_run_result.decisions if d.decision == "PASS"
        }

        def process_items(items: list[YamlStep | YamlLoop]) -> None:
            for item in items:
                if isinstance(item, YamlStep):
                    # Check for exact match (non-loop step)
                    if item.id in pass_decisions:
                        measurements.append(
                            self._simulate_step_measurement(item, item.id)
                        )
                    else:
                        # Check for loop-iteration matches: step_id appears as
                        # a suffix in pass_decisions keys (e.g. "loop1#0#measure"
                        # matches step "measure")
                        for decision_step_id in pass_decisions:
                            if decision_step_id.endswith(f"#{item.id}"):
                                measurements.append(
                                    self._simulate_step_measurement(
                                        item, decision_step_id,
                                    )
                                )
                if isinstance(item, YamlLoop):
                    process_items(item.steps)

        process_items(plan.steps)
        return measurements

    def _simulate_step_measurement(
        self, step: object, step_id: str | None = None,
    ) -> SimulatedMeasurement:
        """Generate a single simulated measurement for a step.

        Args:
            step: The YamlStep object (typed as object to avoid circular import).
            step_id: Optional step_id override. When provided, uses this ID
                instead of the step's id attribute (used for loop iterations
                where the decision ID differs from the plan step ID).

        Returns:
            A SimulatedMeasurement with noise applied.
        """
        script_name: str = getattr(step, "script", "unknown")
        effective_step_id: str = step_id if step_id is not None else getattr(step, "id", "unknown")
        params: dict[str, object] = getattr(step, "params", {}) or {}

        instrument_type = self._infer_instrument_type(script_name)
        simulator = self._instrument_simulators.get(instrument_type)
        if simulator is None:
            simulator = self._instrument_simulators.get("GENERIC")
            if simulator is None:
                # Should not happen - GENERIC is always created
                msg = "No GENERIC simulator available"
                raise RuntimeError(msg)
            instrument_type = "GENERIC"

        # Get the true value from params or use default
        raw_value = params.get("expected_value", self.DEFAULT_TRUE_VALUE)
        if isinstance(raw_value, bool):
            true_value: float = self.DEFAULT_TRUE_VALUE
        elif isinstance(raw_value, (int, float)):
            true_value = float(raw_value)
        else:
            true_value = self.DEFAULT_TRUE_VALUE

        # Apply noise via the instrument simulator
        simulated_value = simulator.simulate_measurement(true_value)

        return SimulatedMeasurement(
            step_id=effective_step_id,
            instrument_type=instrument_type,
            true_value=true_value,
            simulated_value=simulated_value,
            noise_applied=simulator.config.model.value,
        )
