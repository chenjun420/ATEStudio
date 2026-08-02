"""Real OpenHTF test station for the E2E pass-flow test.

Provides a ``create_test()`` factory that returns a real ``htf.Test``
with 3 passing phases (setup, measure_voltage, measure_current) and
optional ``slow`` phase for timeout verification.

Phases:
    - ``setup_phase``: always passes, no measurements (DUT init).
    - ``measure_voltage_phase``: takes voltage measurement (5.0V,
      in [4.5, 5.5] -> PASS).
    - ``measure_current_phase``: takes current measurement (0.12A,
      in [0.0, 1.0] -> PASS).
    - ``slow_phase``: sleeps for a fixed duration to trigger timeout
      when executed with ``use_isolation=True`` and a short timeout.

Factory:
    ``create_test(slow=False, dut_id="UNKNOWN")`` -- includes the
    slow_phase only when ``slow=True``. The ``dut_id`` is consumed by
    ``OpenHTFStepExecutor`` via ``params["dut_id"]`` (passed to
    ``test_start``), not by this module directly.
"""

from __future__ import annotations

import time

import openhtf as htf

__all__ = ["create_test"]

# Fixed sleep duration for the slow phase. Must exceed the timeout
# passed to OpenHTFStepExecutor.execute() in the timeout E2E test.
_SLOW_PHASE_SLEEP_SECONDS = 3.0


@htf.PhaseOptions(name="setup")
def setup_phase(test: htf.TestApi) -> None:
    """Initialize the DUT before measurement."""
    test.logger.info("E2E setup phase complete")


@htf.PhaseOptions(name="measure_voltage")
@htf.measures(htf.Measurement("voltage").in_range(4.5, 5.5).with_units("V"))
def measure_voltage_phase(test: htf.TestApi) -> None:
    """Measure power rail voltage (in-range -> PASS)."""
    test.measurements["voltage"] = 5.0


@htf.PhaseOptions(name="measure_current")
@htf.measures(htf.Measurement("current").in_range(0.0, 1.0).with_units("A"))
def measure_current_phase(test: htf.TestApi) -> None:
    """Measure power rail current (in-range -> PASS)."""
    test.measurements["current"] = 0.12


@htf.PhaseOptions(name="slow_phase")
def slow_phase(test: htf.TestApi) -> None:
    """Sleep for a fixed duration to trigger timeout in isolation mode.

    This phase always passes if allowed to complete, but when executed
    under ``use_isolation=True`` with a timeout shorter than
    ``_SLOW_PHASE_SLEEP_SECONDS``, the parent process terminates the
    child before this phase finishes.
    """
    test.logger.info(
        "Slow phase sleeping for %s seconds", _SLOW_PHASE_SLEEP_SECONDS
    )
    time.sleep(_SLOW_PHASE_SLEEP_SECONDS)


def create_test(
    slow: bool = False,
    dut_id: str = "UNKNOWN",
    **kwargs: object,
) -> htf.Test:
    """Factory that returns a real ``htf.Test`` for E2E testing.

    Args:
        slow: If True, append the slow_phase (used by the timeout test).
        dut_id: DUT identifier (consumed by OpenHTFStepExecutor via
            ``params["dut_id"]``, not used directly by this factory).
        **kwargs: Ignored -- accepted so ``create_test(**params)`` does
            not raise on unexpected keys from the executor.

    Returns:
        A configured ``htf.Test`` ready for ``execute()``.
    """
    del dut_id, kwargs  # consumed by executor, not this factory
    phases = [setup_phase, measure_voltage_phase, measure_current_phase]
    if slow:
        phases.append(slow_phase)
    return htf.Test(*phases)
