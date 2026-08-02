"""Tests for WorkerRegistryService and worker API endpoints (Todo 9).

Verifies:
1. list_workers — empty bucket, bucket missing, multiple workers with decoded metadata.
2. get_worker — found, not found, bucket missing.
3. get_worker_health — online, offline (missing key), offline (missing bucket).
4. API endpoints — GET /workers (empty list), GET /workers/{id} (404), GET /workers/{id}/health.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.errors import KeyNotFoundError, NoKeysError, NotFoundError

from ate_cloud.services.worker_registry import (
    WORKER_KV_BUCKET,
    WorkerRegistryService,
)

# ---------------------------------------------------------------------------
# Helpers
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
    """Build a mock NATS client + JetStream context.

    ``jetstream()`` is sync (returns JetStreamContext without I/O) and
    ``key_value`` is async — matching nats-py's API.
    """
    mock_js = MagicMock()
    if bucket_exists:
        mock_js.key_value = AsyncMock(return_value=kv)
    else:
        mock_js.key_value = AsyncMock(side_effect=NotFoundError)
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc


# ---------------------------------------------------------------------------
# WorkerRegistryService.list_workers
# ---------------------------------------------------------------------------


class TestListWorkers:
    """Tests for WorkerRegistryService.list_workers."""

    @pytest.mark.asyncio
    async def test_empty_bucket_returns_empty_list(self) -> None:
        """list_workers returns [] when the bucket exists but has no keys."""
        kv = _make_mock_kv(entries={})
        nc = _make_mock_nc(kv=kv)
        service = WorkerRegistryService(nc)

        workers = await service.list_workers()

        assert workers == []

    @pytest.mark.asyncio
    async def test_bucket_not_exists_returns_empty_list(self) -> None:
        """list_workers returns [] when the bucket does not exist."""
        nc = _make_mock_nc(bucket_exists=False)
        service = WorkerRegistryService(nc)

        workers = await service.list_workers()

        assert workers == []

    @pytest.mark.asyncio
    async def test_multiple_workers_returns_decoded_metadata(self) -> None:
        """list_workers returns all workers with decoded JSON metadata."""
        ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
        entries = {
            "workers.worker-001": FakeKVEntry(
                key="workers.worker-001",
                value=json.dumps(_worker_metadata(hostname="edge-001")).encode(),
                created=ts,
            ),
            "workers.worker-002": FakeKVEntry(
                key="workers.worker-002",
                value=json.dumps(
                    _worker_metadata(
                        hostname="edge-002",
                        max_concurrent=4,
                        current=2,
                    )
                ).encode(),
                created=ts,
            ),
        }
        kv = _make_mock_kv(entries=entries)
        nc = _make_mock_nc(kv=kv)
        service = WorkerRegistryService(nc)

        workers = await service.list_workers()

        assert len(workers) == 2
        ids = {w.worker_id for w in workers}
        assert ids == {"worker-001", "worker-002"}

        w1 = next(w for w in workers if w.worker_id == "worker-001")
        assert w1.hostname == "edge-001"
        assert w1.capabilities == ["script_execution"]
        assert w1.max_concurrent_tasks == 1
        assert w1.current_tasks == 0
        assert w1.last_heartbeat == ts

        w2 = next(w for w in workers if w.worker_id == "worker-002")
        assert w2.hostname == "edge-002"
        assert w2.max_concurrent_tasks == 4
        assert w2.current_tasks == 2

    @pytest.mark.asyncio
    async def test_skips_keys_without_worker_prefix(self) -> None:
        """list_workers skips keys that don't start with 'workers.'."""
        ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
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
        service = WorkerRegistryService(nc)

        workers = await service.list_workers()

        assert len(workers) == 1
        assert workers[0].worker_id == "worker-001"


# ---------------------------------------------------------------------------
# WorkerRegistryService.get_worker
# ---------------------------------------------------------------------------


class TestGetWorker:
    """Tests for WorkerRegistryService.get_worker."""

    @pytest.mark.asyncio
    async def test_found_returns_worker_info(self) -> None:
        """get_worker returns WorkerInfo when the key exists."""
        ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
        entries = {
            "workers.worker-001": FakeKVEntry(
                key="workers.worker-001",
                value=json.dumps(_worker_metadata(hostname="edge-001")).encode(),
                created=ts,
            ),
        }
        kv = _make_mock_kv(entries=entries)
        nc = _make_mock_nc(kv=kv)
        service = WorkerRegistryService(nc)

        worker = await service.get_worker("worker-001")

        assert worker is not None
        assert worker.worker_id == "worker-001"
        assert worker.hostname == "edge-001"
        assert worker.capabilities == ["script_execution"]
        assert worker.max_concurrent_tasks == 1
        assert worker.current_tasks == 0
        assert worker.last_heartbeat == ts

    @pytest.mark.asyncio
    async def test_not_found_returns_none(self) -> None:
        """get_worker returns None when the key does not exist."""
        kv = _make_mock_kv(entries={})
        nc = _make_mock_nc(kv=kv)
        service = WorkerRegistryService(nc)

        worker = await service.get_worker("nonexistent")

        assert worker is None

    @pytest.mark.asyncio
    async def test_bucket_not_exists_returns_none(self) -> None:
        """get_worker returns None when the bucket does not exist."""
        nc = _make_mock_nc(bucket_exists=False)
        service = WorkerRegistryService(nc)

        worker = await service.get_worker("worker-001")

        assert worker is None


# ---------------------------------------------------------------------------
# WorkerRegistryService.get_worker_health
# ---------------------------------------------------------------------------


class TestGetWorkerHealth:
    """Tests for WorkerRegistryService.get_worker_health."""

    @pytest.mark.asyncio
    async def test_online_when_key_exists(self) -> None:
        """get_worker_health returns 'online' with full worker_info."""
        ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
        entries = {
            "workers.worker-001": FakeKVEntry(
                key="workers.worker-001",
                value=json.dumps(_worker_metadata(hostname="edge-001")).encode(),
                created=ts,
            ),
        }
        kv = _make_mock_kv(entries=entries)
        nc = _make_mock_nc(kv=kv)
        service = WorkerRegistryService(nc)

        health = await service.get_worker_health("worker-001")

        assert health.status == "online"
        assert health.worker_info is not None
        assert health.worker_info.worker_id == "worker-001"
        assert health.last_heartbeat_timestamp == ts

    @pytest.mark.asyncio
    async def test_offline_when_key_missing(self) -> None:
        """get_worker_health returns 'offline' when the key is missing."""
        kv = _make_mock_kv(entries={})
        nc = _make_mock_nc(kv=kv)
        service = WorkerRegistryService(nc)

        health = await service.get_worker_health("nonexistent")

        assert health.status == "offline"
        assert health.worker_info is None
        assert health.last_heartbeat_timestamp is None

    @pytest.mark.asyncio
    async def test_offline_when_bucket_missing(self) -> None:
        """get_worker_health returns 'offline' when the bucket does not exist."""
        nc = _make_mock_nc(bucket_exists=False)
        service = WorkerRegistryService(nc)

        health = await service.get_worker_health("worker-001")

        assert health.status == "offline"
        assert health.worker_info is None
        assert health.last_heartbeat_timestamp is None


# ---------------------------------------------------------------------------
# API endpoint tests (via HTTP client)
# ---------------------------------------------------------------------------


class TestWorkerEndpoints:
    """Integration tests for GET /api/v1/workers endpoints."""

    @pytest.mark.asyncio
    async def test_list_workers_empty(
        self, app: Any, client: Any
    ) -> None:
        """GET /api/v1/workers returns empty list when bucket is empty."""
        kv = _make_mock_kv(entries={})
        nc = _make_mock_nc(kv=kv)
        app.state.nc = nc

        resp = await client.get("/api/v1/workers")

        assert resp.status_code == 200
        data = resp.json()
        assert data["workers"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_workers_with_entries(
        self, app: Any, client: Any
    ) -> None:
        """GET /api/v1/workers returns worker list with decoded metadata."""
        ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
        entries = {
            "workers.worker-001": FakeKVEntry(
                key="workers.worker-001",
                value=json.dumps(_worker_metadata(hostname="edge-001")).encode(),
                created=ts,
            ),
        }
        kv = _make_mock_kv(entries=entries)
        nc = _make_mock_nc(kv=kv)
        app.state.nc = nc

        resp = await client.get("/api/v1/workers")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["workers"][0]["worker_id"] == "worker-001"
        assert data["workers"][0]["hostname"] == "edge-001"

    @pytest.mark.asyncio
    async def test_get_worker_not_found_404(
        self, app: Any, client: Any
    ) -> None:
        """GET /api/v1/workers/{id} returns 404 for missing worker."""
        kv = _make_mock_kv(entries={})
        nc = _make_mock_nc(kv=kv)
        app.state.nc = nc

        resp = await client.get("/api/v1/workers/nonexistent")

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_get_worker_found(
        self, app: Any, client: Any
    ) -> None:
        """GET /api/v1/workers/{id} returns worker info for registered worker."""
        ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
        entries = {
            "workers.worker-001": FakeKVEntry(
                key="workers.worker-001",
                value=json.dumps(_worker_metadata(hostname="edge-001")).encode(),
                created=ts,
            ),
        }
        kv = _make_mock_kv(entries=entries)
        nc = _make_mock_nc(kv=kv)
        app.state.nc = nc

        resp = await client.get("/api/v1/workers/worker-001")

        assert resp.status_code == 200
        data = resp.json()
        assert data["worker_id"] == "worker-001"
        assert data["hostname"] == "edge-001"
        assert data["capabilities"] == ["script_execution"]

    @pytest.mark.asyncio
    async def test_get_worker_health_online(
        self, app: Any, client: Any
    ) -> None:
        """GET /api/v1/workers/{id}/health returns online for registered worker."""
        ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
        entries = {
            "workers.worker-001": FakeKVEntry(
                key="workers.worker-001",
                value=json.dumps(_worker_metadata(hostname="edge-001")).encode(),
                created=ts,
            ),
        }
        kv = _make_mock_kv(entries=entries)
        nc = _make_mock_nc(kv=kv)
        app.state.nc = nc

        resp = await client.get("/api/v1/workers/worker-001/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "online"
        assert data["worker_info"]["worker_id"] == "worker-001"

    @pytest.mark.asyncio
    async def test_get_worker_health_offline(
        self, app: Any, client: Any
    ) -> None:
        """GET /api/v1/workers/{id}/health returns offline for missing worker."""
        kv = _make_mock_kv(entries={})
        nc = _make_mock_nc(kv=kv)
        app.state.nc = nc

        resp = await client.get("/api/v1/workers/nonexistent/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "offline"
        assert data["worker_info"] is None

    @pytest.mark.asyncio
    async def test_nats_unavailable_returns_503(
        self, app: Any, client: Any
    ) -> None:
        """GET /api/v1/workers returns 503 when NATS client is not on app.state."""
        # Ensure app.state.nc is not set (simulates missing lifespan).
        if hasattr(app.state, "nc"):
            del app.state.nc

        resp = await client.get("/api/v1/workers")

        assert resp.status_code == 503
