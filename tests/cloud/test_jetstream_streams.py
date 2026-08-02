"""Tests for JetStream stream creation at startup (Todo 3).

Verifies that StreamManager creates two required streams:
1. ATE_TASKS — workqueue retention, subjects=["ate.tasks.*"]
2. ATE_STATUS — limits retention, max_age=7 days, subjects=["ate.status.*"]

And that creation is idempotent: existing streams are not re-created.

Per AGENTS.md: streams are created at startup, not lazily. Creation
failure is fatal (no silent degradation).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.api import RetentionPolicy
from nats.js.errors import NotFoundError

from ate_cloud.nats.stream_manager import StreamManager

_ATE_TASKS = "ATE_TASKS"
_ATE_STATUS = "ATE_STATUS"


def _make_mock_nc(not_found_streams: set[str]) -> MagicMock:
    """Build a mock NATS client with a JetStream context.

    ``stream_info`` raises ``NotFoundError`` for stream names in
    ``not_found_streams`` and succeeds (returns a truthy info object)
    for all others. ``add_stream`` is an ``AsyncMock`` so call assertions
    can be made.

    Args:
        not_found_streams: Stream names for which stream_info raises.
    """
    mock_js = MagicMock()
    mock_js.add_stream = AsyncMock(return_value=MagicMock())

    async def _stream_info(name: str) -> MagicMock:
        if name in not_found_streams:
            raise NotFoundError(f"stream '{name}' not found")
        return MagicMock()

    mock_js.stream_info = _stream_info

    mock_nc = MagicMock()
    # jetstream() is sync in nats-py — returns a JetStreamContext, not a coroutine.
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc


class TestJetStreamStreams:
    """Tests for StreamManager.create_streams()."""

    @pytest.mark.asyncio
    async def test_ate_tasks_stream_created(self) -> None:
        """ATE_TASKS stream is created with workqueue retention when absent."""
        mock_nc = _make_mock_nc({_ATE_TASKS})

        manager = StreamManager(mock_nc)
        await manager.create_streams()

        js = mock_nc.jetstream()
        js.add_stream.assert_awaited_once()
        config = js.add_stream.call_args.kwargs["config"]
        assert config.name == _ATE_TASKS
        assert config.subjects == ["ate.tasks.*"]
        assert config.retention == RetentionPolicy.WORK_QUEUE

    @pytest.mark.asyncio
    async def test_ate_status_stream_created(self) -> None:
        """ATE_STATUS stream is created with limits retention and 7-day max_age."""
        mock_nc = _make_mock_nc({_ATE_STATUS})

        manager = StreamManager(mock_nc)
        await manager.create_streams()

        js = mock_nc.jetstream()
        js.add_stream.assert_awaited_once()
        config = js.add_stream.call_args.kwargs["config"]
        assert config.name == _ATE_STATUS
        assert config.subjects == ["ate.status.*"]
        assert config.retention == RetentionPolicy.LIMITS
        assert config.max_age == 7 * 24 * 60 * 60

    @pytest.mark.asyncio
    async def test_stream_creation_idempotent(self) -> None:
        """Existing streams are not re-created — add_stream is never called."""
        mock_nc = _make_mock_nc(set())

        manager = StreamManager(mock_nc)
        await manager.create_streams()

        js = mock_nc.jetstream()
        js.add_stream.assert_not_awaited()
