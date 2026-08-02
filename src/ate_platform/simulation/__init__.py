"""Simulation module for ATE Platform.

Three-tier simulation verification system:
- Tier 1: InstrumentSimulator - noise-injecting instrument simulation
- Tier 2: DryRunScheduler - full scheduling graph traversal without real executors
- Tier 3: FullChainSimulator - end-to-end noise injection combining both tiers

Design principles:
- No silent fallback or degradation - errors surface directly
- No threads for user script execution (multiprocessing isolation preserved)
- Configurable random seeds for reproducible test runs
- Dataclass/Pydantic for all data structures (no bare dicts)
"""

from .dry_run_scheduler import DryRunResult, DryRunScheduler, StepDecision
from .full_chain_simulator import FullChainResult, FullChainSimulator
from .instrument_simulator import InstrumentSimulator, NoiseConfig, NoiseModel

__all__ = [
    "DryRunResult",
    "DryRunScheduler",
    "FullChainResult",
    "FullChainSimulator",
    "InstrumentSimulator",
    "NoiseConfig",
    "NoiseModel",
    "StepDecision",
]
