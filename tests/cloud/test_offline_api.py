"""Offline status/reconcile/cache API tests (T24 v41-gap-analysis, doc §10.5).

Covers the ``/api/v1/offline`` router mounted via ``_PROTECTED_ROUTERS``:

- GET  /offline/status        -> {online, pending_upload_count, cache_health{...}}
- POST /offline/reconcile     -> 202 manual trigger (no-op ok while online)
- GET  /offline/cache/items   -> cached entry listing (no filesystem paths)
- GET  /offline/status/stream -> SSE emitting ``offline_status`` events

Auth matrix follows tests/cloud/test_auth_enforcement.py conventions
(dev_mode disabled per-test, real RS256 login tokens). Service-backed
behavior uses FastAPI dependency_overrides on the module's private dep
factory (T17 pattern) plus REAL offline components wired through public
ctor seams for integration coverage.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ate_cloud.config import settings

# ---------------------------------------------------------------------------
# Auth fixtures (compact copy of test_auth_enforcement.py pattern)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def rsa_keypair() -> rsa.RSAPrivateKey:
    """Generate a 2048-bit RSA keypair for RS256 JWT signing (session-scoped)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwt_secret(rsa_keypair: rsa.RSAPrivateKey) -> str:
    """PEM-encoded RSA private key used as Settings.jwt_secret."""
    pem = rsa_keypair.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode()


@pytest.fixture(autouse=True)
def _auth_mode(jwt_secret: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable dev mode and configure the JWT secret for real token checks."""
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "jwt_secret", jwt_secret)


async def _create_admin_user(db_session: Any) -> Any:
    """Insert an active admin user into the test database."""
    from ate_cloud.auth.password import hash_password
    from ate_cloud.models.user import User

    user = User(
        id=str(uuid.uuid4()),
        username=f"admin_{uuid.uuid4().hex[:8]}",
        password_hash=hash_password("secret123"),
        role="admin",
        scopes=None,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _login_token(client: Any, db_session: Any) -> str:
    """Create an admin user and obtain a valid access token via POST /auth/login."""
    user = await _create_admin_user(db_session)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": user.username, "password": "secret123"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Fakes / real-component builders
# ---------------------------------------------------------------------------


class _FakeStatusService:
    """Deterministic canned status provider for shape/override tests."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {
            "online": False,
            "pending_upload_count": 3,
            "cache_health": {
                "size_bytes": 2048,
                "oldest_record_age_h": 1.5,
                "capacity_pct": 20.0,
                "downloads_paused": False,
            },
        }
        self.reconcile_calls = 0

    def status(self) -> dict[str, Any]:
        return self.payload

    def reconcile(self) -> dict[str, Any]:
        self.reconcile_calls += 1
        return {
            "ok": True,
            "uploaded": 0,
            "acked": 0,
            "confirmed_entries": 0,
            "conflicts_resolved": 0,
            "quarantined": 0,
            "locks_released": 0,
            "duration": 0.001,
            "quarantine": [],
        }

    def cache_items(self) -> list[dict[str, Any]]:
        return []


class _FakeClock:
    """Advancing fake clock for HeartbeatMonitor determinism."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _AcceptingUploader:
    """Uploader seam that accepts every record (server ACK semantics)."""

    def upload_record(self, record: Any) -> bool:
        return True

    def resolve_version(self, kind: str, entry_id: str, version: str, checksum: str) -> Any:
        raise NotImplementedError("not exercised by these tests")

    def report_script(self, script_id: str, version: str, checksum: str) -> bool:
        return True


class _RejectingUploader(_AcceptingUploader):
    """Uploader seam that rejects every record -> quarantine path exercise."""

    def upload_record(self, record: Any) -> bool:
        return False


def _build_real_service(
    tmp_path: Any,
    uploader: Any | None = None,
    heartbeat: Any | None = None,
) -> Any:
    """Wire a REAL OfflineStatusService from offline/* public ctor seams."""
    from ate_cloud.api.v1.offline import OfflineStatusService
    from ate_platform.offline import (
        CapacityGuard,
        HeartbeatMonitor,
        OfflineCacheStore,
        Reconciler,
        UploadQueue,
        VersionLockManager,
    )
    from ate_platform.offline.script_cache import OfflineScriptCache

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    queue = UploadQueue(tmp_path / "queue.db")
    store = OfflineCacheStore(tmp_path / "cache.db")
    scripts = OfflineScriptCache(tmp_path / "scripts")
    guard = CapacityGuard(cache_dir)
    reconciler = Reconciler(
        queue=queue,
        cache=store,
        scripts=scripts,
        locks=VersionLockManager(store),
        uploader=uploader if uploader is not None else _AcceptingUploader(),
    )
    return OfflineStatusService(
        heartbeat=heartbeat if heartbeat is not None else HeartbeatMonitor(),
        capacity_guard=guard,
        upload_queue=queue,
        cache_store=store,
        reconciler=reconciler,
        capacity_budget_bytes=10240,
    )


def _override_service(client: Any, service: Any) -> None:
    from ate_cloud.api.v1.offline import _get_status_service

    client.app.dependency_overrides[_get_status_service] = lambda: service


# ---------------------------------------------------------------------------
# Auth enforcement (central _PROTECTED_ROUTERS mount)
# ---------------------------------------------------------------------------


class TestAuthEnforcement:
    @pytest.mark.asyncio
    async def test_anonymous_status_401(self, client) -> None:
        response = await client.get("/api/v1/offline/status")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_anonymous_reconcile_401(self, client) -> None:
        response = await client.post("/api/v1/offline/reconcile")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_anonymous_cache_items_401(self, client) -> None:
        response = await client.get("/api/v1/offline/cache/items")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_anonymous_stream_401(self, client) -> None:
        response = await client.get("/api/v1/offline/status/stream")
        assert response.status_code == 401

    def test_offline_router_in_protected_mounts(self) -> None:
        """Mount-level sanity: offline_router carries get_current_user at include."""
        from ate_cloud.api.v1 import router as router_module

        def _dep_names(entry: Any) -> set[str]:
            ctx = getattr(entry, "include_context", None)
            return {
                getattr(d, "dependency", None).__name__ or ""
                for d in (getattr(ctx, "dependencies", None) or [])
            }

        mounts = list(router_module.api_router.routes)
        protected_routers = [
            m.include_context.included_router
            for m in mounts
            if "get_current_user" in _dep_names(m)
        ]
        assert router_module.offline_router in protected_routers


# ---------------------------------------------------------------------------
# GET /offline/status
# ---------------------------------------------------------------------------


class TestStatusEndpoint:
    @pytest.mark.asyncio
    async def test_status_shape_with_fake_service(self, client, db_session) -> None:
        """Status returns exactly the §10.5 badge shape with canned values."""
        token = await _login_token(client, db_session)
        _override_service(client, _FakeStatusService())

        response = await client.get(
            "/api/v1/offline/status", headers=_auth_headers(token)
        )

        assert response.status_code == 200
        body = response.json()
        assert set(body.keys()) == {"online", "pending_upload_count", "cache_health"}
        assert body["online"] is False
        assert body["pending_upload_count"] == 3
        health = body["cache_health"]
        assert set(health.keys()) == {
            "size_bytes",
            "oldest_record_age_h",
            "capacity_pct",
            "downloads_paused",
        }
        assert health["size_bytes"] == 2048
        assert health["oldest_record_age_h"] == 1.5
        assert health["capacity_pct"] == 20.0

    @pytest.mark.asyncio
    async def test_status_reflects_seeded_queue_and_cache(
        self, client, db_session, tmp_path
    ) -> None:
        """QA happy path: seeded pending records + cache entries show up."""
        token = await _login_token(client, db_session)
        service = _build_real_service(tmp_path)
        service.upload_queue.enqueue("st-1", "exec-1", 0, "payload-0.json")
        service.upload_queue.enqueue("st-1", "exec-1", 1, "payload-1.json")
        service.cache_store.store_sequence("seq-a", "v1", "plan: v1")
        # Seed the capacity-guard's watched dir so size_bytes is deterministic.
        (tmp_path / "cache" / "cached-sequence.bin").write_bytes(b"x" * 128)

        _override_service(client, service)
        response = await client.get(
            "/api/v1/offline/status", headers=_auth_headers(token)
        )

        assert response.status_code == 200
        body = response.json()
        assert body["online"] is True  # fresh monitor never timed out
        assert body["pending_upload_count"] == 2
        assert body["cache_health"]["size_bytes"] == 128

    @pytest.mark.asyncio
    async def test_status_online_false_after_heartbeat_timeout(
        self, client, db_session, tmp_path
    ) -> None:
        """online mirrors HeartbeatMonitor state after timeout+hysteresis."""
        from ate_platform.offline import HeartbeatMonitor

        token = await _login_token(client, db_session)

        clock = _FakeClock()
        monitor = HeartbeatMonitor(timeout_seconds=10.0, required_misses=2, clock=clock)
        clock.advance(11.0)
        monitor.check()
        clock.advance(0.5)
        monitor.check()
        assert monitor.state == "offline"

        # Inject the timed-out monitor through the service ctor seam
        # (no offline/* internals touched).
        service = _build_real_service(tmp_path, heartbeat=monitor)

        _override_service(client, service)
        response = await client.get(
            "/api/v1/offline/status", headers=_auth_headers(token)
        )

        assert response.status_code == 200
        assert response.json()["online"] is False

    @pytest.mark.asyncio
    async def test_capacity_pct_computed_against_budget(
        self, client, db_session, tmp_path
    ) -> None:
        """capacity_pct = size_bytes / budget * 100 with ctor-injected budget."""
        from ate_cloud.api.v1.offline import OfflineStatusService
        from ate_platform.offline import CapacityGuard, HeartbeatMonitor
        from ate_platform.offline.cache_store import OfflineCacheStore
        from ate_platform.offline.upload_queue import UploadQueue

        cache_dir = tmp_path / "cap"
        cache_dir.mkdir()
        blob = cache_dir / "blob.bin"
        blob.write_bytes(b"x" * 512)

        service = OfflineStatusService(
            heartbeat=HeartbeatMonitor(),
            capacity_guard=CapacityGuard(cache_dir),
            upload_queue=UploadQueue(tmp_path / "q.db"),
            cache_store=OfflineCacheStore(tmp_path / "c.db"),
            capacity_budget_bytes=1024,
        )
        _override_service(client, service)
        token = await _login_token(client, db_session)

        response = await client.get(
            "/api/v1/offline/status", headers=_auth_headers(token)
        )

        assert response.status_code == 200
        health = response.json()["cache_health"]
        assert health["size_bytes"] == 512
        assert health["capacity_pct"] == 50.0

    @pytest.mark.asyncio
    async def test_unconfigured_service_returns_503(self, client, db_session) -> None:
        """Valid JWT but no provider wired on app.state -> honest 503."""
        token = await _login_token(client, db_session)
        response = await client.get(
            "/api/v1/offline/status", headers=_auth_headers(token)
        )
        assert response.status_code == 503
        assert "offline" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST /offline/reconcile
# ---------------------------------------------------------------------------


class TestReconcileEndpoint:
    @pytest.mark.asyncio
    async def test_reconcile_202_noop_when_online(self, client, db_session, tmp_path) -> None:
        """QA failure scenario: reconcile while online/empty -> 202 + zero report."""
        token = await _login_token(client, db_session)
        _override_service(client, _build_real_service(tmp_path))

        response = await client.post(
            "/api/v1/offline/reconcile", headers=_auth_headers(token)
        )

        assert response.status_code == 202
        body = response.json()
        assert body["ok"] is True
        assert body["uploaded"] == 0
        assert body["acked"] == 0
        assert body["quarantined"] == 0
        assert body["locks_released"] == 0

    @pytest.mark.asyncio
    async def test_reconcile_flushes_pending_and_updates_status(
        self, client, db_session, tmp_path
    ) -> None:
        """Seeded pending records are uploaded+acked; status count drops to 0."""
        token = await _login_token(client, db_session)
        service = _build_real_service(tmp_path)
        service.upload_queue.enqueue("st-1", "exec-1", 0, "payload-0.json")
        service.upload_queue.enqueue("st-1", "exec-2", 0, "payload-1.json")
        _override_service(client, service)

        response = await client.post(
            "/api/v1/offline/reconcile", headers=_auth_headers(token)
        )

        assert response.status_code == 202
        body = response.json()
        assert body["uploaded"] == 2
        assert body["acked"] == 2

        status_after = await client.get(
            "/api/v1/offline/status", headers=_auth_headers(token)
        )
        assert status_after.json()["pending_upload_count"] == 0

    @pytest.mark.asyncio
    async def test_reconcile_response_hides_filesystem_paths(
        self, client, db_session, tmp_path
    ) -> None:
        """Quarantine view must NOT leak raw payload paths (spec MUST NOT)."""
        token = await _login_token(client, db_session)
        secret_payload = tmp_path / "payload-secret.bin"
        secret_payload.write_text("data")
        service = _build_real_service(tmp_path, uploader=_RejectingUploader())
        service.upload_queue.enqueue("st-1", "exec-1", 7, str(secret_payload))
        _override_service(client, service)

        response = await client.post(
            "/api/v1/offline/reconcile", headers=_auth_headers(token)
        )

        assert response.status_code == 202
        body = response.json()
        assert body["quarantined"] == 1
        assert body["quarantine"][0]["reason"] == "upload_rejected"
        assert str(secret_payload) not in response.text
        assert "detail" not in body["quarantine"][0]

    @pytest.mark.asyncio
    async def test_reconcile_without_reconciler_503(self, client, db_session, tmp_path) -> None:
        """Service wired without a reconciler -> explicit 503, not a crash."""
        from ate_cloud.api.v1.offline import OfflineStatusService
        from ate_platform.offline import (
            CapacityGuard,
            HeartbeatMonitor,
            OfflineCacheStore,
            UploadQueue,
        )

        token = await _login_token(client, db_session)
        service = OfflineStatusService(
            heartbeat=HeartbeatMonitor(),
            capacity_guard=CapacityGuard(tmp_path / "cap"),
            upload_queue=UploadQueue(tmp_path / "q.db"),
            cache_store=OfflineCacheStore(tmp_path / "c.db"),
            reconciler=None,
        )
        _override_service(client, service)
        response = await client.post(
            "/api/v1/offline/reconcile", headers=_auth_headers(token)
        )
        assert response.status_code == 503


# ---------------------------------------------------------------------------
# GET /offline/cache/items
# ---------------------------------------------------------------------------


class TestCacheItemsEndpoint:
    @pytest.mark.asyncio
    async def test_cache_items_lists_entries_without_paths(
        self, client, db_session, tmp_path
    ) -> None:
        """Cached sequence+topology listed with state; no raw paths anywhere."""
        token = await _login_token(client, db_session)
        service = _build_real_service(tmp_path)
        service.cache_store.store_sequence("seq-a", "v1", "plan: v1")
        service.cache_store.store_topology("topo-b", "v2", '{"links": []}')
        _override_service(client, service)

        response = await client.get(
            "/api/v1/offline/cache/items", headers=_auth_headers(token)
        )

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        kinds = {item["kind"] for item in body["items"]}
        assert kinds == {"sequence", "topology"}
        item = next(i for i in body["items"] if i["id"] == "seq-a")
        assert item["version"] == "v1"
        assert item["state"] == "cached"
        assert str(tmp_path) not in response.text

    @pytest.mark.asyncio
    async def test_cache_items_empty(self, client, db_session, tmp_path) -> None:
        token = await _login_token(client, db_session)
        _override_service(client, _build_real_service(tmp_path))

        response = await client.get(
            "/api/v1/offline/cache/items", headers=_auth_headers(token)
        )

        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0}


# ---------------------------------------------------------------------------
# SSE stream: GET /offline/status/stream
# ---------------------------------------------------------------------------


def _mock_request(app: Any) -> Any:
    request = MagicMock()
    request.headers = {}
    request.app = app
    request.is_disconnected = AsyncMock(return_value=False)
    return request


class TestSSEStream:
    @pytest.mark.asyncio
    async def test_sse_emits_offline_status_events(self, app) -> None:
        """Published offline_status events flow through the isolated stream queue."""
        from ate_cloud.api.v1.offline import (
            OFFLINE_STREAM_NAME,
            OFFLINE_STREAM_RUN_ID,
            publish_offline_status,
            stream_offline_status,
        )

        bridge = app.state.sse_bridge
        await publish_offline_status(bridge, {"online": False, "badge": "offline"})

        response = await stream_offline_status(
            request=_mock_request(app),
            bridge=bridge,
            service=_FakeStatusService(),
        )

        seen: list[tuple[str, str]] = []
        body = response.body_iterator
        try:
            for _ in range(2):
                chunk = await asyncio.wait_for(body.__anext__(), timeout=2.0)
                seen.append((chunk.event, chunk.data))
        except (TimeoutError, StopAsyncIteration):
            pass

        # Chunk 0: immediate initial snapshot; Chunk 1: published event.
        assert seen[0][0] == "offline_status"
        assert '"online"' in seen[0][1]
        assert seen[1][0] == "offline_status"
        assert "badge" in seen[1][1]
        bridge.remove_stream_queue(OFFLINE_STREAM_RUN_ID, OFFLINE_STREAM_NAME)

    @pytest.mark.asyncio
    async def test_sse_initial_snapshot_immediate(self, app) -> None:
        """First SSE frame is the current status snapshot (instant badge paint)."""
        from ate_cloud.api.v1.offline import (
            OFFLINE_STREAM_NAME,
            OFFLINE_STREAM_RUN_ID,
            stream_offline_status,
        )

        bridge = app.state.sse_bridge
        service = _FakeStatusService()

        response = await stream_offline_status(
            request=_mock_request(app), bridge=bridge, service=service
        )

        chunk = await asyncio.wait_for(response.body_iterator.__anext__(), timeout=2.0)
        assert chunk.event == "offline_status"
        assert '"pending_upload_count":3' in chunk.data.replace(" ", "")
        bridge.remove_stream_queue(OFFLINE_STREAM_RUN_ID, OFFLINE_STREAM_NAME)

    @pytest.mark.asyncio
    async def test_sse_stream_isolated_from_main_events_queue(self) -> None:
        """offline stream queue is independent from the main /events queue."""
        from ate_cloud.api.v1.offline import (
            OFFLINE_STREAM_NAME,
            OFFLINE_STREAM_RUN_ID,
            publish_offline_status,
        )
        from ate_cloud.nats.sse_bridge import SSEBridge

        bridge = SSEBridge(nc=None)
        main_q = bridge.get_or_create_queue("run-1")
        await publish_offline_status(bridge, {"online": True})

        assert main_q.qsize() == 0  # main queue untouched
        stream_q = bridge.get_stream_queue(OFFLINE_STREAM_RUN_ID, OFFLINE_STREAM_NAME)
        event = await asyncio.wait_for(stream_q.get(), timeout=1.0)
        assert event["type"] == "offline_status"
        assert event["category"] == "offline_status"
