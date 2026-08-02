"""Tests for NATS client connection in FastAPI lifespan startup.

Verifies that the lifespan:
1. Connects to NATS on startup and exposes the client via get_nats()
2. Raises a fatal error when NATS is unavailable (no silent degradation)
3. Properly closes the NATS connection on shutdown

Per AGENTS.md: NATS is required, not optional. Startup must crash on failure.
"""

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ate_cloud.main as main_module
from ate_cloud.main import get_nats, lifespan


@pytest.fixture
def mock_app() -> MagicMock:
    """Create a mock FastAPI app with a state attribute for lifespan."""
    app = MagicMock()
    app.state = MagicMock()
    return app


@pytest.fixture(autouse=True)
def reset_nats_client() -> Generator[None, None, None]:
    """Reset the module-level _nats_client to None before and after each test."""
    saved = main_module._nats_client
    main_module._nats_client = None
    yield
    main_module._nats_client = saved


class TestNATSStartupLifespan:
    """Tests for NATS connection lifecycle in the FastAPI lifespan."""

    @pytest.mark.asyncio
    async def test_nats_connection_success(self, mock_app: MagicMock) -> None:
        """App starts with NATS available; get_nats() returns the connected client."""
        mock_nc = AsyncMock()
        mock_nc.is_connected = True
        # jetstream() is sync in nats-py — returns a JetStreamContext, not a coroutine.
        # account_info() is async — verify JetStream is available on startup (Todo 2).
        mock_js = AsyncMock()
        mock_js.account_info = AsyncMock(return_value=MagicMock())
        mock_nc.jetstream = MagicMock(return_value=mock_js)
        mock_bridge = AsyncMock()
        # FailureIndexer.ensure_collection is async; subscribe_to_events is sync.
        # Use MagicMock so sync methods don't emit un-awaited-coroutine warnings.
        mock_indexer = MagicMock()
        mock_indexer.ensure_collection = AsyncMock()

        with (
            patch("ate_cloud.main.nats.connect", new=AsyncMock(return_value=mock_nc)),
            patch("ate_cloud.main.SSEBridge", return_value=mock_bridge),
            patch("ate_cloud.main.FailureIndexer", return_value=mock_indexer),
            patch("ate_cloud.main.ScriptVersioningService"),
        ):
            async with lifespan(mock_app):
                # During startup, the module-level client must be the connected one
                client = get_nats()
                assert client is mock_nc
                assert client.is_connected is True

            # On shutdown, the NATS client must be closed
            mock_nc.close.assert_awaited()

    @pytest.mark.asyncio
    async def test_nats_connection_failure_crashes(self, mock_app: MagicMock) -> None:
        """App startup raises a fatal RuntimeError when NATS is unavailable."""
        connection_error = OSError("Connection refused")

        with (
            patch("ate_cloud.main.nats.connect", new=AsyncMock(side_effect=connection_error)),
            patch("ate_cloud.main.SSEBridge"),
            patch("ate_cloud.main.FailureIndexer"),
            patch("ate_cloud.main.ScriptVersioningService"),
        ):
            with pytest.raises(RuntimeError, match="Failed to connect to NATS"):
                async with lifespan(mock_app):
                    pass  # Should never reach here — startup must crash

        # After failure, get_nats() must raise because no client is connected
        with pytest.raises(RuntimeError, match="NATS client not connected"):
            get_nats()

    @pytest.mark.asyncio
    async def test_get_nats_returns_connected_client(self) -> None:
        """get_nats() returns the module-level client when connected, raises when not."""
        # When not connected, must raise RuntimeError
        main_module._nats_client = None
        with pytest.raises(RuntimeError, match="NATS client not connected"):
            get_nats()

        # When connected, must return the exact module-level client instance
        mock_nc = AsyncMock()
        main_module._nats_client = mock_nc
        assert get_nats() is mock_nc
