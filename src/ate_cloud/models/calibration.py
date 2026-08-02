"""CalibrationRecord SQLAlchemy model.

Stores instrument calibration records. Each row tracks the calibration
state of a single instrument: last calibration date, calibration interval
(days), computed next-due date, derived status (VALID/EXPIRING/EXPIRED),
and optional notes. The CalibrationManager background service recomputes
the status column based on the next_due date relative to "now".
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class CalibrationRecord(Base):
    """CalibrationRecord database model.

    仪器校准记录 -- 跟踪每台仪器的校准状态。CalibrationManager 根据
    ``next_due`` 日期相对当前时间计算 status 字段：7 天内到期为
    ``EXPIRING`` 告警，超过到期日为 ``EXPIRED`` 阻止执行，否则为
    ``VALID``。

    Attributes:
        id: Unique identifier (UUID as string).
        instrument_id: Instrument identifier (e.g., resource name or VISA address).
        last_calibration: Date/time of the last calibration.
        interval_days: Calibration interval in days.
        next_due: Computed next-due date (last_calibration + interval_days).
        status: Calibration status: ``VALID``, ``EXPIRING``, or ``EXPIRED``.
        notes: Optional free-text notes about the calibration.
        created_at: Timestamp when this DB row was inserted.
        updated_at: Timestamp of the last update.
    """

    __tablename__ = "calibration_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    last_calibration: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    interval_days: Mapped[int] = mapped_column(Integer, nullable=False)
    next_due: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
