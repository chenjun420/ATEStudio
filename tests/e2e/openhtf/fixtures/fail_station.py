"""Real OpenHTF test station for the E2E fail-flow test.

Provides a ``create_test()`` factory that returns a real ``htf.Test``
where one phase fails due to an out-of-range measurement.

Phases:
    - ``setup_phase``: always passes, no measurements.
    - ``measure_fail_phase``: takes a voltage measurement (6.0V,
      outside [4.5, 5.5] -> measurement FAIL -> phase FAIL ->
      test FAIL).

Factory:
    ``create_test(dut_id="UNKNOWN")`` -- the ``dut_id`` is consumed by
    ``OpenHTFStepExecutor`` via ``params["dut_id"]`` (passed to
    ``test_start``), not by this module directly.
"""

from __future__ import annotations

import openhtf as htf

__all__ = ["create_test"]


@htf.PhaseOptions(name="setup")
def setup_phase(test: htf.TestApi) -> None:
    """Initialize the DUT before measurement."""
    test.logger.info("E2E fail-station setup complete")


@htf.PhaseOptions(name="measure_fail")
@htf.measures(htf.Measurement("voltage").in_range(4.5, 5.5).with_units("V"))
def measure_fail_phase(test: htf.TestApi) -> None:
    """Measure voltage with an out-of-range value to trigger FAIL.

    Sets 6.0V which is outside [4.5, 5.5] -> measurement outcome FAIL
    -> phase outcome FAIL -> test outcome FAIL.
    """
    test.measurements["voltage"] = 6.0


def create_test(
    dut_id: str = "UNKNOWN",
    **kwargs: object,
) -> htf.Test:
    """Factory that returns a real ``htf.Test`` that fails.

    Args:
        dut_id: DUT identifier (consumed by OpenHTFStepExecutor via
            ``params["dut_id"]``, not used directly by this factory).
        **kwargs: Ignored -- accepted so ``create_test(**params)`` does
            not raise on unexpected keys from the executor.

    Returns:
        A configured ``htf.Test`` with a failing measurement phase.
    """
    del dut_id, kwargs  # consumed by executor, not this factory
    return htf.Test(setup_phase, measure_fail_phase)
