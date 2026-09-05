"""Tests for HealthMonitorService and the worker history API endpoint (T11).

Verifies:
1. check_once — online workers persisted with status "online".
2. check_once — offline workers (stale heartbeat >30s) persisted with status "offline".
3. check_once — empty KV produces no records.
4. check_once — missing bucket produces no records, no error.
5. check_once — skips non-worker keys.
6. check_once — handles malformed metadata gracefully.
7. start/stop lifecycle — task created and cancelled.
8. API endpoint GET /workers/{id}/history — returns records ordered by recorded_at DESC.
9. API endpoint — limit query param limits results.
10. API endpoint — nonexistent worker returns empty list.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.errors import KeyNotFoundError, NoKeysError, NotFoundError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ate_cloud.models.worker_heartbeat import WorkerHeartbeat
from ate_cloud.services.health_monitor import (
    WORKER_KV_BUCKET,
    HealthMonitorService,
)

# ---------------------------------------------------------------------------
# Helpers (mirroring test_worker_registry.py patterns)
# ---------------------------------------------------------------------------


class FakeKVEntry:
    """Mimics nats-py KeyValue.Entry for testing."""

    def __init__(
        self,
        key: str,
        value: bytes | None,
        revision: int = 1,
        created: datetime | None = None,
    ) -> None:
        self.bucket = WORKER_KV_BUCKET
        self.key = key
        self.value = value
        self.revision = revision
        self.delta = 0
        self.created = created
        self.operation = None


def _worker_metadata(
    hostname: str = "edge-001",
    capabilities: list[str] | None = None,
    max_concurrent: int = 1,
    current: int = 0,
) -> dict[str, Any]:
    """Build worker metadata matching JetStreamWorker._worker_metadata()."""
    return {
        "hostname": hostname,
        "capabilities": capabilities or ["script_execution"],
        "max_concurrent_tasks": max_concurrent,
        "current_tasks": current,
    }


def _make_mock_kv(entries: dict[str, FakeKVEntry] | None = None) -> MagicMock:
    """Build a mock KV store matching nats-py KeyValue API."""
    kv = MagicMock()
    _entries: dict[str, FakeKVEntry] = entries or {}

    async def _get(key: str) -> Any:
        if key in _entries:
            return _entries[key]
        raise KeyNotFoundError

    async def _keys() -> list[str]:
        if not _entries:
            raise NoKeysError
        return list(_entries.keys())

    kv.get = AsyncMock(side_effect=_get)
    kv.keys = AsyncMock(side_effect=_keys)
    return kv


def _make_mock_nc(kv: MagicMock | None = None, bucket_exists: bool = True) -> MagicMock:
    """Build a mock NATS client + JetStream context."""
    mock_js = MagicMock()
    if bucket_exists:
        mock_js.key_value = AsyncMock(return_value=kv)
    else:
        mock_js.key_value = AsyncMock(side_effect=NotFoundError)
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc


def _make_session_factory(
    test_engine: Any,
) -> async_sessionmaker[AsyncSession]:
    """Create a session factory from a test engine."""
    return async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


# ---------------------------------------------------------------------------
# HealthMonitorService.check_once
# ---------------------------------------------------------------------------


class TestCheckOnce:
    """Tests for HealthMonitorService.check_once."""

    @pytest.mark.asyncio
    async def test_online_workers_persisted(self, test_engine: Any) -> None:
        """check_once persists online workers with status 'online'."""
        ts = datetime.now(UTC) - timedelta(seconds=5)
        entries = {
            "workers.worker-001": FakeKVEntry(
                key="workers.worker-001",
                value=json.dumps(_worker_metadata(hostname="edge-001")).encode(),
                created=ts,
            ),
        }
        kv = _make_mock_kv(entries=entries)
        nc = _make_mock_nc(kv=kv)
        session_factory = _make_session_factory(test_engine)
        monitor = HealthMonitorService(nc, session_factory)

        async with session_factory() as session:
            count = await monitor.check_once(session)

        assert count == 1

        async with session_factory() as session:
            result = await session.execute(select(WorkerHeartbeat))
            records = result.scalars().all()

        assert len(records) == 1
        r = records[0]
        assert r.worker_id == "worker-001"
        assert r.hostname == "edge-001"
        assert r.status == "online"
        assert r.capabilities == ["script_execution"]
        assert r.current_tasks == 0
        # SQLite strips timezone on read — compare as naive UTC values
        stored = r.recorded_at.replace(tzinfo=None) if r.recorded_at.tzinfo else r.recorded_at
        expected = ts.replace(tzinfo=None) if ts.tzinfo else ts
        assert stored == expected

    @pytest.mark.asyncio
    async def test_offline_workers_detected(self, test_engine: Any) -> None:
        """check_once marks workers with stale heartbeat (>30s) as offline."""
        stale_ts = datetime.now(UTC) - timedelta(seconds=45)
        entries = {
            "workers.worker-001": FakeKVEntry(
                key="workers.worker-001",
                value=json.dumps(_worker_metadata(hostname="edge-001")).encode(),
                created=stale_ts,
            ),
        }
        kv = _make_mock_kv(entries=entries)
        nc = _make_mock_nc(kv=kv)
        session_factory = _make_session_factory(test_engine)
        monitor = HealthMonitorService(nc, session_factory)

        async with session_factory() as session:
            count = await monitor.check_once(session)

        assert count == 1

        async with session_factory() as session:
            result = await session.execute(select(WorkerHeartbeat))
            records = result.scalars().all()

        assert len(records) == 1
        assert records[0].status == "offline"
        assert records[0].worker_id == "worker-001"

    @pytest.mark.asyncio
    async def test_mixed_online_offline(self, test_engine: Any) -> None:
        """check_once correctly handles a mix of online and offline workers."""
        fresh_ts = datetime.now(UTC) - timedelta(seconds=5)
        stale_ts = datetime.now(UTC) - timedelta(seconds=60)
        entries = {
            "workers.online-worker": FakeKVEntry(
                key="workers.online-worker",
                value=json.dumps(_worker_metadata(hostname="edge-online")).encode(),
                created=fresh_ts,
            ),
            "workers.offline-worker": FakeKVEntry(
                key="workers.offline-worker",
                value=json.dumps(
                    _worker_metadata(hostname="edge-offline", current=2)
                ).encode(),
                created=stale_ts,
            ),
        }
        kv = _make_mock_kv(entries=entries)
        nc = _make_mock_nc(kv=kv)
        session_factory = _make_session_factory(test_engine)
        monitor = HealthMonitorService(nc, session_factory)

        async with session_factory() as session:
            count = await monitor.check_once(session)

        assert count == 2

        async with session_factory() as session:
            result = await session.execute(select(WorkerHeartbeat))
            records = result.scalars().all()

        by_worker = {r.worker_id: r for r in records}
        assert by_worker["online-worker"].status == "online"
        assert by_worker["offline-worker"].status == "offline"
        assert by_worker["offline-worker"].current_tasks == 2

    @pytest.mark.asyncio
    async def test_empty_kv_no_records(self, test_engine: Any) -> None:
        """check_once writes no records when KV is empty."""
        kv = _make_mock_kv(entries={})
        nc = _make_mock_nc(kv=kv)
        session_factory = _make_session_factory(test_engine)
        monitor = HealthMonitorService(nc, session_factory)

        async with session_factory() as session:
            count = await monitor.check_once(session)

        assert count == 0

        async with session_factory() as session:
            result = await session.execute(select(WorkerHeartbeat))
            records = result.scalars().all()

        assert len(records) == 0

    @pytest.mark.asyncio
    async def test_bucket_missing_no_error(self, test_engine: Any) -> None:
        """check_once returns 0 when KV bucket does not exist (no exception)."""
        nc = _make_mock_nc(bucket_exists=False)
        session_factory = _make_session_factory(test_engine)
        monitor = HealthMonitorService(nc, session_factory)

        async with session_factory() as session:
            count = await monitor.check_once(session)

        assert count == 0

    @pytest.mark.asyncio
    async def test_skips_non_worker_keys(self, test_engine: Any) -> None:
        """check_once skips keys that don't start with 'workers.'."""
        ts = datetime.now(UTC) - timedelta(seconds=5)
        entries = {
            "workers.worker-001": FakeKVEntry(
                key="workers.worker-001",
                value=json.dumps(_worker_metadata()).encode(),
                created=ts,
            ),
            "stray_key": FakeKVEntry(
                key="stray_key",
                value=b"junk",
                created=ts,
            ),
        }
        kv = _make_mock_kv(entries=entries)
        nc = _make_mock_nc(kv=kv)
        session_factory = _make_session_factory(test_engine)
        monitor = HealthMonitorService(nc, session_factory)

        async with session_factory() as session:
            count = await monitor.check_once(session)

        assert count == 1

        async with session_factory() as session:
            result = await session.execute(select(WorkerHeartbeat))
            records = result.scalars().all()

        assert len(records) == 1
        assert records[0].worker_id == "worker-001"

    @pytest.mark.asyncio
    async def test_malformed_metadata_handled(self, test_engine: Any) -> None:
        """check_once handles malformed JSON metadata gracefully."""
        ts = datetime.now(UTC) - timedelta(seconds=5)
        entries = {
            "workers.worker-001": FakeKVEntry(
                key="workers.worker-001",
                value=b"not-json",
                created=ts,
            ),
        }
        kv = _make_mock_kv(entries=entries)
        nc = _make_mock_nc(kv=kv)
        session_factory = _make_session_factory(test_engine)
        monitor = HealthMonitorService(nc, session_factory)

        async with session_factory() as session:
            count = await monitor.check_once(session)

        assert count == 1

        async with session_factory() as session:
            result = await session.execute(select(WorkerHeartbeat))
            records = result.scalars().all()

        assert len(records) == 1
        assert records[0].hostname == ""
        assert records[0].capabilities == []
        assert records[0].current_tasks == 0

    @pytest.mark.asyncio
    async def test_naive_timestamp_normalized(self, test_engine: Any) -> None:
        """check_once handles naive datetime from KV entry (assumed UTC)."""
        naive_ts = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=5)
        entries = {
            "workers.worker-001": FakeKVEntry(
                key="workers.worker-001",
                value=json.dumps(_worker_metadata()).encode(),
                created=naive_ts,
            ),
        }
        kv = _make_mock_kv(entries=entries)
        nc = _make_mock_nc(kv=kv)
        session_factory = _make_session_factory(test_engine)
        monitor = HealthMonitorService(nc, session_factory)

        async with session_factory() as session:
            count = await monitor.check_once(session)

        assert count == 1

        async with session_factory() as session:
            result = await session.execute(select(WorkerHeartbeat))
            records = result.scalars().all()

        assert records[0].status == "online"
        # SQLite strips timezone on read; verify the value matches the original
        stored = records[0].recorded_at
        if stored.tzinfo is not None:
            stored = stored.replace(tzinfo=None)
        assert stored == naive_ts

    @pytest.mark.asyncio
    async def test_kv_unreachable_raises_runtime_error(
        self, test_engine: Any
    ) -> None:
        """check_once raises RuntimeError when KV bucket is unreachable (not missing)."""
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(side_effect=ConnectionError("network down"))
        nc = MagicMock()
        nc.jetstream = MagicMock(return_value=mock_js)
        session_factory = _make_session_factory(test_engine)
        monitor = HealthMonitorService(nc, session_factory)

        async with session_factory() as session:
            with pytest.raises(RuntimeError, match="not available"):
                await monitor.check_once(session)


# ---------------------------------------------------------------------------
# HealthMonitorService lifecycle
# ---------------------------------------------------------------------------


class TestHealthMonitorLifecycle:
    """Tests for HealthMonitorService start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_task(self, test_engine: Any) -> None:
        """start() creates a background task."""
        kv = _make_mock_kv(entries={})
        nc = _make_mock_nc(kv=kv)
        session_factory = _make_session_factory(test_engine)
        monitor = HealthMonitorService(nc, session_factory, poll_interval=0.01)

        await monitor.start()
        assert monitor._task is not None
        assert not monitor._task.done()

        await monitor.stop()
        assert monitor._task is None

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, test_engine: Any) -> None:
        """stop() can be called multiple times safely."""
        kv = _make_mock_kv(entries={})
        nc = _make_mock_nc(kv=kv)
        session_factory = _make_session_factory(test_engine)
        monitor = HealthMonitorService(nc, session_factory)

        await monitor.stop()  # Never started — should not raise
        await monitor.stop()  # Double stop — should not raise

    @pytest.mark.asyncio
    async def test_start_twice_raises(self, test_engine: Any) -> None:
        """start() raises RuntimeError if already running."""
        kv = _make_mock_kv(entries={})
        nc = _make_mock_nc(kv=kv)
        session_factory = _make_session_factory(test_engine)
        monitor = HealthMonitorService(nc, session_factory, poll_interval=0.01)

        await monitor.start()
        with pytest.raises(RuntimeError, match="already started"):
            await monitor.start()

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_poll_loop_persists_records(self, tmp_path: Any) -> None:
        """The background poll loop persists heartbeat records.

        Uses a temp-FILE SQLite engine so the background poll task — which
        opens its OWN session/connection (and, under a full-suite run, may run
        on a reused/changed connection) — reliably sees the committed schema.
        An in-memory ``StaticPool`` engine shares one connection and works in
        isolation, but under the full suite that single connection can be
        recycled by the time the poll task commits, surfacing as
        "no such table: worker_heartbeats". A file DB has no such coupling.
        """
        from sqlalchemy.ext.asyncio import create_async_engine

        from ate_cloud.models import Base

        db_path = tmp_path / "health_poll.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = _make_session_factory(engine)
        try:
            ts = datetime.now(UTC) - timedelta(seconds=5)
            entries = {
                "workers.worker-001": FakeKVEntry(
                    key="workers.worker-001",
                    value=json.dumps(_worker_metadata()).encode(),
                    created=ts,
                ),
            }
            kv = _make_mock_kv(entries=entries)
            nc = _make_mock_nc(kv=kv)
            monitor = HealthMonitorService(nc, session_factory, poll_interval=0.05)

            await monitor.start()
            # Wait for at least one poll cycle
            await asyncio.sleep(0.15)
            await monitor.stop()

            async with session_factory() as session:
                result = await session.execute(select(WorkerHeartbeat))
                records = result.scalars().all()

            assert len(records) >= 1
            assert records[0].worker_id == "worker-001"
            assert records[0].status == "online"
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# API endpoint tests (via HTTP client)
# ---------------------------------------------------------------------------


class TestWorkerHistoryEndpoint:
    """Integration tests for GET /api/v1/workers/{worker_id}/history."""

    @pytest.mark.asyncio
    async def test_returns_records_ordered_desc(
        self, app: Any, client: Any, db_session: AsyncSession
    ) -> None:
        """GET /workers/{id}/history returns records ordered by recorded_at DESC."""
        now = datetime.now(UTC)
        for i in range(3):
            db_session.add(WorkerHeartbeat(
                id=str(uuid.uuid4()),
                worker_id="worker-001",
                hostname="edge-001",
                status="online" if i < 2 else "offline",
                capabilities=["script_execution"],
                current_tasks=i,
                recorded_at=now - timedelta(seconds=30 * (2 - i)),
            ))
        await db_session.flush()

        resp = await client.get("/api/v1/workers/worker-001/history")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        items = data["items"]
        # Most recent first
        assert items[0]["current_tasks"] == 2
        assert items[1]["current_tasks"] == 1
        assert items[2]["current_tasks"] == 0
        # Check ordering by recorded_at
        times = [item["recorded_at"] for item in items]
        assert times[0] >= times[1] >= times[2]

    @pytest.mark.asyncio
    async def test_limit_param(self, app: Any, client: Any, db_session: AsyncSession) -> None:
        """GET /workers/{id}/history?limit=N returns at most N records."""
        now = datetime.now(UTC)
        for i in range(5):
            db_session.add(WorkerHeartbeat(
                id=str(uuid.uuid4()),
                worker_id="worker-001",
                hostname="edge-001",
                status="online",
                capabilities=["script_execution"],
                current_tasks=i,
                recorded_at=now - timedelta(seconds=30 * (4 - i)),
            ))
        await db_session.flush()

        resp = await client.get("/api/v1/workers/worker-001/history?limit=2")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    @pytest.mark.asyncio
    async def test_nonexistent_worker_returns_empty(
        self, app: Any, client: Any
    ) -> None:
        """GET /workers/{id}/history returns empty list for unknown worker."""
        resp = await client.get("/api/v1/workers/nonexistent/history")

        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_default_limit_100(
        self, app: Any, client: Any, db_session: AsyncSession
    ) -> None:
        """GET /workers/{id}/history with no limit param defaults to 100."""
        now = datetime.now(UTC)
        for i in range(3):
            db_session.add(WorkerHeartbeat(
                id=str(uuid.uuid4()),
                worker_id="worker-001",
                hostname="edge-001",
                status="online",
                capabilities=[],
                current_tasks=0,
                recorded_at=now - timedelta(seconds=i),
            ))
        await db_session.flush()

        resp = await client.get("/api/v1/workers/worker-001/history")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3

    @pytest.mark.asyncio
    async def test_offline_status_in_history(
        self, app: Any, client: Any, db_session: AsyncSession
    ) -> None:
        """History endpoint returns offline status when present."""
        now = datetime.now(UTC)
        db_session.add(WorkerHeartbeat(
            id=str(uuid.uuid4()),
            worker_id="worker-001",
            hostname="edge-001",
            status="offline",
            capabilities=["script_execution"],
            current_tasks=0,
            recorded_at=now - timedelta(seconds=60),
        ))
        await db_session.flush()

        resp = await client.get("/api/v1/workers/worker-001/history")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["status"] == "offline"
