"""Unit tests for SQLiteCache."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

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