"""Structured measurement model for ATE Platform.

This module defines the Pydantic v2 model for a single structured
measurement captured during test script execution:

- MeasurementOutcome: Enum tagging the pass/fail/warning verdict of a measurement
- Measurement: A single measurement (name, value, limits, expected, unit, outcome, timestamp)

Outcome auto-calculation rule (applied at construction time unless an explicit
outcome is provided):

    value within [limits_min, limits_max] -> PASS
    value outside limits                   -> FAIL
    value within 5% of limit boundary      -> WARNING (if not FAIL)

When a limit is None the corresponding bound is treated as unbounded.
``Measurement`` is a Pydantic v2 ``BaseModel`` (``extra='forbid'``) for strict
validation -- unknown keys are rejected rather than silently ignored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "MeasurementOutcome",
    "Measurement",
    "MEASUREMENT_WARNING_MARGIN",
]


#: Fraction of the limit window used as the warning band.
#: A measurement within this fraction of either limit (but not outside the
#: limits) is flagged WARNING.
MEASUREMENT_WARNING_MARGIN: float = 0.05


class MeasurementOutcome(str, Enum):
    """Verdict for a single measurement.

    测量结果判定 -- 单条测量的最终结论。

    Attributes:
        PASS: Value is within limits (and not in the warning band).
        FAIL: Value is outside the configured limits.
        WARNING: Value is within limits but within the warning margin of a limit.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    WARNING = "WARNING"


def _calculate_outcome(
    value: float | None,
    limits_min: float | None,
    limits_max: float | None,
    margin: float = MEASUREMENT_WARNING_MARGIN,
) -> MeasurementOutcome:
    """Compute the outcome for a measurement value against its limits.

    Args:
        value: The measured value. If None, outcome is PASS (no numeric check).
        limits_min: Lower limit (None means unbounded below).
        limits_max: Upper limit (None means unbounded above).
        margin: Fraction of the limit window treated as the warning band.

    Returns:
        The computed MeasurementOutcome.
    """
    if value is None:
        return MeasurementOutcome.PASS

    # FAIL: outside the configured limits
    if limits_min is not None and value < limits_min:
        return MeasurementOutcome.FAIL
    if limits_max is not None and value > limits_max:
        return MeasurementOutcome.FAIL

    # WARNING: within limits but within margin of either boundary.
    # The warning band is ``margin`` fraction of the limit window width.
    # When both limits are present the window is [limits_min, limits_max].
    # When only one limit is present the window is measured from that limit
    # toward the value (i.e. the band is ``margin * abs(limit)`` when the
    # other side is unbounded, falling back to ``margin * |limit|``).
    if limits_min is not None and limits_max is not None:
        window = limits_max - limits_min
        if window <= 0:
            # Degenerate window (min >= max) -- no warning band possible.
            return MeasurementOutcome.PASS
        band = window * margin
        if value <= limits_min + band or value >= limits_max - band:
            return MeasurementOutcome.WARNING
    elif limits_min is not None:
        # Only lower limit; warning if value is within margin * |limits_min| of it.
        band = abs(limits_min) * margin
        if value <= limits_min + band:
            return MeasurementOutcome.WARNING
    elif limits_max is not None:
        # Only upper limit; warning if value is within margin * |limits_max| of it.
        band = abs(limits_max) * margin
        if value >= limits_max - band:
            return MeasurementOutcome.WARNING

    return MeasurementOutcome.PASS


class Measurement(BaseModel):
    """A single structured measurement captured during a test step.

    结构化测量值 -- 一条测试步骤中采集到的测量值及其判定。

    Attributes:
        name: Measurement identifier (e.g. ``"voltage_3v3"``).
        value: The measured value. May be None for non-numeric measurements.
        limits_min: Lower acceptance limit (None = unbounded below).
        limits_max: Upper acceptance limit (None = unbounded above).
        expected: Expected/nominal value (informational, not used for outcome).
        unit: Engineering unit (e.g. ``"V"``, ``"A"``, ``"Hz"``). None if unitless.
        outcome: PASS | FAIL | WARNING. Auto-calculated from value vs limits
            when not explicitly provided.
        timestamp: When the measurement was recorded (UTC, auto-generated if absent).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Measurement identifier")
    value: float | None = Field(default=None, description="Measured value (None for non-numeric)")
    limits_min: float | None = Field(default=None, description="Lower acceptance limit")
    limits_max: float | None = Field(default=None, description="Upper acceptance limit")
    expected: float | None = Field(default=None, description="Expected/nominal value")
    unit: str | None = Field(default=None, description="Engineering unit (e.g. V, A, Hz)")
    outcome: MeasurementOutcome = Field(
        default=MeasurementOutcome.PASS,
        description="Pass/Fail/Warning verdict (auto-calculated if not provided)",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Measurement timestamp (UTC)",
    )

    @field_validator("outcome", mode="before")
    @classmethod
    def _coerce_outcome(cls, v: object) -> object:
        """Allow outcome to be specified as a plain string."""
        if isinstance(v, str) and not isinstance(v, MeasurementOutcome):
            return MeasurementOutcome(v)
        return v

    @model_validator(mode="after")
    def _auto_calculate_outcome(self) -> Measurement:
        """Recalculate outcome from value vs limits when not explicitly FAIL.

        If the caller set outcome to FAIL explicitly we honour it. Otherwise we
        recompute from the value and limits so that stale outcomes cannot ship.
        """
        if self.outcome == MeasurementOutcome.FAIL:
            # Explicit FAIL is honoured only if it matches the computed verdict;
            # otherwise the computed verdict wins (a value inside limits cannot
            # be a FAIL just because the caller said so).
            computed = _calculate_outcome(self.value, self.limits_min, self.limits_max)
            if computed == MeasurementOutcome.FAIL:
                return self
            # Caller said FAIL but value is inside limits -- trust the math.
            object.__setattr__(self, "outcome", computed)
            return self

        computed = _calculate_outcome(self.value, self.limits_min, self.limits_max)
        object.__setattr__(self, "outcome", computed)
        return self
