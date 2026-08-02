"""CalibrationManager -- instrument calibration status tracking and enforcement.

Provides:
    - check_status(instrument_id) -> status for a single instrument.
    - check_expiry(session) -> refresh status column for all records.
    - record_calibration(session, data) -> create / update a calibration record.
    - is_expired(session, instrument_id) -> True when an instrument is EXPIRED
      (used by the execution dispatch path to block test runs with HTTP 409).

Status derivation (relative to ``next_due``):
    - now < next_due - EXPIRING_WINDOW_DAYS  -> VALID
    - next_due - 7d <= now < next_due         -> EXPIRING (warn)
    - now >= next_due                          -> EXPIRED (block execution)

An instrument with no calibration record is treated as ``UNKNOWN`` and is
NOT blocked - calibration is opt-in per instrument.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.calibration import CalibrationRecord
from ate_cloud.schemas.calibration import (
    EXPIRING_WINDOW_DAYS,
    CalibrationCreate,
    CalibrationResponse,
    CalibrationStatus,
    CalibrationUpdate,
)

logger = logging.getLogger(__name__)


class CalibrationManager:
    """Track instrument calibration status and enforce expiry blocks.

    仪器校准管理器 -- 根据 next_due 日期计算 VALID/EXPIRING/EXPIRED
    状态。EXPIRED 仪器会被执行分发路径阻止（HTTP 409）。

    The manager is stateless across requests: each call performs fresh
    database queries against the provided session. Construct one manager
    per request or reuse a single instance - there is no internal state.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Initialize the manager with a database session.

        Args:
            db: Async SQLAlchemy session used for all queries and writes.
        """
        self._db = db

    # ------------------------------------------------------------------
    # Pure status computation (no DB) - unit-testable in isolation.
    # ------------------------------------------------------------------

    @staticmethod
    def compute_status(
        next_due: datetime,
        now: datetime | None = None,
        expiring_window_days: int = EXPIRING_WINDOW_DAYS,
    ) -> CalibrationStatus:
        """Compute VALID/EXPIRING/EXPIRED from next_due.

        Handles timezone-naive ``next_due`` by treating it as UTC. If
        ``now`` is None, ``datetime.now(UTC)`` is used. When comparing
        naive and aware datetimes, both are normalized to naive UTC for
        the comparison (SQLite stores naive datetimes).

        Args:
            next_due: The next-due datetime for the instrument.
            now: Reference "now" datetime. Defaults to current UTC time.
            expiring_window_days: Days before next_due that status becomes
                EXPIRING. Defaults to 7.

        Returns:
            One of ``VALID``, ``EXPIRING``, ``EXPIRED``.
        """
        ref_now = now if now is not None else datetime.now(UTC)
        due = _normalize(next_due)
        cur = _normalize(ref_now)
        expiring_threshold = due - timedelta(days=expiring_window_days)
        if cur >= due:
            return "EXPIRED"
        if cur >= expiring_threshold:
            return "EXPIRING"
        return "VALID"

    # ------------------------------------------------------------------
    # Single-instrument status query.
    # ------------------------------------------------------------------

    async def check_status(
        self, instrument_id: str
    ) -> CalibrationResponse | None:
        """Return the latest calibration record for an instrument.

        The returned record's ``status`` field is recomputed against the
        current time (it is NOT trusted from the DB column, which may be
        stale between check_expiry runs). If the recomputed status differs
        from the stored value, the DB row is updated in place.

        Args:
            instrument_id: The instrument identifier to look up.

        Returns:
            The latest CalibrationRecord (with a fresh status), or None if
            no calibration record exists for this instrument.
        """
        record = await self._fetch_latest(instrument_id)
        if record is None:
            return None
        fresh = self.compute_status(record.next_due)
        if record.status != fresh:
            record.status = fresh
            await self._db.commit()
            await self._db.refresh(record)
        return CalibrationResponse.model_validate(record)

    async def is_expired(self, instrument_id: str) -> bool:
        """Return True when the instrument is EXPIRED (blocks execution).

        Returns False for instruments with no calibration record
        (calibration is opt-in) and for VALID/EXPIRING instruments.

        Args:
            instrument_id: The instrument identifier to check.

        Returns:
            True if a calibration record exists and its current status is
            EXPIRED; False otherwise.
        """
        record = await self._fetch_latest(instrument_id)
        if record is None:
            return False
        return self.compute_status(record.next_due) == "EXPIRED"

    # ------------------------------------------------------------------
    # Batch expiry refresh (background task hook).
    # ------------------------------------------------------------------

    async def check_expiry(self) -> int:
        """Refresh the status column for all calibration records.

        Iterates every CalibrationRecord row, recomputes the status against
        the current time, and issues a single bulk update for rows whose
        status changed. The caller owns the session commit lifecycle.

        Returns:
            Number of records whose status changed.
        """
        result = await self._db.execute(select(CalibrationRecord))
        records = list(result.scalars().all())
        changed = 0
        for record in records:
            fresh = self.compute_status(record.next_due)
            if record.status != fresh:
                record.status = fresh
                changed += 1
        if changed > 0:
            await self._db.commit()
        return changed

    # ------------------------------------------------------------------
    # Record creation / update.
    # ------------------------------------------------------------------

    async def record_calibration(
        self, data: CalibrationCreate
    ) -> CalibrationResponse:
        """Create a new calibration record for an instrument.

        If a record already exists for ``instrument_id``, the latest one
        is updated in place (last_calibration, interval_days, next_due,
        notes, status) rather than creating a duplicate. This keeps a
        single source of truth per instrument.

        Args:
            data: Calibration creation payload.

        Returns:
            The created or updated CalibrationRecord.
        """
        existing = await self._fetch_latest(data.instrument_id)
        next_due = data.last_calibration + timedelta(days=data.interval_days)
        status = self.compute_status(next_due)

        if existing is not None:
            existing.last_calibration = data.last_calibration
            existing.interval_days = data.interval_days
            existing.next_due = next_due
            existing.status = status
            existing.notes = data.notes
            await self._db.commit()
            await self._db.refresh(existing)
            logger.info(
                "Updated calibration for instrument %s (next_due=%s, status=%s)",
                data.instrument_id,
                next_due.isoformat(),
                status,
            )
            return CalibrationResponse.model_validate(existing)

        record = CalibrationRecord(
            id=str(uuid.uuid4()),
            instrument_id=data.instrument_id,
            last_calibration=data.last_calibration,
            interval_days=data.interval_days,
            next_due=next_due,
            status=status,
            notes=data.notes,
        )
        self._db.add(record)
        await self._db.commit()
        await self._db.refresh(record)
        logger.info(
            "Recorded calibration for instrument %s (next_due=%s, status=%s)",
            data.instrument_id,
            next_due.isoformat(),
            status,
        )
        return CalibrationResponse.model_validate(record)

    async def update_calibration(
        self, instrument_id: str, data: CalibrationUpdate
    ) -> CalibrationResponse | None:
        """Partially update a calibration record for an instrument.

        Args:
            instrument_id: The instrument to update (latest record wins).
            data: Partial update payload.

        Returns:
            The updated CalibrationResponse, or None if no record exists.
        """
        existing = await self._fetch_latest(instrument_id)
        if existing is None:
            return None

        if data.instrument_id is not None:
            existing.instrument_id = data.instrument_id
        if data.last_calibration is not None:
            existing.last_calibration = data.last_calibration
        if data.interval_days is not None:
            existing.interval_days = data.interval_days
        # Recompute next_due if either calibration date or interval changed.
        if data.last_calibration is not None or data.interval_days is not None:
            existing.next_due = existing.last_calibration + timedelta(
                days=existing.interval_days
            )
        if data.notes is not None:
            existing.notes = data.notes
        existing.status = self.compute_status(existing.next_due)

        await self._db.commit()
        await self._db.refresh(existing)
        return CalibrationResponse.model_validate(existing)

    async def list_records(
        self,
        instrument_id: str | None = None,
        status_filter: CalibrationStatus | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[CalibrationRecord]:
        """List calibration records with optional filters.

        Args:
            instrument_id: Optional instrument filter.
            status_filter: Optional status filter.
            skip: Number of records to skip.
            limit: Maximum records to return.

        Returns:
            List of CalibrationRecord models ordered by next_due ascending.
        """
        stmt = select(CalibrationRecord).order_by(CalibrationRecord.next_due.asc())
        if instrument_id is not None:
            stmt = stmt.where(CalibrationRecord.instrument_id == instrument_id)
        if status_filter is not None:
            stmt = stmt.where(CalibrationRecord.status == status_filter)
        stmt = stmt.offset(skip).limit(limit)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def delete_calibration(self, instrument_id: str) -> bool:
        """Delete all calibration records for an instrument.

        Args:
            instrument_id: The instrument whose records should be deleted.

        Returns:
            True if at least one record was deleted, False if none existed.
        """
        records = await self._fetch_all(instrument_id)
        if not records:
            return False
        for record in records:
            await self._db.delete(record)
        await self._db.commit()
        return True

    # ------------------------------------------------------------------
    # Internal helpers.
    # ------------------------------------------------------------------

    async def _fetch_latest(self, instrument_id: str) -> CalibrationRecord | None:
        """Fetch the latest calibration record for an instrument."""
        stmt = (
            select(CalibrationRecord)
            .where(CalibrationRecord.instrument_id == instrument_id)
            .order_by(CalibrationRecord.next_due.desc())
            .limit(1)
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def _fetch_all(self, instrument_id: str) -> list[CalibrationRecord]:
        """Fetch all calibration records for an instrument."""
        stmt = (
            select(CalibrationRecord)
            .where(CalibrationRecord.instrument_id == instrument_id)
            .order_by(CalibrationRecord.next_due.desc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())


def _normalize(dt: datetime) -> datetime:
    """Normalize a datetime to naive-UTC for comparison.

    SQLite strips timezone info on read, so mixing tz-aware (``now``) and
    tz-naive (DB-read ``next_due``) datetimes raises TypeError. This helper
    converts both to naive UTC for safe comparison.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt
