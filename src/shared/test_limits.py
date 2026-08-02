"""Test limits schema for ATE Studio.

This module defines Pydantic v2 models for multi-versioned test limits:
- TestLimit: Defines the acceptable spec range (low/high) for a single test
  measurement on a product type, effective over a date range.
- TestLimitList: Wraps multiple TestLimit entries for batch transport.

测试限值 -- 定义某个产品类型某项测试在指定日期范围内的合格上下限。
Test limits are multi-versioned: multiple limits for the same test_name on the
same product_type can coexist with different effective_from dates. The current
date determines which version is effective (duckDuckGo resolution).

All models use ``extra='forbid'`` for strict validation -- unknown keys are
rejected rather than silently ignored, preventing configuration drift.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

__all__ = [
    "TestLimit",
    "TestLimitList",
]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestLimit(BaseModel):
    """Multi-versioned test limit for a single measurement.

    测试限值 -- 定义某项测试测量值的合格上下限及生效日期范围。

    Attributes:
        limit_id: Unique identifier for this limit version.
        product_type: Product type identifier (e.g. ``"comm_module_v2"``).
        test_name: Name of the test measurement this limit applies to
            (e.g. ``"tx_power"``).
        spec_low: Lower bound of the acceptable range (inclusive).
        spec_high: Upper bound of the acceptable range (inclusive).
        unit: Engineering unit of the measurement (e.g. ``"dBm"``, ``"V"``).
        effective_from: Date (inclusive) from which this limit becomes effective.
        effective_until: Date (inclusive) until which this limit remains
            effective. ``None`` means the limit is effective indefinitely
            (no expiry).
    """

    model_config = ConfigDict(extra="forbid")

    limit_id: str = Field(..., min_length=1, description="Unique limit identifier")
    product_type: str = Field(..., min_length=1, description="Product type identifier")
    test_name: str = Field(..., min_length=1, description="Test measurement name")
    spec_low: float = Field(..., description="Lower bound of acceptable range")
    spec_high: float = Field(..., description="Upper bound of acceptable range")
    unit: str = Field(..., min_length=1, description="Engineering unit of the measurement")
    effective_from: date = Field(..., description="Date (inclusive) this limit becomes effective")
    effective_until: date | None = Field(
        default=None, description="Date (inclusive) this limit expires; None for indefinite"
    )

    @field_validator("spec_high")
    @classmethod
    def _spec_high_must_exceed_low(cls, v: float, info: ValidationInfo) -> float:
        """Ensure spec_high >= spec_low when both are present.

        Validation runs after field assignment, so info.data contains
        spec_low if it was provided. A zero-width range (low == high) is
        permitted (degenerate but valid boundary case).
        """
        low = info.data.get("spec_low")
        if low is not None and v < low:
            raise ValueError(
                f"spec_high ({v}) must be >= spec_low ({low})"
            )
        return v

    @field_validator("effective_until")
    @classmethod
    def _effective_until_after_from(cls, v: date | None, info: ValidationInfo) -> date | None:
        """Ensure effective_until >= effective_from when both are present."""
        if v is None:
            return v
        effective_from = info.data.get("effective_from")
        if effective_from is not None and v < effective_from:
            raise ValueError(
                f"effective_until ({v}) must be >= effective_from ({effective_from})"
            )
        return v


class TestLimitList(BaseModel):
    """Wrapper for a collection of TestLimit entries.

    用于批量传输的测试限值列表。

    Attributes:
        limits: List of TestLimit entries.
    """

    model_config = ConfigDict(extra="forbid")

    limits: list[TestLimit] = Field(default_factory=list, description="Test limit entries")
