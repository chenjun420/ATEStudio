"""Tests for JetStream consumer and KV registry creation at startup (Todo 4).

Verifies that StreamManager creates:
1. Durable pull consumer "ate-worker" on ATE_TASKS (ack_wait=300s, max_deliver=3)
2. Durable pull consumer "ate-status-relay" on ATE_STATUS (ack_wait=30s, filter_subject)
3. Worker KV registry — register_worker() puts JSON metadata at "workers.{id}"
4. KV store created with TTL=30s for heartbeat expiry

Per AGENTS.md §7: consumer/KV creation is enforced at startup, not lazily.
Creation failure is fatal (no silent degradation). Existing resources are
left unchanged (idempotent).
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.api import AckPolicy
from nats.js.errors import NotFoundError

from ate_cloud.nats.stream_manager import StreamManager

_ATE_TASKS = "ATE_TASKS"
_ATE_STATUS = "ATE_STATUS"
_ATE_TASKS_DURABLE = "ate-worker"
_ATE_STATUS_DURABLE = "ate-status-relay"
_WORKER_KV_BUCKET = "ate-workers"


def _make_mock_nc_consumers(not_found: set[tuple[str, str]]) -> MagicMock:
    """Build a mock NATS client for consumer creation tests.

    ``consumer_info`` raises ``NotFoundError`` for (stream, durable) pairs
    in ``not_found`` and succeeds for all others. ``add_consumer`` is an
    ``AsyncMock`` so call assertions can be made.

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
    # jetstream() is sync in nats-py — returns a JetStreamContext, not a coroutine.
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc


def _make_mock_nc_kv(bucket_missing: bool) -> tuple[MagicMock, MagicMock]:
    """Build a mock NATS client and mock KV for KV store tests.

    If ``bucket_missing``, ``key_value`` raises ``NotFoundError``;
    otherwise it returns the mock KV.

    Returns:
        (mock_nc, mock_kv) — the mock client and the mock KeyValue object.
    """
    mock_kv = MagicMock()
    mock_kv.put = AsyncMock(return_value=1)

    mock_js = MagicMock()

    if bucket_missing:

        async def _key_value_missing(bucket: str) -> None:
            raise NotFoundError(f"bucket '{bucket}' not found")

        mock_js.key_value = _key_value_missing
    else:

        async def _key_value_present(bucket: str) -> MagicMock:
            return mock_kv

        mock_js.key_value = _key_value_present

    mock_js.create_key_value = AsyncMock(return_value=mock_kv)

    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc, mock_kv


class TestJetStreamConsumers:
    """Tests for StreamManager consumer + KV creation (Todo 4)."""

    @pytest.mark.asyncio
    async def test_durable_consumer_created(self) -> None:
        """ATE_TASKS durable consumer 'ate-worker' created with ack_wait=300, max_deliver=3."""
        # Only ATE_TASKS consumer is "not found" → ATE_STATUS consumer "exists" (skipped).
        mock_nc = _make_mock_nc_consumers({(_ATE_TASKS, _ATE_TASKS_DURABLE)})

        manager = StreamManager(mock_nc)
        await manager.create_consumers()

        js = mock_nc.jetstream()
        js.add_consumer.assert_awaited_once()
        args, kwargs = js.add_consumer.call_args
        assert args[0] == _ATE_TASKS
        config = kwargs["config"]
        assert config.durable_name == _ATE_TASKS_DURABLE
        assert config.ack_policy == AckPolicy.EXPLICIT
        assert config.ack_wait == 300
        assert config.max_deliver == 3

    @pytest.mark.asyncio
    async def test_ate_status_consumer_created(self) -> None:
        """ATE_STATUS consumer 'ate-status-relay' created with ack_wait=30, filter_subject."""
        # Only ATE_STATUS consumer is "not found" → ATE_TASKS consumer "exists" (skipped).
        mock_nc = _make_mock_nc_consumers({(_ATE_STATUS, _ATE_STATUS_DURABLE)})

        manager = StreamManager(mock_nc)
        await manager.create_consumers()

        js = mock_nc.jetstream()
        js.add_consumer.assert_awaited_once()
        args, kwargs = js.add_consumer.call_args
        assert args[0] == _ATE_STATUS
        config = kwargs["config"]
        assert config.durable_name == _ATE_STATUS_DURABLE
        assert config.ack_policy == AckPolicy.EXPLICIT
        assert config.ack_wait == 30
        assert config.filter_subject == "ate.status.*"

    @pytest.mark.asyncio
    async def test_worker_kv_registry(self) -> None:
        """register_worker puts JSON metadata at key 'workers.{worker_id}'.

        KV bucket already exists (create_kv_store is idempotent — skips
        recreation), then register_worker puts the serialized metadata.
        """
        mock_nc, mock_kv = _make_mock_nc_kv(bucket_missing=False)

        manager = StreamManager(mock_nc)
        await manager.create_kv_store()

        js = mock_nc.jetstream()
        js.create_key_value.assert_not_awaited()

        metadata = {
            "hostname": "worker-1",
            "capabilities": ["dmm", "scope"],
            "started_at": "2026-07-31T00:00:00Z",
            "max_concurrent_tasks": 4,
            "current_tasks": [],
        }
        revision = await manager.register_worker("worker-1", metadata)

        mock_kv.put.assert_awaited_once()
        args, _ = mock_kv.put.call_args
        assert args[0] == "workers.worker-1"
        assert json.loads(args[1].decode("utf-8")) == metadata
        assert revision == 1

    @pytest.mark.asyncio
    async def test_worker_heartbeat_ttl(self) -> None:
        """KV store created with TTL=30s for worker heartbeat expiry."""
        mock_nc, _ = _make_mock_nc_kv(bucket_missing=True)

        manager = StreamManager(mock_nc)
        await manager.create_kv_store()

        js = mock_nc.jetstream()
        js.create_key_value.assert_awaited_once()
        kwargs = js.create_key_value.call_args.kwargs
        assert kwargs["bucket"] == _WORKER_KV_BUCKET
        assert kwargs["ttl"] == 30
