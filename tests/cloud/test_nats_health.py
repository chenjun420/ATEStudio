"""Tests for NATS health check endpoint and JetStream startup validation.

Verifies:
1. GET /api/v1/health/nats returns NATS connection status when connected
2. JetStream availability and worker registry KV status are reported correctly
3. Startup crashes when JetStream is unavailable (per AGENTS.md §7)

Per AGENTS.md: NATS and JetStream are required, not optional. No silent degradation.
The health endpoint reports status; the lifespan pre-check crashes fatally.
"""

from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from nats.js.errors import NotFoundError

import ate_cloud.main as main_module
from ate_cloud.main import create_app, lifespan

# KV bucket name — must match the constant in health.py
_WORKER_KV_BUCKET = "ate-workers"


@pytest.fixture(autouse=True)
def reset_nats_client() -> Generator[None, None, None]:
    """Reset the module-level _nats_client before and after each test."""
    saved = main_module._nats_client
    main_module._nats_client = None
    yield
    main_module._nats_client = saved


def _make_mock_nc(
    jetstream_available: bool = True,
    kv_exists: bool = False,
) -> AsyncMock:
    """Build a mock NATS client with a JetStream context.

    Args:
        jetstream_available: Whether ``js.account_info()`` succeeds.
        kv_exists: Whether the worker KV bucket exists.
    """
    mock_nc = AsyncMock()
    mock_nc.is_connected = True
    mock_nc.close = AsyncMock()

    mock_js = AsyncMock()
    if jetstream_available:
        mock_js.account_info = AsyncMock(return_value=MagicMock())
    else:
        mock_js.account_info = AsyncMock(side_effect=Exception("JetStream not enabled"))

    if kv_exists:
        mock_js.key_value = AsyncMock(return_value=AsyncMock())
    else:
        mock_js.key_value = AsyncMock(side_effect=NotFoundError("bucket not found"))

    # jetstream() is a sync factory — returns the context without network I/O
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc


@pytest.fixture
async def nats_health_client() -> AsyncGenerator[AsyncClient, None]:
    """HTTP client with a mocked connected NATS client for /health/nats tests.

    Sets up: connected NATS client, JetStream available, worker KV absent
    (matches the post-Todo-2 / pre-Todo-4 state).
    """
    mock_nc = _make_mock_nc(jetstream_available=True, kv_exists=False)
    main_module._nats_client = mock_nc

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestNATSHealthEndpoint:
    """Tests for GET /api/v1/health/nats."""

    @pytest.mark.asyncio
    async def test_health_nats_connected(self, nats_health_client: AsyncClient) -> None:
        """Endpoint returns 200 with nats_connected=true when client is connected."""
        response = await nats_health_client.get("/api/v1/health/nats")
        assert response.status_code == 200
        data = response.json()
        assert data["nats_connected"] is True

    @pytest.mark.asyncio
    async def test_health_nats_jetstream_available(self, nats_health_client: AsyncClient) -> None:
        """Endpoint reports jetstream_available=true and KV not_initialized.

        The worker registry KV bucket does not exist yet (Todo 4 creates it);
        the endpoint reports ``"not_initialized"`` rather than failing.
        """
        response = await nats_health_client.get("/api/v1/health/nats")
        assert response.status_code == 200
        data = response.json()
        assert data["jetstream_available"] is True
        assert data["worker_registry_kv"] == "not_initialized"


class TestStartupJetStreamValidation:
    """Tests for lifespan JetStream pre-check (crashes if unavailable)."""

    @pytest.mark.asyncio
    async def test_startup_crashes_without_jetstream(self) -> None:
        """Lifespan raises RuntimeError when JetStream account_info fails.

        NATS connect succeeds, but JetStream is not enabled on the server.
        Per AGENTS.md §7, this is a fatal error — no silent degradation.
        """
        # Mock NATS client: connected, but JetStream account_info raises
        mock_nc = AsyncMock()
        mock_nc.is_connected = True
        mock_nc.close = AsyncMock()
        mock_js = AsyncMock()
        mock_js.account_info = AsyncMock(side_effect=Exception("JetStream not enabled"))
        mock_nc.jetstream = MagicMock(return_value=mock_js)

        mock_app = MagicMock()
        mock_app.state = MagicMock()

        with (
            patch("ate_cloud.main.nats.connect", new=AsyncMock(return_value=mock_nc)),
            patch("ate_cloud.main.SSEBridge"),
            patch("ate_cloud.main.FailureIndexer"),
            patch("ate_cloud.main.ScriptVersioningService"),
        ):
            with pytest.raises(RuntimeError, match="JetStream not available"):
                async with lifespan(mock_app):
                    pass  # Should never reach here — startup must crash
