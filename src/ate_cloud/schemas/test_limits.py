"""Pydantic schemas for TestLimit resources.

This module defines the data models for test limit CRUD:
- TestLimitBase: Common fields for test limit data
- TestLimitCreate: Schema for creating new test limits
- TestLimitUpdate: Schema for updating existing test limits (all optional)
- TestLimitResponse: Schema for test limit API responses
- LimitQuery: Query parameters for the resolve endpoint
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

# Type alias to avoid field-name/type-name shadowing in LimitQuery where
# the field is named ``date`` and the type is ``date | None``. Pydantic v2
# evaluates annotations against the class namespace, where ``date`` would
# resolve to the FieldInfo default rather than the datetime.date class.
DateType = date


class TestLimitBase(BaseModel):
    """Base schema for test limit data.

    Attributes:
        limit_id: Business identifier for this limit version (1-255 chars).
        product_type: Product type identifier (1-255 chars).
        test_name: Test measurement name (1-255 chars).
        spec_low: Lower bound of the acceptable range (inclusive). Optional —
            one-sided criteria (over-voltage / under-voltage protection) are
            the norm in real specs.
        spec_typ: Typical / set-point value. Optional.
        spec_high: Upper bound of the acceptable range (inclusive). Optional,
            see ``spec_low``.
        unit: Engineering unit (1-64 chars).
        effective_from: Date (inclusive) this limit becomes effective.
        effective_until: Date (inclusive) this limit expires; None for
            indefinite.
    """

    limit_id: str = Field(..., min_length=1, max_length=255)
    product_type: str = Field(..., min_length=1, max_length=255)
    test_name: str = Field(..., min_length=1, max_length=255)
    spec_low: float | None = Field(default=None)
    spec_typ: float | None = Field(default=None)
    spec_high: float | None = Field(default=None)
    unit: str = Field(..., min_length=1, max_length=64)
    effective_from: date = Field(...)
    effective_until: date | None = Field(default=None)

    @model_validator(mode="after")
    def _bounds_sane(self) -> TestLimitBase:
        """Bounds sanity: not both absent, and high >= low when both present.

        Both-absent means the row carries no criterion at all — it would sit in
        the limit table looking authoritative while judging nothing, so it is
        rejected at the boundary rather than silently accepted.
        """
        if self.spec_low is None and self.spec_high is None and self.spec_typ is None:
            raise ValueError("spec_low / spec_typ / spec_high 至少要提供一个")
        if (
            self.spec_low is not None
            and self.spec_high is not None
            and self.spec_high < self.spec_low
        ):
            raise ValueError(f"spec_high ({self.spec_high}) must be >= spec_low ({self.spec_low})")
        return self

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


class TestLimitCreate(TestLimitBase):
    """Schema for creating a new test limit.

    Inherits all fields from TestLimitBase.
    """

    pass


class TestLimitUpdate(BaseModel):
    """Schema for updating an existing test limit.

    All fields are optional to support partial updates. The id (UUID) path
    parameter is the lookup key; limit_id may be changed via the body.

    Attributes:
        limit_id: Updated business identifier.
        product_type: Updated product type identifier.
        test_name: Updated test measurement name.
        spec_low: Updated lower bound.
        spec_typ: Updated typical/set-point value.
        spec_high: Updated upper bound.
        unit: Updated engineering unit.
        effective_from: Updated effective-from date.
        effective_until: Updated effective-until date.
    """

    limit_id: str | None = Field(None, min_length=1, max_length=255)
    product_type: str | None = Field(None, min_length=1, max_length=255)
    test_name: str | None = Field(None, min_length=1, max_length=255)
    spec_low: float | None = None
    spec_typ: float | None = None
    spec_high: float | None = None
    unit: str | None = Field(None, min_length=1, max_length=64)
    effective_from: date | None = None
    effective_until: date | None = None

    @model_validator(mode="after")
    def _bounds_sane_if_both(self) -> TestLimitUpdate:
        """Partial update: only compare bounds when both are supplied."""
        if (
            self.spec_low is not None
            and self.spec_high is not None
            and self.spec_high < self.spec_low
        ):
            raise ValueError(
                f"spec_high ({self.spec_high}) must be >= spec_low ({self.spec_low})"
            )
        return self

    @field_validator("effective_until")
    @classmethod
    def _effective_until_after_from_if_both(
        cls, v: date | None, info: ValidationInfo
    ) -> date | None:
        """Ensure effective_until >= effective_from when both are present."""
        if v is None:
            return v
        effective_from = info.data.get("effective_from")
        if effective_from is not None and v < effective_from:
            raise ValueError(
                f"effective_until ({v}) must be >= effective_from ({effective_from})"
            )
        return v


class TestLimitResponse(TestLimitBase):
    """Schema for test limit API responses.

    Extends TestLimitBase with system-managed fields.

    Attributes:
        id: Unique test limit identifier (UUID).
        created_at: Timestamp of creation.
        updated_at: Timestamp of last update.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = {"from_attributes": True}


class LimitQuery(BaseModel):
    """Query parameters for the resolve endpoint.

    Attributes:
        product_type: Product type identifier to resolve for.
        test_name: Test measurement name to resolve for.
        date: Optional date for resolution; defaults to today when None.
    """

    product_type: str = Field(..., min_length=1, max_length=255)
    test_name: str = Field(..., min_length=1, max_length=255)
    date: DateType | None = Field(default=None, description="Resolution date; today if None")
