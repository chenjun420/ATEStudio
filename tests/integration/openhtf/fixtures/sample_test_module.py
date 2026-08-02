"""Real OpenHTF test module for integration testing.

This module provides a ``create_test()`` factory that returns real
``htf.Test`` instances with simple phases. It is imported dynamically by
``OpenHTFStepExecutor`` via ``importlib.import_module`` during integration
tests -- no mocking of OpenHTF internals.

Phases:
    - ``setup_phase``: always passes, no measurements (DUT initialization).
    - ``measure_power_phase``: takes voltage and current measurements with
      ``in_range`` validators. Values are in-range -> PASS.
    - ``measure_fail_phase``: takes a voltage measurement with an
      ``in_range`` validator. The value is out-of-range -> FAIL.

Factory:
    ``create_test(fail=False, dut_id="TEST_DUT_001")`` -- selects phases
    based on the ``fail`` flag. The ``dut_id`` is consumed by
    ``OpenHTFStepExecutor`` via ``params["dut_id"]`` (passed to
    ``test_start``), not by this module directly.
"""

from __future__ import annotations

import openhtf as htf

__all__ = ["create_test", "setup_phase", "measure_power_phase", "measure_fail_phase"]


@htf.PhaseOptions(name="setup")
def setup_phase(test: htf.TestApi) -> None:
    """Initialize the DUT before measurement.

    A no-op phase that always passes. Demonstrates a phase without
    measurements -- common in ATE workflows for instrument setup.
    """
    # Real setup would configure instruments here.
    test.logger.info("Setup phase complete")


@htf.PhaseOptions(name="measure_power")
@htf.measures(htf.Measurement("voltage").in_range(4.5, 5.5).with_units("V"))
@htf.measures(htf.Measurement("current").in_range(0.0, 1.0).with_units("A"))
def measure_power_phase(test: htf.TestApi) -> None:
    """Measure power rail voltage and current.

    Sets in-range values (5.0V, 0.12A) so the phase outcome is PASS and
    the test outcome is PASS.
    """
    test.measurements["voltage"] = 5.0
    test.measurements["current"] = 0.12


@htf.PhaseOptions(name="measure_fail")
@htf.measures(htf.Measurement("voltage").in_range(4.5, 5.5).with_units("V"))
def measure_fail_phase(test: htf.TestApi) -> None:
    """Measure voltage with an out-of-range value to trigger FAIL.

    Sets 6.0V which is outside [4.5, 5.5] -> measurement outcome FAIL ->
    phase outcome FAIL -> test outcome FAIL.
    """
    test.measurements["voltage"] = 6.0


def create_test(fail: bool = False, dut_id: str = "TEST_DUT_001", **kwargs: object) -> htf.Test:
    """Factory that returns a real ``htf.Test`` for integration testing.

    Args:
        fail: If True, include the failing measurement phase; otherwise
            include the passing measurement phase.
        dut_id: DUT identifier (consumed by OpenHTFStepExecutor via
            ``params["dut_id"]``, not used directly by this factory).
        **kwargs: Ignored -- accepted so ``create_test(**params)`` does not
            raise on unexpected keys from the executor.

    Returns:
        A configured ``htf.Test`` ready for ``execute()``.
    """
    del dut_id, kwargs  # consumed by executor, not this factory
    if fail:
        return htf.Test(setup_phase, measure_fail_phase)
    return htf.Test(setup_phase, measure_power_phase)
