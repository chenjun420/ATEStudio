"""One-time SSE ticket auth tests (RH-3, v41-remaining-hardening #3).

Covers POST /api/v1/auth/sse-ticket and the ``require_sse_user``
dependency wired onto every ``text/event-stream`` endpoint:

- issue endpoint requires a JWT (anonymous 401)
- issued tickets are single-consume (second use 401)
- expired / garbage tickets are rejected 401
- a valid ticket admits an SSE endpoint (200 + event-stream content type)
- ALL SSE endpoints (executions events + topology-stream, offline
  status/stream, recordings replay/stream) require the ticket
- ticket binds to a live user (deleted user -> 401)
- dev-mode bypass still works (offline test-suite parity)
- store housekeeping: TTL honored, expired purge, reset helper

Matrix mirrors tests/cloud/test_auth_enforcement.py conventions
(RS256 keypair fixtures, real token verification, dev_mode disabled).
"""

import uuid
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ate_cloud.auth.password import hash_password
from ate_cloud.auth.sse_ticket import (
    SSE_TICKET_TTL_SECONDS,
    consume_sse_ticket,
    issue_sse_ticket,
    reset_ticket_store,
    ticket_store_size,
)
from ate_cloud.config import settings
from ate_cloud.models.user import User

# ---------------------------------------------------------------------------
# Fixtures / helpers (test_auth_enforcement.py pattern)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def rsa_keypair() -> rsa.RSAPrivateKey:
    """Generate a 2048-bit RSA keypair for RS256 JWT signing."""
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
    """Disable dev mode + configure JWT secret + isolate the ticket store."""
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "jwt_secret", jwt_secret)
    reset_ticket_store()
    yield
    reset_ticket_store()


async def _create_admin_user(db_session: Any) -> User:
    """Insert an active admin user into the test database."""
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
    """Create an admin user and obtain a valid access token via login."""
    user = await _create_admin_user(db_session)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": user.username, "password": "secret123"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _issue_ticket(client: Any, token: str) -> str:
    """POST /auth/sse-ticket with a bearer token; return the ticket string."""
    resp = await client.post(
        "/api/v1/auth/sse-ticket",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["ticket"]


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _SseProbeResult:
    """Outcome of a raw-ASGI SSE probe (see _probe_sse_endpoint)."""

    def __init__(self, status_code: int, headers: dict[str, str], body: bytes):
        self.status_code = status_code
        self.headers = headers
        self.body = body

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


async def _probe_sse_endpoint(
    app: Any,
    path: str,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    max_chunks: int = 2,
    chunk_timeout: float = 2.0,
) -> _SseProbeResult:
    """Open an SSE endpoint via a RAW ASGI call and bail after first chunks.

    httpx's ASGITransport awaits the FULL app response, so ``client.get`` /
    ``client.stream`` can never return against an infinite SSE generator
    (pytest-timeout / hang). This probe instead:

    1. builds the ASGI scope itself (path/query/headers),
    2. runs the app in a task,
    3. captures ``http.response.start`` + at most ``max_chunks`` body
       messages (the immediate frame-0 snapshot / keep-alive),
    4. sends ``http.disconnect`` and CANCELS the app task - the
       EventSourceResponse task group unwinds and the generator's finally
       blocks run, exactly like a browser closing the EventSource.

    Returns status, headers and the concatenated body chunks.
    """
    from urllib.parse import urlencode

    query = urlencode(params or {})
    header_list: list[tuple[bytes, bytes]] = []
    for name, value in (headers or {}).items():
        header_list.append((name.lower().encode(), value.encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": header_list,
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }

    import asyncio

    response_started = asyncio.Event()
    done_receiving = asyncio.Event()
    status_code = 0
    resp_headers: dict[str, str] = {}
    body_parts: list[bytes] = []

    async def receive() -> dict[str, Any]:
        # First receive() feeds the (empty) request body; after that, block
        # until the probe is done, then report a client disconnect.
        await done_receiving.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code, resp_headers
        if message["type"] == "http.response.start":
            status_code = message["status"]
            resp_headers = {
                k.decode("latin-1"): v.decode("latin-1")
                for k, v in message.get("headers", [])
            }
            response_started.set()
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))
            if len(body_parts) >= max_chunks:
                done_receiving.set()

    app_task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(response_started.wait(), timeout=chunk_timeout)
        # If the generator produces data promptly (frame-0 snapshot), drain
        # up to max_chunks; otherwise (idle stream) a short grace window.
        try:
            while len(body_parts) < max_chunks:
                await asyncio.wait_for(
                    _wait_for_body(body_parts), timeout=chunk_timeout
                )
        except (TimeoutError, StopAsyncIteration):
            pass
    finally:
        done_receiving.set()
        app_task.cancel()
        try:
            await app_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - probe cleanup
            pass

    return _SseProbeResult(status_code, resp_headers, b"".join(body_parts))


async def _wait_for_body(body_parts: list[bytes]) -> None:
    """Block until at least one new body chunk arrives (poll-light)."""
    import asyncio

    seen = len(body_parts)
    while len(body_parts) == seen:
        await asyncio.sleep(0.01)


async def _open_stream_once(
    client: Any,
    path: str,
    *,
    params: dict[str, str] | None = None,
) -> Any:
    """Open an SSE endpoint with a raw-ASGI probe (anti-hang, see
    ``_probe_sse_endpoint``); returns the probe result with status/headers.
    """
    return await _probe_sse_endpoint(
        client.app, path, params=params, max_chunks=1
    )


# ---------------------------------------------------------------------------
# Ticket issuance
# ---------------------------------------------------------------------------


class TestTicketIssuance:
    @pytest.mark.asyncio
    async def test_issue_requires_jwt(self, client) -> None:
        """Anonymous POST /auth/sse-ticket -> 401 (auth router exemption
        covers login/register only, sse-ticket carries its own guard)."""
        response = await client.post("/api/v1/auth/sse-ticket")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_issue_with_token_returns_ticket(self, client, db_session) -> None:
        """Valid JWT -> 200 with an opaque ticket + 60s TTL."""
        token = await _login_token(client, db_session)
        response = await client.post(
            "/api/v1/auth/sse-ticket", headers=_auth_headers(token)
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["ticket"]) >= 32
        assert body["expires_in"] == 60

    @pytest.mark.asyncio
    async def test_issue_garbage_token_rejected(self, client) -> None:
        response = await client.post(
            "/api/v1/auth/sse-ticket", headers=_auth_headers("not.a.token")
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_each_issue_yields_distinct_ticket(self, client, db_session) -> None:
        token = await _login_token(client, db_session)
        t1 = await _issue_ticket(client, token)
        t2 = await _issue_ticket(client, token)
        assert t1 != t2


# ---------------------------------------------------------------------------
# Ticket consumption semantics (unit level)
# ---------------------------------------------------------------------------


class TestTicketStoreSemantics:
    def test_consume_returns_user_then_none(self) -> None:
        """Single-consume: first consume returns user_id, second None."""
        ticket = issue_sse_ticket("user-1")
        assert consume_sse_ticket(ticket) == "user-1"
        assert consume_sse_ticket(ticket) is None

    def test_unknown_ticket_rejected(self) -> None:
        assert consume_sse_ticket("garbage-ticket") is None

    def test_expired_ticket_rejected(self) -> None:
        """A ticket past its TTL is rejected (and consumed)."""
        ticket = issue_sse_ticket("user-1", ttl=-1.0)
        assert consume_sse_ticket(ticket) is None
        # Also stays consumed:
        assert consume_sse_ticket(ticket) is None

    def test_ttl_constant_is_60s(self) -> None:
        assert SSE_TICKET_TTL_SECONDS == 60.0

    def test_expired_entries_purged_opportunistically(self) -> None:
        """issue() drops expired entries so the store cannot grow unbounded."""
        issue_sse_ticket("user-1", ttl=-1.0)
        assert ticket_store_size() == 1  # not purged until next call
        issue_sse_ticket("user-2")  # triggers opportunistic purge
        assert ticket_store_size() == 1  # only the fresh ticket remains

    def test_ticket_is_urlsafe_random(self) -> None:
        """Tickets are urlsafe (no query-string-escaping hazards)."""
        for _ in range(5):
            ticket = issue_sse_ticket("u")
            assert all(c.isalnum() or c in "-_" for c in ticket)


# ---------------------------------------------------------------------------
# require_sse_user on the SSE endpoints
# ---------------------------------------------------------------------------


class TestSseEndpointEnforcement:
    @pytest.mark.asyncio
    async def test_topology_stream_no_ticket_401(self, client, db_session) -> None:
        """Anonymous topology-stream (no ticket, no bearer) -> 401."""
        await _login_token(client, db_session)  # ensure users exist
        response = await client.get("/api/v1/executions/run-1/topology-stream")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_events_stream_no_ticket_401(self, client, db_session) -> None:
        await _login_token(client, db_session)
        response = await client.get("/api/v1/executions/run-1/events")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_offline_status_stream_no_ticket_401(self, client, db_session) -> None:
        await _login_token(client, db_session)
        response = await client.get("/api/v1/offline/status/stream")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_replay_stream_no_ticket_401(self, client, db_session) -> None:
        """Recordings replay/stream requires a ticket (execution check after auth)."""
        await _login_token(client, db_session)
        response = await client.get("/api/v1/executions/run-1/replay/stream")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_garbage_ticket_401(self, client, db_session) -> None:
        await _login_token(client, db_session)
        response = await client.get(
            "/api/v1/executions/run-1/topology-stream",
            params={"ticket": "garbage"},
        )
        assert response.status_code == 401
        assert "ticket" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_second_consume_401(self, client, db_session) -> None:
        """A ticket already used once cannot open a second stream."""
        token = await _login_token(client, db_session)
        ticket = await _issue_ticket(client, token)

        first = await _open_stream_once(
            client,
            "/api/v1/executions/run-1/topology-stream",
            params={"ticket": ticket},
        )
        assert first.status_code == 200
        assert first.headers["content-type"].startswith("text/event-stream")

        second = await _open_stream_once(
            client,
            "/api/v1/executions/run-1/topology-stream",
            params={"ticket": ticket},
        )
        assert second.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_ticket_401(self, client, db_session) -> None:
        """An expired ticket (minted with negative TTL) is rejected."""
        user = await _create_admin_user(db_session)
        expired = issue_sse_ticket(user.id, ttl=-1.0)
        response = await _open_stream_once(
            client,
            "/api/v1/executions/run-1/topology-stream",
            params={"ticket": expired},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_ticket_reaches_topology_stream(
        self, client, db_session
    ) -> None:
        """Valid ticket -> 200 with text/event-stream content type."""
        token = await _login_token(client, db_session)
        ticket = await _issue_ticket(client, token)
        response = await _open_stream_once(
            client,
            "/api/v1/executions/run-1/topology-stream",
            params={"ticket": ticket},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

    @pytest.mark.asyncio
    async def test_valid_ticket_reaches_events_stream(
        self, client, db_session
    ) -> None:
        token = await _login_token(client, db_session)
        ticket = await _issue_ticket(client, token)
        response = await _open_stream_once(
            client,
            "/api/v1/executions/run-1/events",
            params={"ticket": ticket},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

    @pytest.mark.asyncio
    async def test_valid_ticket_reaches_offline_stream(
        self, client, db_session
    ) -> None:
        """Offline status stream: ticket passes auth; the immediate frame-0
        snapshot streams through (FakeService injected)."""
        from ate_cloud.api.v1.offline import _get_status_service

        class _FakeService:
            def status(self) -> dict[str, Any]:
                return {
                    "status": {"online": True, "pending_upload_count": 0},
                    "cache_health": None,
                }

        client.app.dependency_overrides[_get_status_service] = lambda: _FakeService()
        try:
            token = await _login_token(client, db_session)
            ticket = await _issue_ticket(client, token)
            result = await _probe_sse_endpoint(
                client.app,
                "/api/v1/offline/status/stream",
                params={"ticket": ticket},
                max_chunks=2,
            )
            assert result.status_code == 200
            assert result.content_type.startswith("text/event-stream")
            assert "offline_status" in result.body.decode("utf-8", errors="replace")
        finally:
            client.app.dependency_overrides.pop(_get_status_service, None)

    @pytest.mark.asyncio
    async def test_valid_ticket_reaches_replay_stream_404(
        self, client, db_session
    ) -> None:
        """Replay stream with a valid ticket but unknown run -> 404 (auth
        passed; the execution-existence check rejects the run id)."""
        token = await _login_token(client, db_session)
        ticket = await _issue_ticket(client, token)
        response = await client.get(
            "/api/v1/executions/nope/replay/stream",
            params={"ticket": ticket},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_deleted_user_ticket_401(self, client, db_session) -> None:
        """A ticket bound to a user deleted after issue -> 401."""
        user = await _create_admin_user(db_session)
        ticket = issue_sse_ticket(user.id)
        await db_session.delete(user)
        await db_session.flush()
        response = await client.get(
            "/api/v1/executions/run-1/topology-stream",
            params={"ticket": ticket},
        )
        assert response.status_code == 401
        assert "user" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_dev_mode_bypass_still_works(
        self, client, db_session, monkeypatch
    ) -> None:
        """ATE_DEV_MODE=true without a ticket still admits SSE streams
        (offline conftest dev-mode parity)."""
        monkeypatch.setattr(settings, "dev_mode", True)
        await _login_token(client, db_session)
        response = await _open_stream_once(
            client, "/api/v1/executions/run-1/topology-stream"
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")


# ---------------------------------------------------------------------------
# Mount-level sanity: SSE routers carry require_sse_user, not the JWT guard
# ---------------------------------------------------------------------------


def test_sse_mounts_carry_ticket_dependency() -> None:
    """Every SSE router mount carries require_sse_user at include time.

    Guards against regression where someone re-adds a stream endpoint to a
    JWT-protected router (EventSource cannot send headers -> 401 forever).
    """
    from ate_cloud.api.v1 import router as router_module

    def _dep_names(entry: Any) -> set[str]:
        ctx = getattr(entry, "include_context", None)
        return {
            getattr(d, "dependency", None).__name__ or ""
            for d in (getattr(ctx, "dependencies", None) or [])
        }

    mounts = list(router_module.api_router.routes)
    sse_mounts = [m for m in mounts if "require_sse_user" in _dep_names(m)]
    sse_router_ids = {id(m.include_context.included_router) for m in sse_mounts}

    assert len(sse_mounts) == 3
    assert id(router_module.executions_sse_router) in sse_router_ids
    assert id(router_module.offline_sse_router) in sse_router_ids
    assert id(router_module.recordings_sse_router) in sse_router_ids

    # None of the SSE mounts may carry the bearer guard (would break
    # header-less EventSource connections).
    for m in sse_mounts:
        assert "get_current_user" not in _dep_names(m)


def test_all_event_stream_routes_live_in_sse_routers() -> None:
    """Every EventSourceResponse route in the app is a ticket-guarded route.

    Greps the mounted app's routes for response_class EventSourceResponse
    and asserts each was registered on a *_sse_router (mount-level ticket
    auth) rather than a JWT-protected router.
    """
    from sse_starlette.sse import EventSourceResponse

    from ate_cloud.api.v1 import router as router_module

    guarded_routes = set()
    for sub in (
        router_module.executions_sse_router,
        router_module.offline_sse_router,
        router_module.recordings_sse_router,
    ):
        # Route paths carry the sub-router prefix (e.g. "/executions/...");
        # strip it so the assertion reads as bare handler path segments.
        prefix = getattr(sub, "prefix", "")
        for r in sub.routes:
            guarded_routes.add(r.path[len(prefix):] if prefix else r.path)

    found: set[str] = set()
    for sub in (router_module.executions_router, router_module.recordings_router, router_module.offline_router):
        for r in sub.routes:
            if getattr(r, "response_class", None) is EventSourceResponse:
                found.add(r.path)
    # No SSE endpoints may remain on the JWT-protected routers.
    assert found == set(), (
        f"SSE routes still on JWT-protected routers: {found} "
        f"(expected all in sse routers: {guarded_routes})"
    )
    # And the guarded set actually contains the four streams.
    assert guarded_routes == {
        "/{run_id}/events",
        "/{run_id}/topology-stream",
        "/{run_id}/replay/stream",
        "/status/stream",
    }
