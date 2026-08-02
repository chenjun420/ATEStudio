"""Tests for Dashboard API endpoints (Todo 39).

Verifies:
- GET /api/v1/dashboard/summary — returns active_workers, execution counts,
  pass_rate, total_faults with graceful degradation when NATS/Qdrant unavailable.
- GET /api/v1/dashboard/stations — returns station list from WorkerRegistryService.
- GET /api/v1/dashboard/faults — returns trend + top_faults from Qdrant.
- GET /api/v1/dashboard/executions — returns today's execution breakdown.

The conftest provides `client` (async httpx), `db_session`, and `app`
fixtures. The app has no NATS client (app.state.nc is unset), so
worker/fault endpoints exercise the graceful-degradation path.
For Qdrant/fault tests, a mock failure_indexer is attached to app.state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from ate_cloud.models.execution import Execution


def _make_execution(
    *,
    run_id: str,
    status: str = "COMPLETED",
    seq_id: str | None = "seq-1",
    created_at: datetime | None = None,
) -> Execution:
    """Build an Execution row for test data."""
    return Execution(
        id=run_id,
        sequence_id=seq_id,
        status=status,
        config=None,
        result=None,
        error=None,
        started_at=created_at,
        completed_at=created_at,
        created_at=created_at or datetime.now(UTC),
        updated_at=created_at or datetime.now(UTC),
    )


class TestDashboardSummary:
    """Tests for GET /api/v1/dashboard/summary."""

    @pytest.mark.asyncio
    async def test_summary_returns_zero_when_no_data(self, client) -> None:
        """Summary returns zeros when DB is empty and NATS/Qdrant unavailable."""
        response = await client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        data = response.json()
        assert data["active_workers"] == 0
        assert data["total_executions_today"] == 0
        assert data["completed_today"] == 0
        assert data["failed_today"] == 0
        assert data["pass_rate"] == 0.0
        assert data["total_faults"] == 0

    @pytest.mark.asyncio
    async def test_summary_counts_today_executions(self, db_session, client) -> None:
        """Summary counts only executions created today."""
        now = datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # 2 completed, 1 failed today
        db_session.add(_make_execution(run_id="r1", status="COMPLETED", created_at=now))
        db_session.add(_make_execution(run_id="r2", status="COMPLETED", created_at=now))
        db_session.add(_make_execution(run_id="r3", status="FAILED", created_at=now))
        # 1 execution from yesterday (should NOT count)
        yesterday = today_start - timedelta(hours=2)
        db_session.add(_make_execution(run_id="r4", status="COMPLETED", created_at=yesterday))
        await db_session.flush()

        response = await client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        data = response.json()
        assert data["total_executions_today"] == 3
        assert data["completed_today"] == 2
        assert data["failed_today"] == 1
        assert data["pass_rate"] == 66.7  # 2/(2+1)*100

    @pytest.mark.asyncio
    async def test_summary_pass_rate_zero_when_no_terminal(self, db_session, client) -> None:
        """Pass rate is 0.0 when no completed/failed executions."""
        now = datetime.now(UTC)
        db_session.add(_make_execution(run_id="r1", status="PENDING", created_at=now))
        db_session.add(_make_execution(run_id="r2", status="RUNNING", created_at=now))
        await db_session.flush()

        response = await client.get("/api/v1/dashboard/summary")
        data = response.json()
        assert data["pass_rate"] == 0.0
        assert data["total_executions_today"] == 2

    @pytest.mark.asyncio
    async def test_summary_with_mock_failure_indexer(self, app, db_session, client) -> None:
        """Summary reads total_faults from failure_indexer when available."""
        now = datetime.now(UTC)
        db_session.add(_make_execution(run_id="r1", status="COMPLETED", created_at=now))
        await db_session.flush()

        # Attach a mock failure indexer to app.state
        mock_qdrant = MagicMock()
        mock_collection_info = MagicMock()
        mock_collection_info.points_count = 42
        mock_qdrant.get_collection.return_value = mock_collection_info

        mock_indexer = MagicMock()
        mock_indexer._qdrant_client = mock_qdrant
        mock_indexer._collection_name = "ate_failures"
        app.state.failure_indexer = mock_indexer

        response = await client.get("/api/v1/dashboard/summary")
        data = response.json()
        assert data["total_faults"] == 42

        # Cleanup
        del app.state.failure_indexer


class TestDashboardStations:
    """Tests for GET /api/v1/dashboard/stations."""

    @pytest.mark.asyncio
    async def test_stations_returns_empty_when_no_nats(self, client) -> None:
        """Stations endpoint returns empty list when NATS is unavailable."""
        response = await client.get("/api/v1/dashboard/stations")
        assert response.status_code == 200
        data = response.json()
        assert data["stations"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_stations_returns_workers_when_nats_available(self, app, client) -> None:
        """Stations endpoint returns worker list when NATS client is set."""
        from unittest.mock import AsyncMock

        from ate_cloud.schemas.worker import WorkerInfo

        mock_worker = WorkerInfo(
            worker_id="worker-1",
            hostname="station-a",
            capabilities=["measure", "calibrate"],
            max_concurrent_tasks=4,
            current_tasks=2,
            last_heartbeat=datetime.now(UTC),
        )

        mock_nc = MagicMock()
        app.state.nc = mock_nc

        with patch(
            "ate_cloud.api.v1.dashboard.WorkerRegistryService"
        ) as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.list_workers = AsyncMock(return_value=[mock_worker])

            response = await client.get("/api/v1/dashboard/stations")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["stations"][0]["worker_id"] == "worker-1"
        assert data["stations"][0]["hostname"] == "station-a"
        assert data["stations"][0]["status"] == "online"

        del app.state.nc


class TestDashboardFaults:
    """Tests for GET /api/v1/dashboard/faults."""

    @pytest.mark.asyncio
    async def test_faults_returns_empty_when_no_indexer(self, client) -> None:
        """Faults endpoint returns empty lists when no failure_indexer."""
        response = await client.get("/api/v1/dashboard/faults")
        assert response.status_code == 200
        data = response.json()
        assert data["trend"] == []
        assert data["top_faults"] == []

    @pytest.mark.asyncio
    async def test_faults_returns_data_from_qdrant(self, app, client) -> None:
        """Faults endpoint returns trend + top_faults from Qdrant scroll."""
        now = datetime.now(UTC)

        # Mock Qdrant scroll results
        mock_point1 = MagicMock()
        mock_point1.payload = {
            "failed_step_name": "voltage_check",
            "error_message": "Over voltage",
            "timestamp": now.isoformat(),
        }
        mock_point2 = MagicMock()
        mock_point2.payload = {
            "failed_step_name": "voltage_check",
            "error_message": "Over voltage",
            "timestamp": now.isoformat(),
        }
        mock_point3 = MagicMock()
        mock_point3.payload = {
            "failed_step_name": "current_test",
            "error_message": "Short circuit",
            "timestamp": now.isoformat(),
        }

        mock_qdrant = MagicMock()
        # First scroll returns 3 points + offset=None (done)
        mock_qdrant.scroll.return_value = ([mock_point1, mock_point2, mock_point3], None)

        mock_indexer = MagicMock()
        mock_indexer._qdrant_client = mock_qdrant
        mock_indexer._collection_name = "ate_failures"
        app.state.failure_indexer = mock_indexer

        response = await client.get("/api/v1/dashboard/faults")
        assert response.status_code == 200
        data = response.json()

        # Top faults: voltage_check=2, current_test=1
        assert len(data["top_faults"]) == 2
        assert data["top_faults"][0]["category"] == "voltage_check"
        assert data["top_faults"][0]["count"] == 2
        assert data["top_faults"][1]["category"] == "current_test"
        assert data["top_faults"][1]["count"] == 1

        # Trend should have 25 hourly buckets (0..24 hours)
        assert len(data["trend"]) == 25
        # At least one bucket should have count > 0 (the current hour)
        assert any(b["count"] > 0 for b in data["trend"])

        del app.state.failure_indexer


class TestDashboardExecutions:
    """Tests for GET /api/v1/dashboard/executions."""

    @pytest.mark.asyncio
    async def test_executions_returns_empty_when_no_data(self, client) -> None:
        """Executions endpoint returns zeros and empty lists when no data."""
        response = await client.get("/api/v1/dashboard/executions")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["by_status"] == {}
        assert data["recent"] == []

    @pytest.mark.asyncio
    async def test_executions_breakdown_by_status(self, db_session, client) -> None:
        """Executions endpoint groups by status and returns recent list."""
        now = datetime.now(UTC)

        db_session.add(_make_execution(run_id="r1", status="COMPLETED", created_at=now))
        db_session.add(_make_execution(run_id="r2", status="COMPLETED", created_at=now))
        db_session.add(_make_execution(run_id="r3", status="FAILED", created_at=now))
        db_session.add(_make_execution(run_id="r4", status="RUNNING", created_at=now))
        db_session.add(_make_execution(run_id="r5", status="PENDING", created_at=now))
        await db_session.flush()

        response = await client.get("/api/v1/dashboard/executions")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 5
        assert data["by_status"]["COMPLETED"] == 2
        assert data["by_status"]["FAILED"] == 1
        assert data["by_status"]["RUNNING"] == 1
        assert data["by_status"]["PENDING"] == 1

        # Recent list should have all 5, ordered by created_at desc
        assert len(data["recent"]) == 5

    @pytest.mark.asyncio
    async def test_executions_excludes_yesterday(self, db_session, client) -> None:
        """Executions endpoint only counts today's records."""
        now = datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = today_start - timedelta(hours=2)

        db_session.add(_make_execution(run_id="r1", status="COMPLETED", created_at=now))
        db_session.add(_make_execution(run_id="r2", status="COMPLETED", created_at=yesterday))
        await db_session.flush()

        response = await client.get("/api/v1/dashboard/executions")
        data = response.json()
        assert data["total"] == 1
        assert len(data["recent"]) == 1
        assert data["recent"][0]["id"] == "r1"

    @pytest.mark.asyncio
    async def test_executions_recent_limited_to_10(self, db_session, client) -> None:
        """Recent executions list is capped at 10 entries."""
        now = datetime.now(UTC)
        for i in range(15):
            db_session.add(
                _make_execution(
                    run_id=f"r{i:02d}",
                    status="COMPLETED",
                    created_at=now - timedelta(seconds=i),
                )
            )
        await db_session.flush()

        response = await client.get("/api/v1/dashboard/executions")
        data = response.json()
        assert data["total"] == 15
        assert len(data["recent"]) == 10
