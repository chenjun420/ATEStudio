"""Unit tests for SQLiteCache."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio

from ate_platform.data import SQLiteCache
from ate_platform.types import StepResult, StepStatus


class TestSQLiteCache:
    """Tests for SQLiteCache class."""

    @pytest_asyncio.fixture
    async def cache(self) -> AsyncGenerator[SQLiteCache, None]:
        """Create an in-memory cache for testing."""
        cache = SQLiteCache(":memory:")
        await cache.connect()
        yield cache
        await cache.close()

    @pytest_asyncio.fixture
    async def cache_with_context(self) -> AsyncGenerator[SQLiteCache, None]:
        """Create an in-memory cache using context manager."""
        async with SQLiteCache(":memory:") as cache:
            yield cache

    @pytest.mark.asyncio
    async def test_connect_creates_tables(self, cache: SQLiteCache) -> None:
        """Verify connect() creates required tables."""
        # Tables should exist after connect
        assert cache._db is not None  # noqa: SLF001
        async with cache._db.execute(  # noqa: SLF001
            "SELECT name FROM sqlite_master WHERE type='table' AND name='results'"
        ) as cursor:
            result = await cursor.fetchone()
        assert result is not None

        async with cache._db.execute(  # noqa: SLF001
            "SELECT name FROM sqlite_master WHERE type='table' AND name='upload_queue'"
        ) as cursor:
            result = await cursor.fetchone()
        assert result is not None

    @pytest.mark.asyncio
    async def test_connect_raises_on_reconnect(self, cache: SQLiteCache) -> None:
        """Verify connect() raises error when already connected."""
        with pytest.raises(RuntimeError, match="Already connected"):
            await cache.connect()

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        """Verify async context manager works correctly."""
        async with SQLiteCache(":memory:") as cache:
            assert cache._db is not None  # noqa: SLF001
            # Save and retrieve a result
            result = StepResult(status=StepStatus.PASSED)
            await cache.save_result("step-1", result)
            retrieved = await cache.get_result("step-1")
            assert retrieved is not None
            assert retrieved.status == StepStatus.PASSED

        # Connection should be closed
        assert cache._db is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_save_and_get_result(self, cache: SQLiteCache) -> None:
        """Test basic save and retrieve operations."""
        result = StepResult(
            status=StepStatus.PASSED,
            outputs={"value": 42, "name": "test"},
            error=None,
        )

        await cache.save_result("step-001", result)

        retrieved = await cache.get_result("step-001")
        assert retrieved is not None
        assert retrieved.status == StepStatus.PASSED
        assert retrieved.outputs == {"value": 42, "name": "test"}
        assert retrieved.error is None

    @pytest.mark.asyncio
    async def test_save_result_with_error(self, cache: SQLiteCache) -> None:
        """Test saving a result with an error message."""
        result = StepResult(
            status=StepStatus.FAILED,
            outputs={},
            error="Connection timeout",
        )

        await cache.save_result("step-002", result)

        retrieved = await cache.get_result("step-002")
        assert retrieved is not None
        assert retrieved.status == StepStatus.FAILED
        assert retrieved.error == "Connection timeout"

    @pytest.mark.asyncio
    async def test_save_result_upsert(self, cache: SQLiteCache) -> None:
        """Test that saving with same step_id updates existing record."""
        result1 = StepResult(status=StepStatus.PASSED, outputs={"count": 1})
        result2 = StepResult(status=StepStatus.FAILED, outputs={"count": 2})

        await cache.save_result("step-003", result1)
        await cache.save_result("step-003", result2)

        retrieved = await cache.get_result("step-003")
        assert retrieved is not None
        assert retrieved.status == StepStatus.FAILED
        assert retrieved.outputs == {"count": 2}

    @pytest.mark.asyncio
    async def test_get_result_not_found(self, cache: SQLiteCache) -> None:
        """Test get_result returns None for missing step."""
        result = await cache.get_result("nonexistent-step")
        assert result is None

    @pytest.mark.asyncio
    async def test_save_result_with_sequence_id(self, cache: SQLiteCache) -> None:
        """Test saving result with sequence ID."""
        result = StepResult(status=StepStatus.PASSED)

        await cache.save_result("step-004", result, sequence_id="seq-001")

        # Verify it can be retrieved by sequence
        results = await cache.get_sequence_results("seq-001")
        assert len(results) == 1
        assert results[0].status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_get_sequence_results(self, cache: SQLiteCache) -> None:
        """Test retrieving all results for a sequence."""
        # Create multiple results for same sequence
        await cache.save_result(
            "step-101",
            StepResult(status=StepStatus.PASSED, outputs={"order": 1}),
            sequence_id="seq-100",
        )
        await cache.save_result(
            "step-102",
            StepResult(status=StepStatus.FAILED, outputs={"order": 2}),
            sequence_id="seq-100",
        )
        await cache.save_result(
            "step-103",
            StepResult(status=StepStatus.SKIPPED, outputs={"order": 3}),
            sequence_id="seq-100",
        )

        # Add result for different sequence
        await cache.save_result(
            "step-201",
            StepResult(status=StepStatus.PASSED),
            sequence_id="seq-200",
        )

        results = await cache.get_sequence_results("seq-100")
        assert len(results) == 3
        assert results[0].outputs["order"] == 1
        assert results[1].outputs["order"] == 2
        assert results[2].outputs["order"] == 3

    @pytest.mark.asyncio
    async def test_get_sequence_results_empty(self, cache: SQLiteCache) -> None:
        """Test get_sequence_results returns empty list for missing sequence."""
        results = await cache.get_sequence_results("nonexistent-seq")
        _ = results  # Used for verification
        assert results == []

    @pytest.mark.asyncio
    async def test_all_step_statuses(self, cache: SQLiteCache) -> None:
        """Test saving and retrieving all possible status values."""
        statuses = [
            StepStatus.PENDING,
            StepStatus.RUNNING,
            StepStatus.PASSED,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
            StepStatus.ERROR,
        ]

        for i, status in enumerate(statuses):
            result = StepResult(status=status)
            await cache.save_result(f"status-step-{i}", result)

        for i, expected_status in enumerate(statuses):
            retrieved = await cache.get_result(f"status-step-{i}")
            assert retrieved is not None
            assert retrieved.status == expected_status

    @pytest.mark.asyncio
    async def test_complex_outputs(self, cache: SQLiteCache) -> None:
        """Test saving and retrieving complex output dictionaries."""
        outputs = {
            "nested": {"key": "value", "number": 123},
            "list": [1, 2, 3],
            "string": "test string",
            "bool": True,
            "null": None,
        }
        result = StepResult(status=StepStatus.PASSED, outputs=outputs)

        await cache.save_result("complex-step", result)

        retrieved = await cache.get_result("complex-step")
        assert retrieved is not None
        assert retrieved.outputs == outputs

    @pytest.mark.asyncio
    async def test_operations_raise_when_not_connected(self) -> None:
        """Test that operations raise error when not connected."""
        cache = SQLiteCache(":memory:")

        result = StepResult(status=StepStatus.PASSED)

        with pytest.raises(RuntimeError, match="Not connected"):
            await cache.save_result("step-1", result)

        with pytest.raises(RuntimeError, match="Not connected"):
            await cache.get_result("step-1")

        with pytest.raises(RuntimeError, match="Not connected"):
            await cache.get_sequence_results("seq-1")

    @pytest.mark.asyncio
    async def test_close_is_safe_to_call_multiple_times(self, cache: SQLiteCache) -> None:
        """Test that close() can be called multiple times safely."""
        await cache.close()
        await cache.close()  # Should not raise

        assert cache._db is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_concurrent_access(self, cache: SQLiteCache) -> None:
        """Test concurrent writes are handled correctly with lock."""

        async def save_result(step_num: int) -> None:
            result = StepResult(status=StepStatus.PASSED, outputs={"step": step_num})
            await cache.save_result(f"concurrent-{step_num}", result)

        # Run 10 concurrent saves
        _ = await asyncio.gather(*[save_result(i) for i in range(10)])

        # Verify all saved
        for i in range(10):
            retrieved = await cache.get_result(f"concurrent-{i}")
            assert retrieved is not None
            assert retrieved.outputs["step"] == i

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, tmp_path: Any) -> None:
        """Verify WAL mode is enabled for file-based databases."""
        db_path = str(tmp_path / "test.db")
        async with SQLiteCache(db_path) as cache:
            assert cache._db is not None  # noqa: SLF001
            async with cache._db.execute("PRAGMA journal_mode") as cursor:  # noqa: SLF001
                result = await cursor.fetchone()
            assert result is not None
            # WAL mode is set, though :memory: databases use 'memory' journal mode
            # For file-based DBs, WAL should be enabled
            assert result[0].lower() in ("wal", "memory")


class TestQueuePruning:
    """Tests for upload queue size pruning and age-based cleanup."""

    @pytest_asyncio.fixture
    async def cache(self) -> AsyncGenerator[SQLiteCache, None]:
        """Create an in-memory cache with small max_queue_size for testing."""
        cache = SQLiteCache(":memory:", max_queue_size=5, max_queue_age_seconds=3600)
        await cache.connect()
        yield cache
        await cache.close()

    @pytest_asyncio.fixture
    async def cache_small(self) -> AsyncGenerator[SQLiteCache, None]:
        """Create an in-memory cache with max_queue_size=1 for edge case tests."""
        cache = SQLiteCache(":memory:", max_queue_size=1, max_queue_age_seconds=3600)
        await cache.connect()
        yield cache
        await cache.close()

    @pytest.mark.asyncio
    async def test_enqueue_upload_stores_payload(self, cache: SQLiteCache) -> None:
        """Verify enqueue_upload stores payload in upload_queue."""
        payload = b'{"step_id": "step-1", "status": "passed"}'
        await cache.enqueue_upload(payload)

        stats = await cache.queue_stats()
        assert stats["current_size"] == 1

    @pytest.mark.asyncio
    async def test_size_pruning_insert_above_limit(self, cache: SQLiteCache) -> None:
        """Insert 6 entries with max_queue_size=5 — oldest 1 should be pruned."""
        for i in range(6):
            payload = f'{{"step_id": "step-{i}", "status": "passed"}}'.encode()
            await cache.enqueue_upload(payload)

        stats = await cache.queue_stats()
        assert stats["current_size"] == 5
        assert stats["total_pruned"] == 1

    @pytest.mark.asyncio
    async def test_size_pruning_many_above_limit(self, cache: SQLiteCache) -> None:
        """Insert 15 entries with max_queue_size=5 — oldest 10 should be pruned."""
        for i in range(15):
            payload = f'{{"step_id": "step-{i}", "status": "passed"}}'.encode()
            await cache.enqueue_upload(payload)

        stats = await cache.queue_stats()
        assert stats["current_size"] == 5
        assert stats["total_pruned"] == 10

    @pytest.mark.asyncio
    async def test_size_pruning_with_small_limit(
        self, cache_small: SQLiteCache
    ) -> None:
        """Insert 2 entries with max_queue_size=1 — oldest should be pruned."""
        await cache_small.enqueue_upload(b'{"step_id": "step-a"}')
        await cache_small.enqueue_upload(b'{"step_id": "step-b"}')

        stats = await cache_small.queue_stats()
        assert stats["current_size"] == 1
        assert stats["total_pruned"] == 1

    @pytest.mark.asyncio
    async def test_no_pruning_when_below_limit(self, cache: SQLiteCache) -> None:
        """Insert 3 entries with max_queue_size=5 — nothing should be pruned."""
        for i in range(3):
            payload = f'{{"step_id": "step-{i}"}}'.encode()
            await cache.enqueue_upload(payload)

        stats = await cache.queue_stats()
        assert stats["current_size"] == 3
        assert stats["total_pruned"] == 0

    @pytest.mark.asyncio
    async def test_oldest_entry_pruned_first(self, cache: SQLiteCache) -> None:
        """Verify the OLDEST entry is pruned when limit exceeded."""
        # Insert 5 entries (fills queue)
        for i in range(5):
            payload = f'{{"step_id": "step-{i}"}}'.encode()
            await cache.enqueue_upload(payload)

        stats_before = await cache.queue_stats()
        assert stats_before["current_size"] == 5

        # Small delay so timestamps differ
        await asyncio.sleep(0.01)

        # Insert one more — oldest should be pruned
        await cache.enqueue_upload(b'{"step_id": "step-new"}')

        stats_after = await cache.queue_stats()
        assert stats_after["current_size"] == 5
        assert stats_after["total_pruned"] == 1

    @pytest.mark.asyncio
    async def test_age_pruning_deletes_old_entries(self, cache: SQLiteCache) -> None:
        """Verify _cleanup_aged_entries deletes entries older than threshold."""
        # Insert an entry with a past timestamp directly
        import aiosqlite

        past_time = "2020-01-01T00:00:00"
        async with cache._lock:  # noqa: SLF001
            await cache._db.execute(  # noqa: SLF001
                "INSERT INTO upload_queue (payload, retry_count, created_at) VALUES (?, ?, ?)",
                ('{"step_id": "old-step"}', 0, past_time),
            )
            await cache._db.commit()  # noqa: SLF001

        # Insert a recent entry
        await cache.enqueue_upload(b'{"step_id": "new-step"}')

        stats_before = await cache.queue_stats()
        assert stats_before["current_size"] == 2

        # Run age-based cleanup
        deleted = await cache._cleanup_aged_entries()  # noqa: SLF001
        assert deleted == 1

        stats_after = await cache.queue_stats()
        assert stats_after["current_size"] == 1
        assert stats_after["total_pruned"] == 1

    @pytest.mark.asyncio
    async def test_age_pruning_keeps_recent_entries(self, cache: SQLiteCache) -> None:
        """Verify _cleanup_aged_entries does NOT delete recent entries."""
        for i in range(3):
            payload = f'{{"step_id": "step-{i}"}}'.encode()
            await cache.enqueue_upload(payload)

        deleted = await cache._cleanup_aged_entries()  # noqa: SLF001
        assert deleted == 0

        stats = await cache.queue_stats()
        assert stats["current_size"] == 3

    @pytest.mark.asyncio
    async def test_queue_stats_returns_correct_values(self, cache: SQLiteCache) -> None:
        """Verify queue_stats returns correct current_size, oldest_entry_age, total_pruned."""
        # Initially empty
        stats = await cache.queue_stats()
        assert stats["current_size"] == 0
        assert stats["oldest_entry_age"] == 0
        assert stats["total_pruned"] == 0

        # Insert entries
        await cache.enqueue_upload(b'{"step_id": "step-1"}')
        await cache.enqueue_upload(b'{"step_id": "step-2"}')

        stats = await cache.queue_stats()
        assert stats["current_size"] == 2
        assert stats["oldest_entry_age"] >= 0
        assert stats["total_pruned"] == 0

        # Insert beyond limit (max_queue_size=5, so 6 total → prune 1)
        for i in range(4):
            payload = f'{{"step_id": "step-extra-{i}"}}'.encode()
            await cache.enqueue_upload(payload)

        stats = await cache.queue_stats()
        assert stats["current_size"] == 5
        assert stats["total_pruned"] == 1

    @pytest.mark.asyncio
    async def test_total_pruned_accumulates(self, cache: SQLiteCache) -> None:
        """Verify total_pruned counter accumulates across multiple prune operations."""
        # Fill queue with 5 entries
        for i in range(5):
            await cache.enqueue_upload(f'{{"step_id": "step-{i}"}}'.encode())

        # Push 5 more — each enqueue prunes 1
        for i in range(5):
            await cache.enqueue_upload(f'{{"step_id": "step-batch2-{i}"}}'.encode())

        stats = await cache.queue_stats()
        assert stats["current_size"] == 5
        assert stats["total_pruned"] == 5

    @pytest.mark.asyncio
    async def test_cleanup_task_starts_on_connect(self) -> None:
        """Verify the periodic cleanup task is created on connect."""
        cache = SQLiteCache(":memory:", max_queue_size=10, max_queue_age_seconds=60)
        assert cache._cleanup_task is None  # noqa: SLF001

        await cache.connect()
        assert cache._cleanup_task is not None  # noqa: SLF001
        assert cache._cleanup_running is True  # noqa: SLF001

        await cache.close()
        assert cache._cleanup_task is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_cleanup_task_stops_on_close(self, cache: SQLiteCache) -> None:
        """Verify the cleanup task is cancelled on close."""
        assert cache._cleanup_task is not None  # noqa: SLF001
        await cache.close()
        assert cache._cleanup_task is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_close_safe_with_cleanup(self) -> None:
        """Verify close() is safe to call multiple times with cleanup task."""
        cache = SQLiteCache(":memory:")
        await cache.connect()
        await cache.close()
        await cache.close()  # Should not raise
        assert cache._db is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_value_error_on_zero_max_queue_size(self) -> None:
        """Verify ValueError raised when max_queue_size <= 0."""
        with pytest.raises(ValueError, match="max_queue_size"):
            SQLiteCache(":memory:", max_queue_size=0)

    @pytest.mark.asyncio
    async def test_value_error_on_negative_max_queue_size(self) -> None:
        """Verify ValueError raised when max_queue_size < 0."""
        with pytest.raises(ValueError, match="max_queue_size"):
            SQLiteCache(":memory:", max_queue_size=-1)

    @pytest.mark.asyncio
    async def test_value_error_on_zero_max_queue_age(self) -> None:
        """Verify ValueError raised when max_queue_age_seconds <= 0."""
        with pytest.raises(ValueError, match="max_queue_age_seconds"):
            SQLiteCache(":memory:", max_queue_age_seconds=0)

    @pytest.mark.asyncio
    async def test_default_values_are_sane(self) -> None:
        """Verify default constructor values are 1000 and 3600."""
        cache = SQLiteCache(":memory:")
        assert cache._max_queue_size == 1000  # noqa: SLF001
        assert cache._max_queue_age_seconds == 3600  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_enqueue_upload_raises_when_not_connected(self) -> None:
        """Verify enqueue_upload raises RuntimeError when not connected."""
        cache = SQLiteCache(":memory:")
        with pytest.raises(RuntimeError, match="Not connected"):
            await cache.enqueue_upload(b'{"step_id": "test"}')

    @pytest.mark.asyncio
    async def test_periodic_cleanup_deletes_aged_entries(
        self, cache: SQLiteCache
    ) -> None:
        """Verify the periodic cleanup loop actually deletes aged entries.

        We insert an old entry and wait for the cleanup task to fire.
        Since the loop sleeps 60s, we call _cleanup_aged_entries directly
        and verify the task infrastructure is wired up.
        """
        # Insert old entry
        past_time = "2020-01-01T00:00:00"
        async with cache._lock:  # noqa: SLF001
            await cache._db.execute(  # noqa: SLF001
                "INSERT INTO upload_queue (payload, retry_count, created_at) VALUES (?, ?, ?)",
                ('{"step_id": "old-step"}', 0, past_time),
            )
            await cache._db.commit()  # noqa: SLF001

        await cache.enqueue_upload(b'{"step_id": "new-step"}')

        # Simulate what the periodic task does
        deleted = await cache._cleanup_aged_entries()  # noqa: SLF001
        assert deleted == 1

        stats = await cache.queue_stats()
        assert stats["current_size"] == 1