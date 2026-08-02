"""Unit tests for CalibrationManager.

Covers:
- compute_status (pure function) - VALID/EXPIRING/EXPIRED boundaries.
- record_calibration - create new, update existing (idempotent per instrument).
- check_status - returns None when no record, recomputes stale status.
- is_expired - True only when EXPIRED, False for no-record / VALID / EXPIRING.
- check_expiry - bulk refresh of stale statuses.
- update_calibration - partial updates, next_due recomputation.
- list_records / delete_calibration.
- timezone-naive vs aware datetime handling.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ate_cloud.models import Base
from ate_cloud.schemas.calibration import (
    EXPIRING_WINDOW_DAYS,
    CalibrationCreate,
    CalibrationUpdate,
)
from ate_cloud.services.calibration_manager import CalibrationManager


# Local in-memory SQLite engine fixture for DB-backed unit tests.
# The cloud conftest.py test_engine fixture is not available under tests/unit/.
@pytest.fixture
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Create an in-memory SQLite engine with all tables created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def _make_session_factory(test_engine: Any) -> async_sessionmaker[AsyncSession]:
    """Create a session factory from a test engine (mirrors health_monitor tests)."""
    return async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


# ---------------------------------------------------------------------------
# compute_status - pure function, no DB needed.
# ---------------------------------------------------------------------------


class TestComputeStatus:
    """Tests for CalibrationManager.compute_status (pure function)."""

    def test_valid_when_far_from_due(self) -> None:
        """Status is VALID when next_due is more than 7 days away."""
        now = datetime.now(UTC)
        next_due = now + timedelta(days=30)
        assert CalibrationManager.compute_status(next_due, now=now) == "VALID"

    def test_expiring_within_7_days(self) -> None:
        """Status is EXPIRING when next_due is within 7 days (exclusive of due)."""
        now = datetime.now(UTC)
        next_due = now + timedelta(days=3)
        assert CalibrationManager.compute_status(next_due, now=now) == "EXPIRING"

    def test_expiring_at_threshold_boundary(self) -> None:
        """Status is EXPIRING exactly 7 days before due."""
        now = datetime.now(UTC)
        next_due = now + timedelta(days=EXPIRING_WINDOW_DAYS)
        assert CalibrationManager.compute_status(next_due, now=now) == "EXPIRING"

    def test_valid_just_outside_expiring_window(self) -> None:
        """Status is VALID when next_due is just over 7 days away."""
        now = datetime.now(UTC)
        next_due = now + timedelta(days=EXPIRING_WINDOW_DAYS, seconds=1)
        assert CalibrationManager.compute_status(next_due, now=now) == "VALID"

    def test_expired_at_due_date(self) -> None:
        """Status is EXPIRED when now == next_due."""
        now = datetime.now(UTC)
        next_due = now
        assert CalibrationManager.compute_status(next_due, now=now) == "EXPIRED"

    def test_expired_past_due(self) -> None:
        """Status is EXPIRED when now is past next_due."""
        now = datetime.now(UTC)
        next_due = now - timedelta(days=1)
        assert CalibrationManager.compute_status(next_due, now=now) == "EXPIRED"

    def test_naive_datetimes_handled(self) -> None:
        """Naive datetimes (no tzinfo) are compared correctly."""
        now = datetime(2026, 8, 2, 12, 0, 0)  # naive
        next_due = datetime(2026, 9, 2, 12, 0, 0)  # naive
        assert CalibrationManager.compute_status(next_due, now=now) == "VALID"

    def test_mixed_tz_naive_aware(self) -> None:
        """Aware now + naive next_due does not raise TypeError."""
        now = datetime.now(UTC)
        next_due = (now + timedelta(days=3)).replace(tzinfo=None)  # naive
        assert CalibrationManager.compute_status(next_due, now=now) == "EXPIRING"

    def test_custom_expiring_window(self) -> None:
        """Custom expiring_window_days is honored."""
        now = datetime.now(UTC)
        next_due = now + timedelta(days=15)
        # Default window (7d) -> VALID; 30d window -> EXPIRING.
        assert CalibrationManager.compute_status(next_due, now=now) == "VALID"
        assert (
            CalibrationManager.compute_status(
                next_due, now=now, expiring_window_days=30
            )
            == "EXPIRING"
        )

    def test_default_now_when_none(self) -> None:
        """compute_status uses datetime.now(UTC) when now is None."""
        next_due = datetime.now(UTC) + timedelta(days=365)
        # Just verify it does not raise and returns VALID for far-future due.
        assert CalibrationManager.compute_status(next_due) == "VALID"


# ---------------------------------------------------------------------------
# record_calibration - DB-backed.
# ---------------------------------------------------------------------------


class TestRecordCalibration:
    """Tests for CalibrationManager.record_calibration."""

    @pytest.mark.asyncio
    async def test_create_new_record(self, test_engine: Any) -> None:
        """record_calibration creates a new record with computed next_due + status."""
        sf = _make_session_factory(test_engine)
        last_cal = datetime.now(UTC) - timedelta(days=10)
        data = CalibrationCreate(
            instrument_id="osc-001",
            last_calibration=last_cal,
            interval_days=365,
            notes="Annual calibration",
        )
        async with sf() as session:
            manager = CalibrationManager(session)
            result = await manager.record_calibration(data)

        assert result.instrument_id == "osc-001"
        assert result.interval_days == 365
        assert result.notes == "Annual calibration"
        # next_due = last_calibration + 365 days
        expected_due = last_cal + timedelta(days=365)
        assert _to_naive(result.next_due) == _to_naive(expected_due)
        # 355 days remain -> VALID
        assert result.status == "VALID"

    @pytest.mark.asyncio
    async def test_update_existing_record(self, test_engine: Any) -> None:
        """record_calibration updates the latest record instead of creating a duplicate."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            await manager.record_calibration(CalibrationCreate(
                instrument_id="dmm-001",
                last_calibration=datetime.now(UTC) - timedelta(days=400),
                interval_days=365,
            ))

        # Second record_calibration for same instrument updates in place.
        new_cal = datetime.now(UTC)
        async with sf() as session:
            manager = CalibrationManager(session)
            result = await manager.record_calibration(CalibrationCreate(
                instrument_id="dmm-001",
                last_calibration=new_cal,
                interval_days=180,
                notes="Recalibrated",
            ))

        assert result.interval_days == 180
        assert result.notes == "Recalibrated"
        expected_due = new_cal + timedelta(days=180)
        assert _to_naive(result.next_due) == _to_naive(expected_due)

        # Only one record should exist for the instrument.
        from sqlalchemy import select

        from ate_cloud.models.calibration import CalibrationRecord

        async with sf() as session:
            stmt = select(CalibrationRecord).where(
                CalibrationRecord.instrument_id == "dmm-001"
            )
            res = await session.execute(stmt)
            records = res.scalars().all()
        assert len(records) == 1

    @pytest.mark.asyncio
    async def test_status_expiring_on_create(self, test_engine: Any) -> None:
        """A record created with next_due within 7 days has status EXPIRING."""
        sf = _make_session_factory(test_engine)
        last_cal = datetime.now(UTC) - timedelta(days=363)
        async with sf() as session:
            manager = CalibrationManager(session)
            result = await manager.record_calibration(CalibrationCreate(
                instrument_id="psu-001",
                last_calibration=last_cal,
                interval_days=365,
            ))
        # next_due is ~2 days away -> EXPIRING
        assert result.status == "EXPIRING"

    @pytest.mark.asyncio
    async def test_status_expired_on_create(self, test_engine: Any) -> None:
        """A record created with next_due in the past has status EXPIRED."""
        sf = _make_session_factory(test_engine)
        last_cal = datetime.now(UTC) - timedelta(days=400)
        async with sf() as session:
            manager = CalibrationManager(session)
            result = await manager.record_calibration(CalibrationCreate(
                instrument_id="rf-001",
                last_calibration=last_cal,
                interval_days=365,
            ))
        assert result.status == "EXPIRED"


# ---------------------------------------------------------------------------
# check_status / is_expired.
# ---------------------------------------------------------------------------


class TestCheckStatus:
    """Tests for CalibrationManager.check_status and is_expired."""

    @pytest.mark.asyncio
    async def test_check_status_returns_none_when_no_record(
        self, test_engine: Any
    ) -> None:
        """check_status returns None when no calibration record exists."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            result = await manager.check_status("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_is_expired_false_when_no_record(self, test_engine: Any) -> None:
        """is_expired returns False when no calibration record exists."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            result = await manager.is_expired("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_check_status_recomputes_stale_status(self, test_engine: Any) -> None:
        """check_status recomputes status if the stored value is stale."""
        sf = _make_session_factory(test_engine)
        # Create a record that was VALID when stored (next_due far future).
        async with sf() as session:
            manager = CalibrationManager(session)
            await manager.record_calibration(CalibrationCreate(
                instrument_id="scope-001",
                last_calibration=datetime.now(UTC) - timedelta(days=1),
                interval_days=365,
            ))

        # Manually flip the stored status to EXPIRED (simulating stale data).
        from sqlalchemy import update as sa_update

        from ate_cloud.models.calibration import CalibrationRecord

        async with sf() as session:
            await session.execute(
                sa_update(CalibrationRecord)
                .where(CalibrationRecord.instrument_id == "scope-001")
                .values(status="EXPIRED")
            )
            await session.commit()

        # check_status should recompute to VALID and update the row.
        async with sf() as session:
            manager = CalibrationManager(session)
            result = await manager.check_status("scope-001")
        assert result is not None
        assert result.status == "VALID"

    @pytest.mark.asyncio
    async def test_is_expired_true_for_expired_instrument(
        self, test_engine: Any
    ) -> None:
        """is_expired returns True for an instrument past its due date."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            await manager.record_calibration(CalibrationCreate(
                instrument_id="expired-001",
                last_calibration=datetime.now(UTC) - timedelta(days=400),
                interval_days=365,
            ))
        async with sf() as session:
            manager = CalibrationManager(session)
            assert await manager.is_expired("expired-001") is True

    @pytest.mark.asyncio
    async def test_is_expired_false_for_valid_instrument(
        self, test_engine: Any
    ) -> None:
        """is_expired returns False for a VALID instrument."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            await manager.record_calibration(CalibrationCreate(
                instrument_id="valid-001",
                last_calibration=datetime.now(UTC),
                interval_days=365,
            ))
        async with sf() as session:
            manager = CalibrationManager(session)
            assert await manager.is_expired("valid-001") is False

    @pytest.mark.asyncio
    async def test_is_expired_false_for_expiring_instrument(
        self, test_engine: Any
    ) -> None:
        """is_expired returns False for an EXPIRING (not yet EXPIRED) instrument."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            await manager.record_calibration(CalibrationCreate(
                instrument_id="expiring-001",
                last_calibration=datetime.now(UTC) - timedelta(days=363),
                interval_days=365,
            ))
        async with sf() as session:
            manager = CalibrationManager(session)
            assert await manager.is_expired("expiring-001") is False


# ---------------------------------------------------------------------------
# check_expiry - bulk refresh.
# ---------------------------------------------------------------------------


class TestCheckExpiry:
    """Tests for CalibrationManager.check_expiry (bulk refresh)."""

    @pytest.mark.asyncio
    async def test_no_records_returns_zero(self, test_engine: Any) -> None:
        """check_expiry returns 0 when there are no records."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            changed = await manager.check_expiry()
        assert changed == 0

    @pytest.mark.asyncio
    async def test_updates_stale_statuses(self, test_engine: Any) -> None:
        """check_expiry recomputes statuses for records whose stored value is stale."""
        sf = _make_session_factory(test_engine)
        # Create one VALID (far future) and one EXPIRED (past due) record.
        async with sf() as session:
            manager = CalibrationManager(session)
            await manager.record_calibration(CalibrationCreate(
                instrument_id="valid-002",
                last_calibration=datetime.now(UTC),
                interval_days=365,
            ))
            await manager.record_calibration(CalibrationCreate(
                instrument_id="expired-002",
                last_calibration=datetime.now(UTC) - timedelta(days=400),
                interval_days=365,
            ))

        # Flip both statuses to something wrong (EXPIRING) to simulate drift.
        from sqlalchemy import update as sa_update

        from ate_cloud.models.calibration import CalibrationRecord

        async with sf() as session:
            await session.execute(
                sa_update(CalibrationRecord).values(status="EXPIRING")
            )
            await session.commit()

        # check_expiry should detect both changed (EXPIRING -> VALID / EXPIRED).
        async with sf() as session:
            manager = CalibrationManager(session)
            changed = await manager.check_expiry()
        assert changed == 2

        # Verify the DB now has correct statuses.
        from sqlalchemy import select

        async with sf() as session:
            res = await session.execute(
                select(CalibrationRecord).order_by(CalibrationRecord.instrument_id)
            )
            records = {r.instrument_id: r.status for r in res.scalars().all()}
        assert records["expired-002"] == "EXPIRED"
        assert records["valid-002"] == "VALID"

    @pytest.mark.asyncio
    async def test_no_change_when_statuses_correct(
        self, test_engine: Any
    ) -> None:
        """check_expiry returns 0 when all statuses are already correct."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            await manager.record_calibration(CalibrationCreate(
                instrument_id="fresh-001",
                last_calibration=datetime.now(UTC),
                interval_days=365,
            ))
        # Statuses are already correct (computed at creation). check_expiry -> 0.
        async with sf() as session:
            manager = CalibrationManager(session)
            changed = await manager.check_expiry()
        assert changed == 0


# ---------------------------------------------------------------------------
# update_calibration.
# ---------------------------------------------------------------------------


class TestUpdateCalibration:
    """Tests for CalibrationManager.update_calibration."""

    @pytest.mark.asyncio
    async def test_update_returns_none_when_no_record(
        self, test_engine: Any
    ) -> None:
        """update_calibration returns None when no record exists."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            result = await manager.update_calibration(
                "nonexistent", CalibrationUpdate(notes="updated")
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_update_interval_recomputes_next_due(
        self, test_engine: Any
    ) -> None:
        """Updating interval_days recomputes next_due from last_calibration."""
        sf = _make_session_factory(test_engine)
        last_cal = datetime.now(UTC) - timedelta(days=10)
        async with sf() as session:
            manager = CalibrationManager(session)
            await manager.record_calibration(CalibrationCreate(
                instrument_id="dmm-002",
                last_calibration=last_cal,
                interval_days=365,
            ))

        async with sf() as session:
            manager = CalibrationManager(session)
            result = await manager.update_calibration(
                "dmm-002", CalibrationUpdate(interval_days=90)
            )
        assert result is not None
        assert result.interval_days == 90
        expected_due = last_cal + timedelta(days=90)
        assert _to_naive(result.next_due) == _to_naive(expected_due)

    @pytest.mark.asyncio
    async def test_update_last_calibration_recomputes_next_due(
        self, test_engine: Any
    ) -> None:
        """Updating last_calibration recomputes next_due."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            await manager.record_calibration(CalibrationCreate(
                instrument_id="scope-002",
                last_calibration=datetime.now(UTC) - timedelta(days=400),
                interval_days=365,
            ))

        new_cal = datetime.now(UTC)
        async with sf() as session:
            manager = CalibrationManager(session)
            result = await manager.update_calibration(
                "scope-002", CalibrationUpdate(last_calibration=new_cal)
            )
        assert result is not None
        expected_due = new_cal + timedelta(days=365)
        assert _to_naive(result.next_due) == _to_naive(expected_due)
        # Freshly calibrated -> VALID
        assert result.status == "VALID"

    @pytest.mark.asyncio
    async def test_update_notes_only(self, test_engine: Any) -> None:
        """Updating only notes does not change next_due or status."""
        sf = _make_session_factory(test_engine)
        last_cal = datetime.now(UTC)
        async with sf() as session:
            manager = CalibrationManager(session)
            created = await manager.record_calibration(CalibrationCreate(
                instrument_id="psu-002",
                last_calibration=last_cal,
                interval_days=365,
                notes="initial",
            ))

        async with sf() as session:
            manager = CalibrationManager(session)
            result = await manager.update_calibration(
                "psu-002", CalibrationUpdate(notes="updated notes")
            )
        assert result is not None
        assert result.notes == "updated notes"
        # next_due unchanged
        assert _to_naive(result.next_due) == _to_naive(created.next_due)


# ---------------------------------------------------------------------------
# list_records / delete_calibration.
# ---------------------------------------------------------------------------


class TestListAndDelete:
    """Tests for list_records and delete_calibration."""

    @pytest.mark.asyncio
    async def test_list_all_records(self, test_engine: Any) -> None:
        """list_records returns all records ordered by next_due asc."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            await manager.record_calibration(CalibrationCreate(
                instrument_id="a-late",
                last_calibration=datetime.now(UTC) - timedelta(days=1),
                interval_days=365,
            ))
            await manager.record_calibration(CalibrationCreate(
                instrument_id="b-early",
                last_calibration=datetime.now(UTC) - timedelta(days=100),
                interval_days=365,
            ))

        async with sf() as session:
            manager = CalibrationManager(session)
            records = await manager.list_records()
        # Ordered by next_due asc -> b-early (older last_cal) comes first.
        assert len(records) == 2
        assert records[0].instrument_id == "b-early"
        assert records[1].instrument_id == "a-late"

    @pytest.mark.asyncio
    async def test_list_filter_by_instrument(self, test_engine: Any) -> None:
        """list_records filters by instrument_id."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            await manager.record_calibration(CalibrationCreate(
                instrument_id="inst-a", interval_days=365
            ))
            await manager.record_calibration(CalibrationCreate(
                instrument_id="inst-b", interval_days=365
            ))
        async with sf() as session:
            manager = CalibrationManager(session)
            records = await manager.list_records(instrument_id="inst-a")
        assert len(records) == 1
        assert records[0].instrument_id == "inst-a"

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self, test_engine: Any) -> None:
        """list_records filters by status."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            await manager.record_calibration(CalibrationCreate(
                instrument_id="valid-003", interval_days=365
            ))
            await manager.record_calibration(CalibrationCreate(
                instrument_id="expired-003",
                last_calibration=datetime.now(UTC) - timedelta(days=400),
                interval_days=365,
            ))
        async with sf() as session:
            manager = CalibrationManager(session)
            records = await manager.list_records(status_filter="EXPIRED")
        assert len(records) == 1
        assert records[0].instrument_id == "expired-003"

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_no_record(
        self, test_engine: Any
    ) -> None:
        """delete_calibration returns False when no record exists."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            result = await manager.delete_calibration("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_removes_records(self, test_engine: Any) -> None:
        """delete_calibration removes all records for an instrument."""
        sf = _make_session_factory(test_engine)
        async with sf() as session:
            manager = CalibrationManager(session)
            await manager.record_calibration(CalibrationCreate(
                instrument_id="del-001", interval_days=365
            ))
        async with sf() as session:
            manager = CalibrationManager(session)
            result = await manager.delete_calibration("del-001")
        assert result is True
        # Verify it's gone.
        async with sf() as session:
            manager = CalibrationManager(session)
            assert await manager.check_status("del-001") is None


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _to_naive(dt: datetime) -> datetime:
    """Convert a datetime to naive UTC for SQLite-safe comparison."""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt
