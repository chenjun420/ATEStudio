"""TestLimit SQLAlchemy model.

This module defines the TestLimit database model for storing multi-versioned
test limits. Each row is one version of a limit for a (product_type, test_name)
pair, effective over a date range [effective_from, effective_until]. Multiple
versions can coexist; the LimitResolver selects the effective one for a given
date.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class TestLimit(Base):
    """TestLimit database model.

    测试限值数据库模型 -- 存储多版本测试限值，按生效日期范围区分版本。

    Attributes:
        id: Unique identifier (UUID as string).
        limit_id: Business identifier for this limit version.
        product_type: Product type identifier this limit applies to.
        test_name: Name of the test measurement.
        spec_low: Lower bound (inclusive). NULLABLE since one-sided limits are
            the norm in real specs (an over-voltage test has only an upper
            bound, an under-voltage test only a lower one). Previously NOT NULL,
            which made single-sided criteria impossible to store — the only way
            to fit was to invent the missing side, and inventing a pass/fail
            criterion is fabricating a quality standard.
        spec_typ: Typical/set-point value. NULLABLE; added for three-valued
            limits (min/typ/max) whose set point was previously unrepresentable.
        spec_high: Upper bound (inclusive). NULLABLE, see ``spec_low``.
        unit: Engineering unit of the measurement.
        effective_from: Date (inclusive) this limit becomes effective.
        effective_until: Date (inclusive) this limit expires; NULL for
            indefinite (no expiry).
        created_at: Timestamp of creation.
        updated_at: Timestamp of last update.
    """

    # Not a pytest test class — the name starts with "Test" so tell pytest not
    # to collect it (suppresses PytestCollectionWarning "cannot collect").
    __test__ = False

    __tablename__ = "test_limits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    limit_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    product_type: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    test_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    spec_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    spec_typ: Mapped[float | None] = mapped_column(Float, nullable=True)
    spec_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, index=True
    )
    effective_until: Mapped[date | None] = mapped_column(
        Date, nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
