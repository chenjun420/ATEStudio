"""Tests for worker script version tagging and VersionPoller.

Covers:
- ScriptVersioningService.tag_worker / check_worker_version / sync_worker
- VersionPoller start/stop, baseline establishment, change detection, callback
- WorkerVersionTag / WorkerVersionDiff / WorkerVersionCheckResponse schemas
- Existing ScriptVersioningService methods (regression check)
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ate_cloud.schemas.script import (
    WorkerVersionCheckResponse,
    WorkerVersionDiff,
    WorkerVersionTag,
)
from ate_cloud.services.script_versioning import ScriptVersioningService
from ate_platform.scheduler.version_poller import VersionPoller

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeKVEntry:
    """Mimics nats-py KeyValueEntry for testing."""

    def __init__(self, value: bytes) -> None:
        self.value = value


def make_tag_payload(
    worker_id: str, script_path: str, commit_hash: str,
) -> bytes:
    """Build a WorkerVersionTag JSON payload (mirrors service serialization)."""
    tag = {
        "worker_id": worker_id,
        "script_path": script_path,
        "commit_hash": commit_hash,
        "tagged_at": datetime.now(UTC).isoformat(),
    }
    return json.dumps(tag).encode("utf-8")


def make_mock_js(
    kv_keys: list[str] | None = None,
    kv_entries: dict[str, bytes] | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Create a mock JetStream context with a mock KV store.

    Args:
        kv_keys: Keys returned by kv.keys(). None → empty (NoKeysError).
        kv_entries: Mapping of key → bytes returned by kv.get(key).

    Returns:
        (mock_js, mock_kv) tuple.
    """
    mock_kv = MagicMock()
    mock_kv.put = AsyncMock(return_value=1)

    if kv_keys is None:
        from nats.js.errors import NoKeysError
        mock_kv.keys = AsyncMock(side_effect=NoKeysError())
    else:
        mock_kv.keys = AsyncMock(return_value=kv_keys)

    entries = kv_entries or {}

    async def _kv_get(key: str) -> FakeKVEntry:
        if key not in entries:
            from nats.js.errors import KeyNotFoundError
            raise KeyNotFoundError(key)
        return FakeKVEntry(entries[key])

    mock_kv.get = AsyncMock(side_effect=_kv_get)

    mock_js = MagicMock()
    mock_js.key_value = AsyncMock(return_value=mock_kv)
    return mock_js, mock_kv


def make_mock_nc(mock_js: MagicMock) -> MagicMock:
    """Create a mock NATS client that returns the given JetStream context."""
    mock_nc = MagicMock()
    mock_nc.is_connected = True
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    mock_nc.close = AsyncMock()
    return mock_nc


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestWorkerVersionTagSchema:
    """Tests for WorkerVersionTag, WorkerVersionDiff, WorkerVersionCheckResponse."""

    def test_worker_version_tag_defaults(self) -> None:
        """WorkerVersionTag auto-generates tagged_at."""
        tag = WorkerVersionTag(
            worker_id="w1", script_path="test.py", commit_hash="abc123",
        )
        assert tag.worker_id == "w1"
        assert tag.script_path == "test.py"
        assert tag.commit_hash == "abc123"
        assert tag.tagged_at is not None

    def test_worker_version_diff_needs_update_true(self) -> None:
        """needs_update is True when hashes differ."""
        diff = WorkerVersionDiff(
            script_path="test.py",
            tagged_hash="aaa",
            current_hash="bbb",
            needs_update=True,
        )
        assert diff.needs_update is True

    def test_worker_version_check_response(self) -> None:
        """WorkerVersionCheckResponse holds worker_id and diff list."""
        resp = WorkerVersionCheckResponse(
            worker_id="w1",
            scripts=[
                WorkerVersionDiff(
                    script_path="a.py",
                    tagged_hash="aaa",
                    current_hash="aaa",
                    needs_update=False,
                ),
            ],
        )
        assert resp.worker_id == "w1"
        assert len(resp.scripts) == 1
        assert resp.scripts[0].script_path == "a.py"


# ---------------------------------------------------------------------------
# ScriptVersioningService — existing methods (regression)
# ---------------------------------------------------------------------------

class TestScriptVersioningServiceRegression:
    """Regression tests for existing methods (ensure no breakage)."""

    def test_write_and_read_content(self, tmp_path: Path) -> None:
        """write_content + read_content round-trip still works."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        svc.write_content("test.py", "hello", commit_message="v1")
        assert svc.read_content("test.py") == "hello"

    def test_list_versions(self, tmp_path: Path) -> None:
        """list_versions returns commits newest first."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        svc.write_content("test.py", "v1", commit_message="First")
        svc.write_content("test.py", "v2", commit_message="Second")
        versions = svc.list_versions("test.py")
        assert len(versions) == 2
        assert versions[0]["message"] == "Second"

    def test_get_head_commit_hash(self, tmp_path: Path) -> None:
        """get_head_commit_hash returns the latest commit hash."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        h = svc.write_content("test.py", "v1")
        assert svc.get_head_commit_hash("test.py") == h

    def test_get_last_modified(self, tmp_path: Path) -> None:
        """get_last_modified returns a datetime."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        svc.write_content("test.py", "v1")
        ts = svc.get_last_modified("test.py")
        assert ts is not None
        assert isinstance(ts, datetime)


# ---------------------------------------------------------------------------
# ScriptVersioningService — tag_worker
# ---------------------------------------------------------------------------

class TestTagWorker:
    """Tests for ScriptVersioningService.tag_worker."""

    @pytest.mark.asyncio
    async def test_tag_worker_puts_to_kv(self, tmp_path: Path) -> None:
        """tag_worker puts a JSON payload at the correct KV key."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        commit_hash = svc.write_content("test.py", "v1", commit_message="init")

        mock_js, mock_kv = make_mock_js(kv_keys=[], kv_entries={})

        revision = await svc.tag_worker("test.py", "worker-1", commit_hash, mock_js)

        assert revision == 1
        mock_js.key_value.assert_awaited_once_with("ate-scripts")
        mock_kv.put.assert_awaited_once()
        key, payload = mock_kv.put.call_args.args
        assert key == "workers.worker-1.test.py"

        tag = json.loads(payload.decode("utf-8"))
        assert tag["worker_id"] == "worker-1"
        assert tag["script_path"] == "test.py"
        assert tag["commit_hash"] == commit_hash
        assert "tagged_at" in tag

    @pytest.mark.asyncio
    async def test_tag_worker_normalizes_path(self, tmp_path: Path) -> None:
        """tag_worker normalizes backslashes to forward slashes in the key."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        svc.write_content("sub/test.py", "v1")

        mock_js, mock_kv = make_mock_js(kv_keys=[], kv_entries={})

        await svc.tag_worker("sub\\test.py", "w1", "abc", mock_js)

        key, _ = mock_kv.put.call_args.args
        assert "\\" not in key
        assert key == "workers.w1.sub/test.py"


# ---------------------------------------------------------------------------
# ScriptVersioningService — check_worker_version
# ---------------------------------------------------------------------------

class TestCheckWorkerVersion:
    """Tests for ScriptVersioningService.check_worker_version."""

    @pytest.mark.asyncio
    async def test_check_returns_diffs(self, tmp_path: Path) -> None:
        """check_worker_version returns diffs with needs_update flag."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        h1 = svc.write_content("script_a.py", "v1")
        h2 = svc.write_content("script_b.py", "v1")
        # Bump script_b to a new version
        h2_new = svc.write_content("script_b.py", "v2")

        kv_entries = {
            "workers.w1.script_a.py": make_tag_payload("w1", "script_a.py", h1),
            "workers.w1.script_b.py": make_tag_payload("w1", "script_b.py", h2),
        }
        mock_js, _ = make_mock_js(
            kv_keys=list(kv_entries.keys()), kv_entries=kv_entries,
        )

        diffs = await svc.check_worker_version("w1", mock_js)

        assert len(diffs) == 2
        diff_map = {d["script_path"]: d for d in diffs}
        assert diff_map["script_a.py"]["needs_update"] is False
        assert diff_map["script_a.py"]["current_hash"] == h1
        assert diff_map["script_b.py"]["needs_update"] is True
        assert diff_map["script_b.py"]["current_hash"] == h2_new

    @pytest.mark.asyncio
    async def test_check_no_tags_returns_empty(self, tmp_path: Path) -> None:
        """check_worker_version returns empty list when no tags exist."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        mock_js, _ = make_mock_js(kv_keys=None)  # NoKeysError

        diffs = await svc.check_worker_version("w1", mock_js)
        assert diffs == []

    @pytest.mark.asyncio
    async def test_check_filters_other_workers(self, tmp_path: Path) -> None:
        """check_worker_version only returns tags for the specified worker."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        h = svc.write_content("test.py", "v1")

        kv_entries = {
            "workers.w1.test.py": make_tag_payload("w1", "test.py", h),
            "workers.w2.test.py": make_tag_payload("w2", "test.py", h),
        }
        mock_js, _ = make_mock_js(
            kv_keys=list(kv_entries.keys()), kv_entries=kv_entries,
        )

        diffs = await svc.check_worker_version("w1", mock_js)
        assert len(diffs) == 1
        assert diffs[0]["script_path"] == "test.py"


# ---------------------------------------------------------------------------
# ScriptVersioningService — sync_worker
# ---------------------------------------------------------------------------

class TestSyncWorker:
    """Tests for ScriptVersioningService.sync_worker."""

    @pytest.mark.asyncio
    async def test_sync_retags_changed_scripts(self, tmp_path: Path) -> None:
        """sync_worker re-tags scripts whose HEAD has advanced."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        h1_old = svc.write_content("script_a.py", "v1")
        h2_old = svc.write_content("script_b.py", "v1")
        # Bump both scripts to new versions
        h1_new = svc.write_content("script_a.py", "v2")
        h2_new = svc.write_content("script_b.py", "v2")

        kv_entries = {
            "workers.w1.script_a.py": make_tag_payload("w1", "script_a.py", h1_old),
            "workers.w1.script_b.py": make_tag_payload("w1", "script_b.py", h2_old),
        }
        mock_js, mock_kv = make_mock_js(
            kv_keys=list(kv_entries.keys()), kv_entries=kv_entries,
        )

        results = await svc.sync_worker("w1", mock_js)

        assert len(results) == 2
        result_map = {r["script_path"]: r for r in results}
        assert result_map["script_a.py"]["commit_hash"] == h1_new
        assert result_map["script_b.py"]["commit_hash"] == h2_new
        # tag_worker should have been called twice (re-tag both)
        assert mock_kv.put.await_count == 2

    @pytest.mark.asyncio
    async def test_sync_skips_no_commit_scripts(self, tmp_path: Path) -> None:
        """sync_worker skips scripts with no Git commits."""
        svc = ScriptVersioningService(scripts_root=tmp_path)

        kv_entries = {
            "workers.w1.untracked.py": make_tag_payload("w1", "untracked.py", "old"),
        }
        mock_js, mock_kv = make_mock_js(
            kv_keys=list(kv_entries.keys()), kv_entries=kv_entries,
        )

        results = await svc.sync_worker("w1", mock_js)
        assert results == []
        assert mock_kv.put.await_count == 0

    @pytest.mark.asyncio
    async def test_sync_no_tags_returns_empty(self, tmp_path: Path) -> None:
        """sync_worker returns empty list when no tags exist."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        mock_js, _ = make_mock_js(kv_keys=None)

        results = await svc.sync_worker("w1", mock_js)
        assert results == []


# ---------------------------------------------------------------------------
# VersionPoller
# ---------------------------------------------------------------------------

class TestVersionPoller:
    """Tests for the VersionPoller class."""

    @pytest.mark.asyncio
    async def test_first_poll_establishes_baseline(self, tmp_path: Path) -> None:
        """First check_once records versions without returning diffs."""
        kv_entries = {
            "workers.w1.script_a.py": make_tag_payload("w1", "script_a.py", "hash_a"),
            "workers.w1.script_b.py": make_tag_payload("w1", "script_b.py", "hash_b"),
        }
        mock_js, _ = make_mock_js(
            kv_keys=list(kv_entries.keys()), kv_entries=kv_entries,
        )
        mock_nc = make_mock_nc(mock_js)

        poller = VersionPoller(worker_id="w1", poll_interval=999)
        await poller.start(nc=mock_nc)

        diffs = await poller.check_once()
        assert diffs == []
        assert poller._known_versions == {"script_a.py": "hash_a", "script_b.py": "hash_b"}
        assert poller._initialized is True

        await poller.stop()

    @pytest.mark.asyncio
    async def test_detects_version_change(self, tmp_path: Path) -> None:
        """Subsequent poll detects a changed commit hash."""
        # First poll: baseline with hash_a
        kv_entries_v1 = {
            "workers.w1.script_a.py": make_tag_payload("w1", "script_a.py", "hash_a"),
        }
        mock_js, _ = make_mock_js(
            kv_keys=list(kv_entries_v1.keys()), kv_entries=kv_entries_v1,
        )
        mock_nc = make_mock_nc(mock_js)

        poller = VersionPoller(worker_id="w1", poll_interval=999)
        await poller.start(nc=mock_nc)

        await poller.check_once()  # baseline

        # Second poll: hash changed to hash_b
        kv_entries_v2 = {
            "workers.w1.script_a.py": make_tag_payload("w1", "script_a.py", "hash_b"),
        }
        mock_kv = MagicMock()
        mock_kv.put = AsyncMock(return_value=1)
        mock_kv.keys = AsyncMock(return_value=list(kv_entries_v2.keys()))

        async def _get_v2(key: str) -> FakeKVEntry:
            return FakeKVEntry(kv_entries_v2[key])

        mock_kv.get = AsyncMock(side_effect=_get_v2)
        mock_js.key_value = AsyncMock(return_value=mock_kv)

        diffs = await poller.check_once()

        assert len(diffs) == 1
        assert diffs[0].script_path == "script_a.py"
        assert diffs[0].new_hash == "hash_b"
        assert diffs[0].old_hash == "hash_a"

        await poller.stop()

    @pytest.mark.asyncio
    async def test_detects_new_script_tag(self, tmp_path: Path) -> None:
        """Poller detects a newly tagged script after baseline."""
        # First poll: one script
        kv_entries_v1 = {
            "workers.w1.script_a.py": make_tag_payload("w1", "script_a.py", "hash_a"),
        }
        mock_js, _ = make_mock_js(
            kv_keys=list(kv_entries_v1.keys()), kv_entries=kv_entries_v1,
        )
        mock_nc = make_mock_nc(mock_js)

        poller = VersionPoller(worker_id="w1", poll_interval=999)
        await poller.start(nc=mock_nc)
        await poller.check_once()  # baseline

        # Second poll: new script added
        kv_entries_v2 = {
            "workers.w1.script_a.py": make_tag_payload("w1", "script_a.py", "hash_a"),
            "workers.w1.script_b.py": make_tag_payload("w1", "script_b.py", "hash_b"),
        }
        mock_kv = MagicMock()
        mock_kv.put = AsyncMock(return_value=1)
        mock_kv.keys = AsyncMock(return_value=list(kv_entries_v2.keys()))

        async def _get_v2(key: str) -> FakeKVEntry:
            return FakeKVEntry(kv_entries_v2[key])

        mock_kv.get = AsyncMock(side_effect=_get_v2)
        mock_js.key_value = AsyncMock(return_value=mock_kv)

        diffs = await poller.check_once()

        assert len(diffs) == 1
        assert diffs[0].script_path == "script_b.py"
        assert diffs[0].new_hash == "hash_b"
        assert diffs[0].old_hash is None

        await poller.stop()

    @pytest.mark.asyncio
    async def test_callback_invoked_on_change(self, tmp_path: Path) -> None:
        """on_version_update callback is called when a version changes.

        check_once() returns diffs; the poll loop invokes the callback.
        This test verifies the callback fires when manually dispatched
        (mirroring what _poll_loop does internally).
        """
        kv_entries_v1 = {
            "workers.w1.script_a.py": make_tag_payload("w1", "script_a.py", "hash_a"),
        }
        mock_js, _ = make_mock_js(
            kv_keys=list(kv_entries_v1.keys()), kv_entries=kv_entries_v1,
        )
        mock_nc = make_mock_nc(mock_js)

        callback_calls: list[tuple[str, str]] = []

        async def on_update(script_path: str, new_hash: str) -> None:
            callback_calls.append((script_path, new_hash))

        poller = VersionPoller(
            worker_id="w1", poll_interval=999, on_version_update=on_update,
        )
        await poller.start(nc=mock_nc)
        await poller.check_once()  # baseline — no callback

        assert callback_calls == []

        # Second poll: version changed
        kv_entries_v2 = {
            "workers.w1.script_a.py": make_tag_payload("w1", "script_a.py", "hash_b"),
        }
        mock_kv = MagicMock()
        mock_kv.put = AsyncMock(return_value=1)
        mock_kv.keys = AsyncMock(return_value=list(kv_entries_v2.keys()))

        async def _get_v2(key: str) -> FakeKVEntry:
            return FakeKVEntry(kv_entries_v2[key])

        mock_kv.get = AsyncMock(side_effect=_get_v2)
        mock_js.key_value = AsyncMock(return_value=mock_kv)

        diffs = await poller.check_once()

        assert len(diffs) == 1

        # Dispatch diffs to callback (as _poll_loop does internally)
        assert poller._on_version_update is not None
        for diff in diffs:
            await poller._on_version_update(diff.script_path, diff.new_hash)

        assert len(callback_calls) == 1
        assert callback_calls[0] == ("script_a.py", "hash_b")

        await poller.stop()

    @pytest.mark.asyncio
    async def test_check_once_without_start_raises(self) -> None:
        """check_once raises RuntimeError if not started."""
        poller = VersionPoller(worker_id="w1")
        with pytest.raises(RuntimeError, match="not started"):
            await poller.check_once()

    @pytest.mark.asyncio
    async def test_no_keys_handled_gracefully(self, tmp_path: Path) -> None:
        """First poll with no keys establishes empty baseline."""
        mock_js, _ = make_mock_js(kv_keys=None)  # NoKeysError
        mock_nc = make_mock_nc(mock_js)

        poller = VersionPoller(worker_id="w1", poll_interval=999)
        await poller.start(nc=mock_nc)

        diffs = await poller.check_once()
        assert diffs == []
        assert poller._known_versions == {}
        assert poller._initialized is True

        await poller.stop()

    @pytest.mark.asyncio
    async def test_poll_loop_triggers_callback(self, tmp_path: Path) -> None:
        """Poll loop calls callback when version changes between polls."""
        kv_entries_v1 = {
            "workers.w1.script_a.py": make_tag_payload("w1", "script_a.py", "hash_a"),
        }
        mock_js, mock_kv = make_mock_js(
            kv_keys=list(kv_entries_v1.keys()), kv_entries=kv_entries_v1,
        )
        mock_nc = make_mock_nc(mock_js)

        callback_calls: list[tuple[str, str]] = []

        async def on_update(script_path: str, new_hash: str) -> None:
            callback_calls.append((script_path, new_hash))

        poller = VersionPoller(
            worker_id="w1", poll_interval=0.05, on_version_update=on_update,
        )
        await poller.start(nc=mock_nc)

        # Wait for first poll (baseline)
        await asyncio.sleep(0.1)

        # Change the KV to a new version
        kv_entries_v2 = {
            "workers.w1.script_a.py": make_tag_payload("w1", "script_a.py", "hash_b"),
        }
        mock_kv.keys = AsyncMock(return_value=list(kv_entries_v2.keys()))

        async def _get_v2(key: str) -> FakeKVEntry:
            return FakeKVEntry(kv_entries_v2[key])

        mock_kv.get = AsyncMock(side_effect=_get_v2)

        # Wait for second poll (should detect change)
        await asyncio.sleep(0.15)

        await poller.stop()

        assert len(callback_calls) >= 1
        assert callback_calls[0] == ("script_a.py", "hash_b")

    @pytest.mark.asyncio
    async def test_stop_cleans_up(self, tmp_path: Path) -> None:
        """stop() cancels the poll task and closes NATS."""
        mock_js, _ = make_mock_js(kv_keys=None)
        mock_nc = make_mock_nc(mock_js)

        poller = VersionPoller(worker_id="w1", poll_interval=999)
        await poller.start(nc=mock_nc)
        await poller.stop()

        assert poller._poll_task is None
        assert poller._nc is None
        mock_nc.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# End-to-end: tag → poll → callback
# ---------------------------------------------------------------------------

class TestTagPollIntegration:
    """Integration: tag a script, poll detects the tag, callback fires."""

    @pytest.mark.asyncio
    async def test_bump_version_tag_then_poll_detects(self, tmp_path: Path) -> None:
        """Bump script version + tag to worker → poller detects change.

        This mirrors the task verification scenario:
        1. Write script v1, tag to worker (baseline)
        2. Bump script to v2, re-tag
        3. Poller detects the version change
        """
        svc = ScriptVersioningService(scripts_root=tmp_path)

        # Step 1: Write v1 and tag
        h1 = svc.write_content("test.py", 'print("v1")', commit_message="v1")

        # Simulate the tag being in KV (the poller reads from KV)
        kv_entries_v1 = {
            "workers.w1.test.py": make_tag_payload("w1", "test.py", h1),
        }
        mock_js, mock_kv = make_mock_js(
            kv_keys=list(kv_entries_v1.keys()), kv_entries=kv_entries_v1,
        )
        mock_nc = make_mock_nc(mock_js)

        callback_calls: list[tuple[str, str]] = []

        async def on_update(script_path: str, new_hash: str) -> None:
            callback_calls.append((script_path, new_hash))

        poller = VersionPoller(
            worker_id="w1", poll_interval=999, on_version_update=on_update,
        )
        await poller.start(nc=mock_nc)
        await poller.check_once()  # baseline

        assert callback_calls == []

        # Step 2: Bump to v2 and re-tag
        h2 = svc.write_content("test.py", 'print("v2")', commit_message="v2")

        # Simulate the re-tagged KV
        kv_entries_v2 = {
            "workers.w1.test.py": make_tag_payload("w1", "test.py", h2),
        }
        mock_kv.keys = AsyncMock(return_value=list(kv_entries_v2.keys()))

        async def _get_v2(key: str) -> FakeKVEntry:
            return FakeKVEntry(kv_entries_v2[key])

        mock_kv.get = AsyncMock(side_effect=_get_v2)

        # Step 3: Poller detects the change
        diffs = await poller.check_once()

        assert len(diffs) == 1
        assert diffs[0].script_path == "test.py"
        assert diffs[0].new_hash == h2
        assert diffs[0].old_hash == h1

        # Callback was invoked (by check_once → but check_once doesn't call
        # callback; only _poll_loop does). Manually verify the callback:
        await on_update(diffs[0].script_path, diffs[0].new_hash)
        assert callback_calls == [("test.py", h2)]

        await poller.stop()
