"""SPC math helpers - capability indices, control-chart constants, Western Electric rules.

Pure functions with no I/O; the SPCProcessor composes them over a sliding
window of measurements. Kept separate from spc.py so the math is testable
in isolation and spc.py stays under the 250-LOC ceiling.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean. Raises ValueError on empty input."""
    n = len(values)
    if n == 0:
        raise ValueError("mean of empty sequence")
    return sum(values) / n


def population_stddev(values: Sequence[float], mu: float | None = None) -> float:
    """Population standard deviation (divide by N, not N-1).

    Used for Ppk (overall sigma). Raises ValueError on empty input.
    """
    n = len(values)
    if n == 0:
        raise ValueError("stddev of empty sequence")
    if mu is None:
        mu = mean(values)
    var = sum((v - mu) ** 2 for v in values) / n
    return math.sqrt(var)


def subgroup_ranges(subgroups: Sequence[Sequence[float]]) -> list[float]:
    """Range (max - min) of each subgroup. Single-element subgroups have range 0."""
    return [max(sg) - min(sg) for sg in subgroups if len(sg) > 0]


def chunk(values: Sequence[float], size: int) -> list[list[float]]:
    """Split values into consecutive chunks of ``size``; the last may be shorter."""
    if size < 1:
        raise ValueError("size must be >= 1")
    return [list(values[i : i + size]) for i in range(0, len(values), size)]


# Control-chart constants for subgroup size n (Shewhart tables).
# A2, D3, D4 are the standard factors for n=2..7. D3=0 for n<7 (R LCL clamped to 0).
_CONTROL_CONSTANTS: dict[int, tuple[float, float, float]] = {
    2: (1.880, 0.000, 3.267),
    3: (1.023, 0.000, 2.574),
    4: (0.729, 0.000, 2.282),
    5: (0.577, 0.000, 2.114),
    6: (0.483, 0.000, 2.004),
    7: (0.419, 0.076, 1.924),
}

# d2 constant: ratio of expected range to sigma for a given subgroup size.
_D2: dict[int, float] = {2: 1.128, 3: 1.693, 4: 2.059, 5: 2.326, 6: 2.534, 7: 2.704}


def control_constants(n: int) -> tuple[float, float, float]:
    """Return (A2, D3, D4) for subgroup size n (2..7). Raises KeyError outside range."""
    return _CONTROL_CONSTANTS[n]


def d2(n: int) -> float:
    """Return the d2 constant for subgroup size n (2..7)."""
    return _D2[n]


def cp(usl: float, lsl: float, sigma_within: float) -> float:
    """Process capability (potential): (USL - LSL) / (6 * sigma_within)."""
    return (usl - lsl) / (6.0 * sigma_within)


def cpk(usl: float, lsl: float, mu: float, sigma_within: float) -> float:
    """Process capability index (actual): min((USL-mu), (mu-LSL)) / (3*sigma)."""
    upper = (usl - mu) / (3.0 * sigma_within)
    lower = (mu - lsl) / (3.0 * sigma_within)
    return min(upper, lower)


def ppk(usl: float, lsl: float, mu: float, sigma_overall: float) -> float:
    """Preliminary process performance: min((USL-mu), (mu-LSL)) / (3*sigma_overall)."""
    upper = (usl - mu) / (3.0 * sigma_overall)
    lower = (mu - lsl) / (3.0 * sigma_overall)
    return min(upper, lower)


def western_electric_rules(
    values: Sequence[float],
    mu: float,
    sigma: float,
) -> list[str]:
    """Evaluate Western Electric (Nelson) rules on the latest samples.

    Checks the most recent samples against the four classic WE rules:
      WE1: a single point beyond 3 sigma.
      WE2: 2 of 3 consecutive points beyond 2 sigma (same side).
      WE3: 4 of 5 consecutive points beyond 1 sigma (same side).
      WE4: 8 consecutive points on one side of the center line.

    Returns the list of rule names triggered by the most recent point. Only
    rules whose pattern is completed by the last sample are reported - this
    makes the detector online (one alert per qualifying measurement).
    """
    n = len(values)
    if n == 0 or sigma <= 0:
        return []

    triggered: list[str] = []
    last = values[-1]

    # WE1 - latest point beyond 3 sigma
    if abs(last - mu) > 3 * sigma:
        triggered.append("WE1_beyond_3sigma")

    # WE2 - 2 of 3 consecutive beyond 2 sigma, same side
    if n >= 2:
        window = values[-3:] if n >= 3 else values[-2:]
        beyond = [v for v in window if abs(v - mu) > 2 * sigma]
        if len(beyond) >= 2 and _same_side(beyond, mu):
            triggered.append("WE2_2of3_beyond_2sigma")

    # WE3 - 4 of 5 consecutive beyond 1 sigma, same side
    if n >= 4:
        window = values[-5:] if n >= 5 else values[-4:]
        beyond = [v for v in window if abs(v - mu) > 1 * sigma]
        if len(beyond) >= 4 and _same_side(beyond, mu):
            triggered.append("WE3_4of5_beyond_1sigma")

    # WE4 - 8 consecutive on one side of center
    if n >= 8:
        window = values[-8:]
        if all(v > mu for v in window) or all(v < mu for v in window):
            triggered.append("WE4_8_consecutive_one_side")

    return triggered


def _same_side(values: Sequence[float], center: float) -> bool:
    """True if all values are strictly on the same side of center."""
    if not values:
        return False
    above = all(v > center for v in values)
    below = all(v < center for v in values)
    return above or below
