"""Tests for the calibration CRUD API and the execution 409 block (T31).

Covers:
- POST /api/v1/calibrations - record calibration (create + update existing).
- GET /api/v1/calibrations - list with filters.
- GET /api/v1/calibrations/status?instrument_id=... - status check.
- GET /api/v1/calibrations/{instrument_id} - get latest (404 when missing).
- PUT /api/v1/calibrations/{instrument_id} - update.
- DELETE /api/v1/calibrations/{instrument_id} - delete.
- POST /api/v1/calibrations/check-expiry - bulk status refresh.
- HTTP 409 block: POST /api/v1/executions with an EXPIRED instrument in
  config.instrument_ids returns 409 and does not create an Execution row.
- HTTP 200 path: POST /api/v1/executions with a VALID instrument is allowed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from ate_cloud.models.calibration import CalibrationRecord
from ate_cloud.models.execution import Execution


def _sample_cal_data(
    instrument_id: str = "osc-001",
    interval_days: int = 365,
    days_ago: int = 10,
) -> dict[str, object]:
    """Return sample calibration creation payload relative to now."""
    last_cal = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return {
        "instrument_id": instrument_id,
        "last_calibration": last_cal,
        "interval_days": interval_days,
        "notes": "Annual calibration",
    }


# ---------------------------------------------------------------------------
# POST /api/v1/calibrations - record calibration.
# ---------------------------------------------------------------------------


class TestRecordCalibration:
    """Tests for POST /api/v1/calibrations."""

    @pytest.mark.asyncio
    async def test_create_returns_201(self, client: Any) -> None:
        """Recording a new calibration returns 201 with computed fields."""
        resp = await client.post("/api/v1/calibrations", json=_sample_cal_data())
        assert resp.status_code == 201
        data = resp.json()
        assert data["instrument_id"] == "osc-001"
        assert data["interval_days"] == 365
        assert data["status"] == "VALID"
        assert data["notes"] == "Annual calibration"
        assert "next_due" in data
        assert "id" in data

    @pytest.mark.asyncio
    async def test_create_expired_status(self, client: Any) -> None:
        """A calibration with next_due in the past has status EXPIRED."""
        data = _sample_cal_data(instrument_id="expired-inst", days_ago=400)
        resp = await client.post("/api/v1/calibrations", json=data)
        assert resp.status_code == 201
        assert resp.json()["status"] == "EXPIRED"

    @pytest.mark.asyncio
    async def test_create_expiring_status(self, client: Any) -> None:
        """A calibration with next_due within 7 days has status EXPIRING."""
        data = _sample_cal_data(instrument_id="expiring-inst", days_ago=363)
        resp = await client.post("/api/v1/calibrations", json=data)
        assert resp.status_code == 201
        assert resp.json()["status"] == "EXPIRING"

    @pytest.mark.asyncio
    async def test_update_existing_in_place(self, client: Any) -> None:
        """Recording a calibration for an existing instrument updates in place."""
        await client.post("/api/v1/calibrations", json=_sample_cal_data("dup-001"))
        # Second call with same instrument_id updates rather than creating a 2nd row.
        data = _sample_cal_data("dup-001", interval_days=180, days_ago=0)
        resp = await client.post("/api/v1/calibrations", json=data)
        assert resp.status_code == 201
        assert resp.json()["interval_days"] == 180

        # List should show only one record for dup-001.
        list_resp = await client.get("/api/v1/calibrations?instrument_id=dup-001")
        assert list_resp.json()["total"] == 1

    @pytest.mark.asyncio
    async def test_missing_required_field(self, client: Any) -> None:
        """Missing instrument_id returns 422."""
        data = _sample_cal_data()
        del data["instrument_id"]
        resp = await client.post("/api/v1/calibrations", json=data)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_interval_days(self, client: Any) -> None:
        """interval_days < 1 returns 422."""
        data = _sample_cal_data()
        data["interval_days"] = 0
        resp = await client.post("/api/v1/calibrations", json=data)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/calibrations - list.
# ---------------------------------------------------------------------------


class TestListCalibrations:
    """Tests for GET /api/v1/calibrations."""

    @pytest.mark.asyncio
    async def test_list_empty(self, client: Any) -> None:
        """List returns empty when no records exist."""
        resp = await client.get("/api/v1/calibrations")
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "total": 0}

    @pytest.mark.asyncio
    async def test_list_with_data(self, client: Any) -> None:
        """List returns all created records."""
        await client.post("/api/v1/calibrations", json=_sample_cal_data("a"))
        await client.post("/api/v1/calibrations", json=_sample_cal_data("b"))
        resp = await client.get("/api/v1/calibrations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2

    @pytest.mark.asyncio
    async def test_list_filter_by_instrument(self, client: Any) -> None:
        """List filters by instrument_id query param."""
        await client.post("/api/v1/calibrations", json=_sample_cal_data("inst-a"))
        await client.post("/api/v1/calibrations", json=_sample_cal_data("inst-b"))
        resp = await client.get("/api/v1/calibrations?instrument_id=inst-a")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["instrument_id"] == "inst-a"

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self, client: Any) -> None:
        """List filters by status query param."""
        await client.post("/api/v1/calibrations", json=_sample_cal_data("valid-i", days_ago=1))
        await client.post(
            "/api/v1/calibrations",
            json=_sample_cal_data("expired-i", days_ago=400),
        )
        resp = await client.get("/api/v1/calibrations?status=EXPIRED")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["status"] == "EXPIRED"


# ---------------------------------------------------------------------------
# GET /api/v1/calibrations/status - status check.
# ---------------------------------------------------------------------------


class TestStatusCheck:
    """Tests for GET /api/v1/calibrations/status."""

    @pytest.mark.asyncio
    async def test_status_unknown_when_no_record(self, client: Any) -> None:
        """Status endpoint returns UNKNOWN when no record exists."""
        resp = await client.get(
            "/api/v1/calibrations/status?instrument_id=nonexistent"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "UNKNOWN"
        assert data["next_due"] is None
        assert data["days_until_due"] is None
        assert data["record"] is None

    @pytest.mark.asyncio
    async def test_status_valid(self, client: Any) -> None:
        """Status endpoint returns VALID for a freshly calibrated instrument."""
        await client.post("/api/v1/calibrations", json=_sample_cal_data("valid-s", days_ago=1))
        resp = await client.get("/api/v1/calibrations/status?instrument_id=valid-s")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "VALID"
        assert data["days_until_due"] is not None
        assert data["days_until_due"] > 0
        assert data["record"] is not None

    @pytest.mark.asyncio
    async def test_status_expired_with_negative_days(self, client: Any) -> None:
        """Status endpoint returns EXPIRED with negative days_until_due."""
        await client.post(
            "/api/v1/calibrations",
            json=_sample_cal_data("expired-s", days_ago=400),
        )
        resp = await client.get("/api/v1/calibrations/status?instrument_id=expired-s")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "EXPIRED"
        assert data["days_until_due"] is not None
        assert data["days_until_due"] < 0


# ---------------------------------------------------------------------------
# GET /api/v1/calibrations/{instrument_id} - get latest.
# ---------------------------------------------------------------------------


class TestGetCalibration:
    """Tests for GET /api/v1/calibrations/{instrument_id}."""

    @pytest.mark.asyncio
    async def test_get_existing(self, client: Any) -> None:
        """Get returns the latest record for an instrument."""
        await client.post("/api/v1/calibrations", json=_sample_cal_data("get-001"))
        resp = await client.get("/api/v1/calibrations/get-001")
        assert resp.status_code == 200
        assert resp.json()["instrument_id"] == "get-001"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_404(self, client: Any) -> None:
        """Get returns 404 when no record exists."""
        resp = await client.get("/api/v1/calibrations/nonexistent")
        assert resp.status_code == 404
        assert "nonexistent" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# PUT /api/v1/calibrations/{instrument_id} - update.
# ---------------------------------------------------------------------------


class TestUpdateCalibration:
    """Tests for PUT /api/v1/calibrations/{instrument_id}."""

    @pytest.mark.asyncio
    async def test_update_interval(self, client: Any) -> None:
        """Updating interval_days recomputes next_due."""
        await client.post("/api/v1/calibrations", json=_sample_cal_data("upd-001"))
        resp = await client.put(
            "/api/v1/calibrations/upd-001",
            json={"interval_days": 90},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["interval_days"] == 90

    @pytest.mark.asyncio
    async def test_update_nonexistent_returns_404(self, client: Any) -> None:
        """Updating a nonexistent record returns 404."""
        resp = await client.put(
            "/api/v1/calibrations/nonexistent",
            json={"notes": "x"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v1/calibrations/{instrument_id}.
# ---------------------------------------------------------------------------


class TestDeleteCalibration:
    """Tests for DELETE /api/v1/calibrations/{instrument_id}."""

    @pytest.mark.asyncio
    async def test_delete_existing(self, client: Any) -> None:
        """Delete removes the record and returns 204."""
        await client.post("/api/v1/calibrations", json=_sample_cal_data("del-001"))
        resp = await client.delete("/api/v1/calibrations/del-001")
        assert resp.status_code == 204
        # Verify it's gone.
        get_resp = await client.get("/api/v1/calibrations/del-001")
        assert get_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, client: Any) -> None:
        """Deleting a nonexistent record returns 404."""
        resp = await client.delete("/api/v1/calibrations/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/calibrations/check-expiry - bulk refresh.
# ---------------------------------------------------------------------------


class TestCheckExpiry:
    """Tests for POST /api/v1/calibrations/check-expiry."""

    @pytest.mark.asyncio
    async def test_check_expiry_no_records(self, client: Any) -> None:
        """check-expiry returns updated=0 when no records exist."""
        resp = await client.post("/api/v1/calibrations/check-expiry")
        assert resp.status_code == 200
        assert resp.json()["updated"] == 0

    @pytest.mark.asyncio
    async def test_check_expiry_refreshes_stale(
        self, client: Any, db_session: Any
    ) -> None:
        """check-expiry recomputes statuses for records with stale status."""
        # Create a VALID record.
        await client.post("/api/v1/calibrations", json=_sample_cal_data("stale-001", days_ago=1))

        # Manually flip its status to EXPIRED (simulating drift).
        from sqlalchemy import update as sa_update

        await db_session.execute(
            sa_update(CalibrationRecord)
            .where(CalibrationRecord.instrument_id == "stale-001")
            .values(status="EXPIRED")
        )
        await db_session.flush()

        resp = await client.post("/api/v1/calibrations/check-expiry")
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1

        # Verify the status is now VALID.
        status_resp = await client.get(
            "/api/v1/calibrations/status?instrument_id=stale-001"
        )
        assert status_resp.json()["status"] == "VALID"


# ---------------------------------------------------------------------------
# HTTP 409 execution block (T31 core verification).
# ---------------------------------------------------------------------------


class TestExecutionCalibrationBlock:
    """Tests that POST /api/v1/executions is blocked (409) when an instrument
    in config.instrument_ids is EXPIRED, and allowed when VALID/none.
    """

    @pytest.mark.asyncio
    async def test_expired_instrument_blocks_execution(
        self, client: Any, db_session: Any
    ) -> None:
        """An EXPIRED instrument in config.instrument_ids returns 409 and does
        not create an Execution row."""
        # Create an EXPIRED calibration record for an instrument.
        await client.post(
            "/api/v1/calibrations",
            json=_sample_cal_data("block-001", days_ago=400),
        )

        # Attempt to start an execution that uses that instrument.
        resp = await client.post(
            "/api/v1/executions",
            json={
                "sequence_id": "seq-does-not-matter",
                "config": {"instrument_ids": ["block-001"]},
            },
        )
        assert resp.status_code == 409
        assert "expired" in resp.json()["detail"].lower()
        assert "block-001" in resp.json()["detail"]

        # Verify no Execution row was created.
        result = await db_session.execute(select(Execution))
        assert len(list(result.scalars().all())) == 0

    @pytest.mark.asyncio
    async def test_valid_instrument_allows_execution(
        self, client: Any, db_session: Any
    ) -> None:
        """A VALID instrument in config.instrument_ids does NOT block execution
        (the request proceeds past the calibration gate)."""
        await client.post(
            "/api/v1/calibrations",
            json=_sample_cal_data("allow-001", days_ago=1),
        )

        resp = await client.post(
            "/api/v1/executions",
            json={
                "sequence_id": "seq-does-not-matter",
                "config": {"instrument_ids": ["allow-001"]},
            },
        )
        # The execution is created (201) even though the sequence doesn't exist
        # - dispatch failure is caught later and returns 503, but the calibration
        # gate passed. We accept 201 or 503 (dispatch may fail in tests without
        # NATS), but NOT 409.
        assert resp.status_code in (201, 503)
        assert resp.status_code != 409

    @pytest.mark.asyncio
    async def test_no_instrument_ids_allows_execution(
        self, client: Any
    ) -> None:
        """No instrument_ids in config means no calibration check (opt-in)."""
        resp = await client.post(
            "/api/v1/executions",
            json={"sequence_id": "seq-does-not-matter"},
        )
        assert resp.status_code in (201, 503)
        assert resp.status_code != 409

    @pytest.mark.asyncio
    async def test_unknown_instrument_allows_execution(
        self, client: Any
    ) -> None:
        """An instrument with no calibration record is allowed (opt-in)."""
        resp = await client.post(
            "/api/v1/executions",
            json={
                "sequence_id": "seq-does-not-matter",
                "config": {"instrument_ids": ["never-calibrated"]},
            },
        )
        assert resp.status_code in (201, 503)
        assert resp.status_code != 409

    @pytest.mark.asyncio
    async def test_mixed_expired_and_valid_blocks(
        self, client: Any
    ) -> None:
        """If any instrument in the list is EXPIRED, execution is blocked."""
        await client.post(
            "/api/v1/calibrations",
            json=_sample_cal_data("mix-valid", days_ago=1),
        )
        await client.post(
            "/api/v1/calibrations",
            json=_sample_cal_data("mix-expired", days_ago=400),
        )
        resp = await client.post(
            "/api/v1/executions",
            json={
                "sequence_id": "seq-does-not-matter",
                "config": {"instrument_ids": ["mix-valid", "mix-expired"]},
            },
        )
        assert resp.status_code == 409
        assert "mix-expired" in resp.json()["detail"]
