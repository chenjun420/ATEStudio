"""Measurement SQLAlchemy model - persisted test measurement records.

Stores individual structured measurements (one row per measurement name per
DUT per execution) for SPC analysis, historical traceability, and alert
correlation. The shared Pydantic ``Measurement`` (``shared.measurement``)
is the in-memory wire format; this ORM model is the persistence projection.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class Measurement(Base):
    """SQLAlchemy model for persisted measurement records.

    测量值持久化模型 -- 单条测量值及其判定，用于 SPC 分析与故障追溯。

    Attributes:
        measurement_id: Unique identifier (UUID4).
        execution_ref: Reference to the execution that produced this measurement.
        station_ref: Reference to the station that captured the measurement.
        product_ref: Product type identifier (FK-like; not enforced).
        dut_serial: Device-under-test serial number.
        timestamp: When the measurement was captured (UTC).
        name: Measurement identifier (e.g. ``"voltage_3v3"``).
        value: Numeric measured value.
        limits_min: Lower acceptance limit (nullable).
        limits_max: Upper acceptance limit (nullable).
        unit: Engineering unit (e.g. ``"V"``, ``"A"``).
        outcome: PASS | FAIL | WARNING verdict.
        created_at: Row insertion timestamp.
    """

    __tablename__ = "measurements"

    measurement_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_ref: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    station_ref: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    product_ref: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    dut_serial: Mapped[str] = mapped_column(String(255), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    limits_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    limits_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="PASS")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # SPC queries filter by product + measurement name, then order by time.
        Index("ix_measurements_product_name_ts", "product_ref", "name", "timestamp"),
    )
