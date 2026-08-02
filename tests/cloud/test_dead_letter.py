"""Tests for the ATE_DEAD_LETTERS stream and consumer DLQ policy (Todo 5).

Verifies that StreamManager:
1. Creates ATE_DEAD_LETTERS stream with limits retention + subjects=["ate.tasks.*.dlq"]
2. Configures the ATE_TASKS "ate-worker" consumer with dead-letter routing metadata
3. Sets a 30-day max_age on the DLQ stream

nats-py 2.15.0 has no native dead_letter_policy field on ConsumerConfig,
and NATS JetStream has no server-side auto-routing to a DLQ stream. The
consumer's ``metadata`` field (a supported nats-py / NATS 2.10+ feature)
records the DLQ routing policy so application code or an advisory
subscriber can publish failed messages to ATE_DEAD_LETTERS after max_deliver
is exhausted.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.api import RetentionPolicy
from nats.js.errors import NotFoundError

from ate_cloud.nats.stream_manager import StreamManager

_ATE_DEAD_LETTERS = "ATE_DEAD_LETTERS"
_ATE_TASKS = "ATE_TASKS"
_ATE_TASKS_DURABLE = "ate-worker"

_30_DAYS_SECONDS: int = 30 * 24 * 60 * 60


def _make_mock_nc_stream(not_found_streams: set[str]) -> MagicMock:
    """Build a mock NATS client for DLQ stream creation tests.

    ``stream_info`` raises ``NotFoundError`` for stream names in
    ``not_found_streams`` and succeeds for all others.

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
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc


def _make_mock_nc_consumer(not_found: set[tuple[str, str]]) -> MagicMock:
    """Build a mock NATS client for consumer creation tests.

    ``consumer_info`` raises ``NotFoundError`` for (stream, durable) pairs
    in ``not_found`` and succeeds for all others.

    Args:
        not_found: (stream, durable) pairs for which consumer_info raises.
    """
    mock_js = MagicMock()
    mock_js.add_consumer = AsyncMock(return_value=MagicMock())

    async def _consumer_info(stream: str, consumer: str) -> MagicMock:
        if (stream, consumer) in not_found:
            raise NotFoundError(f"consumer '{consumer}' not found on stream '{stream}'")
        return MagicMock()

    mock_js.consumer_info = _consumer_info

    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc


class TestDeadLetter:
    """Tests for ATE_DEAD_LETTERS stream and consumer DLQ policy (Todo 5)."""

    @pytest.mark.asyncio
    async def test_dead_letter_stream_created(self) -> None:
        """ATE_DEAD_LETTERS created with limits retention + subjects when absent."""
        mock_nc = _make_mock_nc_stream({_ATE_DEAD_LETTERS})

        manager = StreamManager(mock_nc)
        await manager.create_dead_letter_stream()

        js = mock_nc.jetstream()
        js.add_stream.assert_awaited_once()
        config = js.add_stream.call_args.kwargs["config"]
        assert config.name == _ATE_DEAD_LETTERS
        assert config.subjects == ["ate.tasks.*.dlq"]
        assert config.retention == RetentionPolicy.LIMITS

    @pytest.mark.asyncio
    async def test_consumer_has_dead_letter_policy(self) -> None:
        """ATE_TASKS 'ate-worker' consumer carries DLQ routing metadata.

        nats-py 2.15.0 has no dead_letter_policy field. The consumer's
        ``metadata`` dict records the dead-letter stream, subject, and
        max_deliver threshold so application code or an advisory subscriber
        can route failed messages to ATE_DEAD_LETTERS.
        """
        mock_nc = _make_mock_nc_consumer({(_ATE_TASKS, _ATE_TASKS_DURABLE)})

        manager = StreamManager(mock_nc)
        await manager.create_consumers()

        js = mock_nc.jetstream()
        js.add_consumer.assert_awaited_once()
        args, kwargs = js.add_consumer.call_args
        assert args[0] == _ATE_TASKS
        config = kwargs["config"]
        assert config.durable_name == _ATE_TASKS_DURABLE
        assert config.metadata is not None
        assert config.metadata["dead_letter_stream"] == _ATE_DEAD_LETTERS
        assert config.metadata["dead_letter_subject"] == "ate.tasks.dlq"
        assert config.metadata["max_deliver"] == "3"

    @pytest.mark.asyncio
    async def test_dead_letter_stream_30_day_ttl(self) -> None:
        """ATE_DEAD_LETTERS stream has a 30-day max_age (in seconds)."""
        mock_nc = _make_mock_nc_stream({_ATE_DEAD_LETTERS})

        manager = StreamManager(mock_nc)
        await manager.create_dead_letter_stream()

        js = mock_nc.jetstream()
        js.add_stream.assert_awaited_once()
        config = js.add_stream.call_args.kwargs["config"]
        assert config.max_age == _30_DAYS_SECONDS
