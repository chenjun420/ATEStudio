"""Tests for ConfigDistributionService and ConfigWatcher (Todo 10).

Verifies:
1. ConfigDistributionService.put_config — writes to the correct KV key.
2. ConfigDistributionService.get_config — reads from KV, returns None for missing.
3. ConfigDistributionService.get_all_config — lists all keys for a worker.
4. ConfigDistributionService.put_batch — puts multiple keys.
5. ConfigDistributionService.ensure_bucket — creates bucket if not exists.
6. KV bucket name follows convention (``ate-configs``).
7. Key pattern follows convention (``workers.{worker_id}.{config_key}``).
8. ConfigWatcher.start/stop — connects and cleans up.
9. ConfigWatcher watch loop — invokes callback on config change.
10. ConfigWatcher handles None marker and delete operations.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.errors import KeyNotFoundError, NoKeysError, NotFoundError

from ate_cloud.services.config_distribution import (
    CONFIG_KV_BUCKET,
    ConfigDistributionService,
    _worker_config_key,
)
from ate_platform.scheduler.config_watcher import ConfigWatcher

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
        operation: str | None = None,
    ) -> None:
        self.bucket = CONFIG_KV_BUCKET
        self.key = key
        self.value = value
        self.revision = revision
        self.delta = 0
        self.created = None
        self.operation = operation


class FakeKeyWatcher:
    """Mimics nats-py KeyWatcher async iterator for testing.

    Yields the provided entries in order. A ``None`` entry in the list
    represents the end-of-initial-values marker (matching nats-py behavior).
    """

    def __init__(self, entries: list[Any]) -> None:
        self._entries = list(entries)
        self._stopped = False
        self._stop_called = False

    def __aiter__(self) -> FakeKeyWatcher:
        return self

    async def __anext__(self) -> Any:
        if self._entries:
            return self._entries.pop(0)
        # Block until stopped — simulates nats-py's queue-based behavior.
        while not self._stop_called:
            await asyncio.sleep(0.01)
        raise StopAsyncIteration

    async def stop(self) -> None:
        self._stop_called = True


def _make_mock_kv(
    put_return: int = 1,
    existing_keys: list[str] | None = None,
    entries: dict[str, bytes] | None = None,
) -> MagicMock:
    """Build a mock KV store matching nats-py KeyValue API."""
    kv = MagicMock()
    kv.put = AsyncMock(return_value=put_return)

    _entries = entries or {}
    _keys_list = existing_keys or list(_entries.keys())

    async def _get(key: str) -> Any:
        if key in _entries:
            return FakeKVEntry(key=key, value=_entries[key])
        raise KeyNotFoundError(f"key '{key}' not found")

    async def _list_keys() -> list[str]:
        if not _keys_list:
            raise NoKeysError("no keys")
        return list(_keys_list)

    kv.get = AsyncMock(side_effect=_get)
    kv.keys = AsyncMock(side_effect=_list_keys)
    return kv


def _make_mock_nc(kv: MagicMock | None = None) -> tuple[MagicMock, MagicMock]:
    """Build a mock NATS client + JetStream context.

    ``jetstream()`` is sync (returns JetStreamContext without I/O) and
    ``key_value`` / ``create_key_value`` are async — matching nats-py's API.
    """
    mock_js = MagicMock()
    if kv is not None:
        mock_js.key_value = AsyncMock(return_value=kv)
        mock_js.create_key_value = AsyncMock(return_value=kv)
    else:
        mock_js.key_value = AsyncMock(side_effect=NotFoundError("no bucket"))
        mock_js.create_key_value = AsyncMock(return_value=MagicMock())
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc, mock_js


def _make_mock_nc_existing(kv: MagicMock) -> tuple[MagicMock, MagicMock]:
    """Build mock NC where the KV bucket already exists."""
    mock_js = MagicMock()
    mock_js.key_value = AsyncMock(return_value=kv)
    mock_js.create_key_value = AsyncMock(return_value=kv)
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc, mock_js


# ---------------------------------------------------------------------------
# ConfigDistributionService tests
# ---------------------------------------------------------------------------


class TestConfigDistributionService:
    """Tests for ConfigDistributionService put/get/get_all/put_batch."""

    @pytest.mark.asyncio
    async def test_put_config_writes_correct_key(self) -> None:
        """put_config writes to workers.{worker_id}.{config_key} in ate-configs."""
        kv = _make_mock_kv(put_return=42)
        mock_nc, _ = _make_mock_nc_existing(kv)
        service = ConfigDistributionService(mock_nc)

        revision = await service.put_config("worker-001", "instrument.rate", "9600")

        assert revision == 42
        kv.put.assert_awaited_once_with(
            "workers.worker-001.instrument.rate",
            b"9600",
        )

    @pytest.mark.asyncio
    async def test_put_config_encodes_value_as_utf8(self) -> None:
        """put_config encodes the string value as UTF-8 bytes."""
        kv = _make_mock_kv()
        mock_nc, _ = _make_mock_nc_existing(kv)
        service = ConfigDistributionService(mock_nc)

        await service.put_config("w1", "key1", '{"sample_rate": 9600}')

        _, kwargs = kv.put.call_args
        call_args = kv.put.call_args
        payload = call_args.args[1]
        assert payload == b'{"sample_rate": 9600}'

    @pytest.mark.asyncio
    async def test_put_config_raises_runtime_error_on_failure(self) -> None:
        """put_config raises RuntimeError if the KV put fails."""
        kv = MagicMock()
        kv.put = AsyncMock(side_effect=Exception("NATS connection lost"))
        mock_nc, _ = _make_mock_nc_existing(kv)
        service = ConfigDistributionService(mock_nc)

        with pytest.raises(RuntimeError, match="Failed to put config"):
            await service.put_config("w1", "key1", "val1")

    @pytest.mark.asyncio
    async def test_get_config_returns_value(self) -> None:
        """get_config returns the decoded string value for an existing key."""
        kv = _make_mock_kv(
            entries={"workers.w1.instrument.rate": b"9600"},
        )
        mock_nc, _ = _make_mock_nc_existing(kv)
        service = ConfigDistributionService(mock_nc)

        value = await service.get_config("w1", "instrument.rate")

        assert value == "9600"
        kv.get.assert_awaited_once_with("workers.w1.instrument.rate")

    @pytest.mark.asyncio
    async def test_get_config_returns_none_for_missing_key(self) -> None:
        """get_config returns None when the key doesn't exist."""
        kv = _make_mock_kv(entries={})
        mock_nc, _ = _make_mock_nc_existing(kv)
        service = ConfigDistributionService(mock_nc)

        value = await service.get_config("w1", "nonexistent")

        assert value is None

    @pytest.mark.asyncio
    async def test_get_config_raises_runtime_error_on_failure(self) -> None:
        """get_config raises RuntimeError for non-NotFoundError exceptions."""
        kv = MagicMock()
        kv.get = AsyncMock(side_effect=Exception("KV unavailable"))
        mock_nc, _ = _make_mock_nc_existing(kv)
        service = ConfigDistributionService(mock_nc)

        with pytest.raises(RuntimeError, match="Failed to get config"):
            await service.get_config("w1", "key1")

    @pytest.mark.asyncio
    async def test_get_all_config_returns_all_keys_for_worker(self) -> None:
        """get_all_config returns all config keys (without prefix) for a worker."""
        kv = _make_mock_kv(
            existing_keys=[
                "workers.w1.instrument.rate",
                "workers.w1.instrument.gain",
                "workers.w2.instrument.rate",  # Different worker — should be excluded
            ],
            entries={
                "workers.w1.instrument.rate": b"9600",
                "workers.w1.instrument.gain": b"0.5",
                "workers.w2.instrument.rate": b"115200",
            },
        )
        mock_nc, _ = _make_mock_nc_existing(kv)
        service = ConfigDistributionService(mock_nc)

        configs = await service.get_all_config("w1")

        assert configs == {
            "instrument.rate": "9600",
            "instrument.gain": "0.5",
        }

    @pytest.mark.asyncio
    async def test_get_all_config_returns_empty_for_no_configs(self) -> None:
        """get_all_config returns empty dict when the worker has no configs."""
        kv = _make_mock_kv(existing_keys=[], entries={})
        mock_nc, _ = _make_mock_nc_existing(kv)
        service = ConfigDistributionService(mock_nc)

        configs = await service.get_all_config("w1")

        assert configs == {}

    @pytest.mark.asyncio
    async def test_get_all_config_raises_runtime_error_on_failure(self) -> None:
        """get_all_config raises RuntimeError if kv.keys() fails unexpectedly."""
        kv = MagicMock()
        kv.keys = AsyncMock(side_effect=Exception("KV error"))
        mock_nc, _ = _make_mock_nc_existing(kv)
        service = ConfigDistributionService(mock_nc)

        with pytest.raises(RuntimeError, match="Failed to list keys"):
            await service.get_all_config("w1")

    @pytest.mark.asyncio
    async def test_put_batch_puts_all_keys(self) -> None:
        """put_batch puts all key-value pairs and returns revision numbers."""
        kv = _make_mock_kv(put_return=1)
        # Make put return incrementing revisions
        revisions = iter([10, 11, 12])
        kv.put = AsyncMock(side_effect=lambda k, v: next(revisions))
        mock_nc, _ = _make_mock_nc_existing(kv)
        service = ConfigDistributionService(mock_nc)

        configs = {
            "instrument.rate": "9600",
            "instrument.gain": "0.5",
            "instrument.channel": "1",
        }
        result = await service.put_batch("w1", configs)

        assert result == [10, 11, 12]
        assert kv.put.await_count == 3
        # Verify each key was written with the correct prefix
        called_keys = [call.args[0] for call in kv.put.call_args_list]
        assert "workers.w1.instrument.rate" in called_keys
        assert "workers.w1.instrument.gain" in called_keys
        assert "workers.w1.instrument.channel" in called_keys

    @pytest.mark.asyncio
    async def test_put_batch_raises_runtime_error_on_failure(self) -> None:
        """put_batch raises RuntimeError if any put fails."""
        kv = MagicMock()
        kv.put = AsyncMock(side_effect=Exception("KV write failed"))
        mock_nc, _ = _make_mock_nc_existing(kv)
        service = ConfigDistributionService(mock_nc)

        with pytest.raises(RuntimeError, match="Failed to put config"):
            await service.put_batch("w1", {"key1": "val1", "key2": "val2"})

    @pytest.mark.asyncio
    async def test_ensure_bucket_creates_if_not_exists(self) -> None:
        """ensure_bucket creates the KV bucket when it doesn't exist."""
        mock_kv = MagicMock()
        mock_nc = MagicMock()
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(side_effect=NotFoundError("no bucket"))
        mock_js.create_key_value = AsyncMock(return_value=mock_kv)
        mock_nc.jetstream = MagicMock(return_value=mock_js)
        service = ConfigDistributionService(mock_nc)

        await service.ensure_bucket()

        mock_js.key_value.assert_awaited_once_with(CONFIG_KV_BUCKET)
        mock_js.create_key_value.assert_awaited_once_with(bucket=CONFIG_KV_BUCKET)

    @pytest.mark.asyncio
    async def test_ensure_bucket_skips_if_exists(self) -> None:
        """ensure_bucket does not create the bucket if it already exists."""
        mock_kv = MagicMock()
        mock_nc = MagicMock()
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(return_value=mock_kv)
        mock_js.create_key_value = AsyncMock(return_value=mock_kv)
        mock_nc.jetstream = MagicMock(return_value=mock_js)
        service = ConfigDistributionService(mock_nc)

        await service.ensure_bucket()

        mock_js.key_value.assert_awaited_once_with(CONFIG_KV_BUCKET)
        mock_js.create_key_value.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_bucket_raises_runtime_error_on_failure(self) -> None:
        """ensure_bucket raises RuntimeError if creation fails."""
        mock_nc = MagicMock()
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(side_effect=NotFoundError("no bucket"))
        mock_js.create_key_value = AsyncMock(side_effect=Exception("NATS error"))
        mock_nc.jetstream = MagicMock(return_value=mock_js)
        service = ConfigDistributionService(mock_nc)

        with pytest.raises(RuntimeError, match="Failed to create KV bucket"):
            await service.ensure_bucket()


class TestConfigDistributionNaming:
    """Tests for KV bucket name and key pattern conventions."""

    def test_kv_bucket_name_follows_convention(self) -> None:
        """KV bucket name is 'ate-configs' (lower-kebab, per NATS naming)."""
        assert CONFIG_KV_BUCKET == "ate-configs"

    def test_key_pattern_follows_convention(self) -> None:
        """Key pattern is workers.{worker_id}.{config_key} (lower.dot)."""
        key = _worker_config_key("worker-001", "instrument.sample_rate")
        assert key == "workers.worker-001.instrument.sample_rate"

    def test_key_pattern_with_simple_config_key(self) -> None:
        """Key pattern works with simple (non-dotted) config keys."""
        key = _worker_config_key("w1", "timeout")
        assert key == "workers.w1.timeout"


# ---------------------------------------------------------------------------
# ConfigWatcher tests
# ---------------------------------------------------------------------------


class TestConfigWatcher:
    """Tests for ConfigWatcher start/stop/watch loop."""

    @pytest.mark.asyncio
    async def test_start_connects_and_watches(self) -> None:
        """start() connects to NATS and begins watching the correct key filter."""
        fake_watcher = FakeKeyWatcher(entries=[])
        mock_kv = MagicMock()
        mock_kv.watch = AsyncMock(return_value=fake_watcher)
        mock_nc = MagicMock()
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(return_value=mock_kv)
        mock_nc.jetstream = MagicMock(return_value=mock_js)

        watcher = ConfigWatcher(
            worker_id="worker-001",
            on_config_change=AsyncMock(),
        )
        await watcher.start(nc=mock_nc)

        # Verify the watch filter matches the worker's key prefix
        mock_kv.watch.assert_awaited_once_with(keys="workers.worker-001.>")
        assert watcher._watch_task is not None

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_start_raises_if_already_running(self) -> None:
        """start() raises RuntimeError if already running."""
        fake_watcher = FakeKeyWatcher(entries=[])
        mock_kv = MagicMock()
        mock_kv.watch = AsyncMock(return_value=fake_watcher)
        mock_nc = MagicMock()
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(return_value=mock_kv)
        mock_nc.jetstream = MagicMock(return_value=mock_js)

        watcher = ConfigWatcher(worker_id="w1", on_config_change=AsyncMock())
        await watcher.start(nc=mock_nc)

        with pytest.raises(RuntimeError, match="already running"):
            await watcher.start(nc=mock_nc)

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_start_raises_if_worker_id_empty(self) -> None:
        """start() raises RuntimeError if worker_id is not set."""
        watcher = ConfigWatcher(worker_id="", on_config_change=AsyncMock())

        with pytest.raises(RuntimeError, match="worker_id must be set"):
            await watcher.start()

    @pytest.mark.asyncio
    async def test_watch_loop_invokes_callback_on_config_change(self) -> None:
        """Watch loop invokes callback with config_key and value on PUT."""
        entry = FakeKVEntry(
            key="workers.w1.instrument.rate",
            value=b"9600",
            revision=5,
        )
        fake_watcher = FakeKeyWatcher(entries=[entry])
        mock_kv = MagicMock()
        mock_kv.watch = AsyncMock(return_value=fake_watcher)
        mock_nc = MagicMock()
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(return_value=mock_kv)
        mock_nc.jetstream = MagicMock(return_value=mock_js)

        callback = AsyncMock()
        watcher = ConfigWatcher(worker_id="w1", on_config_change=callback)
        await watcher.start(nc=mock_nc)

        # Wait for the callback to be invoked
        await asyncio.sleep(0.1)

        callback.assert_awaited_once_with("instrument.rate", "9600")

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_watch_loop_strips_worker_prefix_from_key(self) -> None:
        """Callback receives config_key without the workers.{worker_id}. prefix."""
        entry = FakeKVEntry(
            key="workers.worker-42.calibration.offset",
            value=b"0.001",
        )
        fake_watcher = FakeKeyWatcher(entries=[entry])
        mock_kv = MagicMock()
        mock_kv.watch = AsyncMock(return_value=fake_watcher)
        mock_nc = MagicMock()
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(return_value=mock_kv)
        mock_nc.jetstream = MagicMock(return_value=mock_js)

        callback = AsyncMock()
        watcher = ConfigWatcher(worker_id="worker-42", on_config_change=callback)
        await watcher.start(nc=mock_nc)

        await asyncio.sleep(0.1)

        callback.assert_awaited_once_with("calibration.offset", "0.001")

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_watch_loop_skips_none_marker(self) -> None:
        """Watch loop skips None entries (end-of-initial-values marker)."""
        none_marker = None
        entry = FakeKVEntry(
            key="workers.w1.key1",
            value=b"val1",
        )
        fake_watcher = FakeKeyWatcher(entries=[none_marker, entry])
        mock_kv = MagicMock()
        mock_kv.watch = AsyncMock(return_value=fake_watcher)
        mock_nc = MagicMock()
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(return_value=mock_kv)
        mock_nc.jetstream = MagicMock(return_value=mock_js)

        callback = AsyncMock()
        watcher = ConfigWatcher(worker_id="w1", on_config_change=callback)
        await watcher.start(nc=mock_nc)

        await asyncio.sleep(0.1)

        # Only the real entry should trigger the callback, not the None marker
        callback.assert_awaited_once_with("key1", "val1")

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_watch_loop_skips_delete_operations(self) -> None:
        """Watch loop skips DEL/PURGE operations (only processes PUTs)."""
        delete_entry = FakeKVEntry(
            key="workers.w1.key1",
            value=b"",
            operation="DEL",
        )
        put_entry = FakeKVEntry(
            key="workers.w1.key2",
            value=b"val2",
            operation=None,
        )
        fake_watcher = FakeKeyWatcher(entries=[delete_entry, put_entry])
        mock_kv = MagicMock()
        mock_kv.watch = AsyncMock(return_value=fake_watcher)
        mock_nc = MagicMock()
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(return_value=mock_kv)
        mock_nc.jetstream = MagicMock(return_value=mock_js)

        callback = AsyncMock()
        watcher = ConfigWatcher(worker_id="w1", on_config_change=callback)
        await watcher.start(nc=mock_nc)

        await asyncio.sleep(0.1)

        # Only the PUT entry should trigger the callback
        callback.assert_awaited_once_with("key2", "val2")

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_watch_loop_callback_exception_does_not_crash_loop(self) -> None:
        """A failing callback does not crash the watch loop."""
        entry1 = FakeKVEntry(key="workers.w1.key1", value=b"val1")
        entry2 = FakeKVEntry(key="workers.w1.key2", value=b"val2")
        fake_watcher = FakeKeyWatcher(entries=[entry1, entry2])
        mock_kv = MagicMock()
        mock_kv.watch = AsyncMock(return_value=fake_watcher)
        mock_nc = MagicMock()
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(return_value=mock_kv)
        mock_nc.jetstream = MagicMock(return_value=mock_js)

        callback = AsyncMock(side_effect=[Exception("callback error"), None])
        watcher = ConfigWatcher(worker_id="w1", on_config_change=callback)
        await watcher.start(nc=mock_nc)

        await asyncio.sleep(0.1)

        # Both entries should have been processed despite the first callback failing
        assert callback.await_count == 2

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_stop_cleans_up_resources(self) -> None:
        """stop() cancels the watch task and unsubscribes the watcher."""
        fake_watcher = FakeKeyWatcher(entries=[])
        mock_kv = MagicMock()
        mock_kv.watch = AsyncMock(return_value=fake_watcher)
        mock_nc = MagicMock()
        mock_nc.close = AsyncMock()
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(return_value=mock_kv)
        mock_nc.jetstream = MagicMock(return_value=mock_js)

        watcher = ConfigWatcher(worker_id="w1", on_config_change=AsyncMock())
        await watcher.start(nc=mock_nc)

        await watcher.stop()

        assert watcher._watch_task is None
        assert watcher._watcher is None
        assert watcher._nc is None
        assert fake_watcher._stop_called

    @pytest.mark.asyncio
    async def test_stop_is_safe_to_call_multiple_times(self) -> None:
        """stop() can be called multiple times without error."""
        fake_watcher = FakeKeyWatcher(entries=[])
        mock_kv = MagicMock()
        mock_kv.watch = AsyncMock(return_value=fake_watcher)
        mock_nc = MagicMock()
        mock_nc.close = AsyncMock()
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(return_value=mock_kv)
        mock_nc.jetstream = MagicMock(return_value=mock_js)

        watcher = ConfigWatcher(worker_id="w1", on_config_change=AsyncMock())
        await watcher.start(nc=mock_nc)

        await watcher.stop()
        await watcher.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_watch_loop_handles_empty_value(self) -> None:
        """Watch loop handles entries with None value (empty config)."""
        entry = FakeKVEntry(
            key="workers.w1.key1",
            value=None,
        )
        fake_watcher = FakeKeyWatcher(entries=[entry])
        mock_kv = MagicMock()
        mock_kv.watch = AsyncMock(return_value=fake_watcher)
        mock_nc = MagicMock()
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(return_value=mock_kv)
        mock_nc.jetstream = MagicMock(return_value=mock_js)

        callback = AsyncMock()
        watcher = ConfigWatcher(worker_id="w1", on_config_change=callback)
        await watcher.start(nc=mock_nc)

        await asyncio.sleep(0.1)

        callback.assert_awaited_once_with("key1", "")

        await watcher.stop()
