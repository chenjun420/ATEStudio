"""SQLite-based cache for step execution results.

This module provides async SQLite caching with WAL mode for concurrent access.
Supports storing and retrieving StepResult objects with automatic checkpointing.
Includes upload queue management with size-based pruning and TTL-based cleanup.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import aiosqlite

from ate_platform.types import StepResult, StepStatus

logger = logging.getLogger(__name__)


class SQLiteCache:
    """Async SQLite cache for step execution results.

    Features:
    - WAL mode for improved concurrent access
    - Automatic checkpointing when WAL exceeds threshold
    - Thread-safe via asyncio.Lock
    - Support for :memory: databases for testing

    Example:
        async with SQLiteCache("results.db") as cache:
            result = StepResult(status=StepStatus.PASSED, outputs={"value": 42})
            await cache.save_result("step-001", result)
            retrieved = await cache.get_result("step-001")
    """

    __slots__ = (
        "_db_path",
        "_db",
        "_lock",
        "_checkpoint_page_threshold",
        "_max_queue_size",
        "_max_queue_age_seconds",
        "_total_pruned",
        "_cleanup_task",
        "_cleanup_running",
    )

    def __init__(
        self,
        db_path: str,
        checkpoint_page_threshold: int = 1000,
        max_queue_size: int = 1000,
        max_queue_age_seconds: int = 3600,
    ) -> None:
        """Initialize the cache.

        Args:
            db_path: Path to SQLite database file, or ":memory:" for in-memory DB.
            checkpoint_page_threshold: WAL checkpoint trigger threshold (default: 1000 pages).
            max_queue_size: Maximum number of entries in upload queue before pruning (default: 1000).
            max_queue_age_seconds: Maximum age in seconds before entries are pruned (default: 3600).

        Raises:
            ValueError: If max_queue_size <= 0 or max_queue_age_seconds <= 0.
        """
        if max_queue_size <= 0:
            raise ValueError("max_queue_size must be greater than 0")
        if max_queue_age_seconds <= 0:
            raise ValueError("max_queue_age_seconds must be greater than 0")

        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._checkpoint_page_threshold = checkpoint_page_threshold
        self._max_queue_size = max_queue_size
        self._max_queue_age_seconds = max_queue_age_seconds
        self._total_pruned = 0
        self._cleanup_task: asyncio.Task[Any] | None = None
        self._cleanup_running = False

    async def connect(self) -> None:
        """Connect to the database and initialize schema.

        Raises:
            RuntimeError: If already connected.
        """
        async with self._lock:
            if self._db is not None:
                raise RuntimeError("Already connected to database")

            self._db = await aiosqlite.connect(self._db_path)

            # Enable WAL mode for better concurrent access
            await self._db.execute("PRAGMA journal_mode=WAL")

            # Create tables
            await self._create_tables()

        # Start periodic cleanup of aged upload queue entries
        self._cleanup_running = True
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup_loop())

    async def _create_tables(self) -> None:
        """Create database tables if they don't exist."""
        if self._db is None:
            raise RuntimeError("Not connected to database")

        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_id TEXT,
                step_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                outputs TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                timestamp TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_results_sequence_id ON results(sequence_id);
            CREATE INDEX IF NOT EXISTS idx_results_step_id ON results(step_id);

            CREATE TABLE IF NOT EXISTS upload_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_upload_queue_created_at ON upload_queue(created_at);
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        """Close the database connection.

        Safe to call multiple times.
        """
        # Stop cleanup task
        self._cleanup_running = False
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        async with self._lock:
            if self._db is not None:
                await self._db.close()
                self._db = None

    async def enqueue_upload(self, payload: bytes) -> None:
        """Enqueue a result payload for upload to NATS.

        Persists the payload to the upload_queue table. If the queue exceeds
        max_queue_size, prunes oldest entries to bring it back to the limit.
        Also triggers lightweight age-based cleanup.

        Args:
            payload: JSON-encoded bytes of the upload payload.

        Raises:
            RuntimeError: If not connected to database.
        """
        async with self._lock:
            if self._db is None:
                raise RuntimeError("Not connected to database")

            timestamp = datetime.now().isoformat()

            await self._db.execute(
                """
                INSERT INTO upload_queue (payload, retry_count, created_at)
                VALUES (?, ?, ?)
                """,
                (payload.decode("utf-8"), 0, timestamp),
            )
            await self._db.commit()

            # Check and enforce size limit
            await self._prune_excess_entries()

    async def _prune_excess_entries(self) -> None:
        """Prune oldest entries if upload_queue exceeds max_queue_size."""
        if self._db is None:
            return

        async with self._db.execute("SELECT COUNT(*) FROM upload_queue") as cursor:
            row = await cursor.fetchone()
        count = row[0] if row else 0

        if count > self._max_queue_size:
            excess = count - self._max_queue_size
            await self._db.execute(
                """
                DELETE FROM upload_queue
                WHERE id IN (
                    SELECT id FROM upload_queue
                    ORDER BY created_at ASC
                    LIMIT ?
                )
                """,
                (excess,),
            )
            await self._db.commit()
            self._total_pruned += excess
            logger.warning("Pruned %d upload queue entries (size limit)", excess)

    async def _cleanup_aged_entries(self) -> int:
        """Delete upload queue entries older than max_queue_age_seconds.

        Returns:
            Number of entries deleted.
        """
        async with self._lock:
            if self._db is None:
                return 0

            async with self._db.execute(
                """
                DELETE FROM upload_queue
                WHERE created_at < datetime('now', ?)
                """,
                (f"-{self._max_queue_age_seconds} seconds",),
            ) as cursor:
                deleted = cursor.rowcount

            if deleted > 0:
                await self._db.commit()
                self._total_pruned += deleted
                logger.warning("Pruned %d upload queue entries (age threshold)", deleted)

            return deleted

    async def _periodic_cleanup_loop(self) -> None:
        """Background task that runs age-based cleanup every 60 seconds."""
        while self._cleanup_running:
            try:
                await asyncio.sleep(60)
                if not self._cleanup_running:
                    break
                await self._cleanup_aged_entries()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in periodic cleanup loop")

    async def queue_stats(self) -> dict[str, int | float]:
        """Return statistics about the upload queue.

        Returns:
            dict with keys:
                current_size: Number of entries in upload_queue.
                oldest_entry_age: Age in seconds of the oldest entry (0 if empty).
                total_pruned: Total number of entries pruned since startup.
        """
        async with self._lock:
            if self._db is None:
                return {
                    "current_size": 0,
                    "oldest_entry_age": 0,
                    "total_pruned": self._total_pruned,
                }

            async with self._db.execute(
                "SELECT COUNT(*) FROM upload_queue"
            ) as cursor:
                row = await cursor.fetchone()
            current_size = row[0] if row else 0

            oldest_entry_age: float = 0.0
            if current_size > 0:
                async with self._db.execute(
                    "SELECT created_at FROM upload_queue ORDER BY created_at ASC LIMIT 1"
                ) as cursor:
                    row = await cursor.fetchone()
                if row and row[0]:
                    try:
                        oldest_dt = datetime.fromisoformat(row[0])
                        oldest_entry_age = (datetime.now() - oldest_dt).total_seconds()
                    except (ValueError, TypeError):
                        oldest_entry_age = 0

            return {
                "current_size": current_size,
                "oldest_entry_age": oldest_entry_age,
                "total_pruned": self._total_pruned,
            }

    async def _checkpoint_if_needed(self) -> None:
        """Perform WAL checkpoint if page count exceeds threshold."""
        if self._db is None:
            return

        async with self._db.execute("PRAGMA wal_checkpoint(PASSIVE)") as cursor:
            result = await cursor.fetchone()

        # result[1] = number of pages in WAL file
        if result and len(result) > 1 and result[1] >= self._checkpoint_page_threshold:
            await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await self._db.commit()

    async def save_result(
        self,
        step_id: str,
        result: StepResult,
        sequence_id: str | None = None,
    ) -> None:
        """Save a step execution result.

        Args:
            step_id: Unique identifier for the step.
            result: The StepResult to save.
            sequence_id: Optional sequence ID to group results.

        Raises:
            RuntimeError: If not connected to database.
        """
        async with self._lock:
            if self._db is None:
                raise RuntimeError("Not connected to database")

            timestamp = datetime.now().isoformat()
            outputs_json = json.dumps(result.outputs)
            status_value = result.status.value

            await self._db.execute(
                """
                INSERT OR REPLACE INTO results
                (sequence_id, step_id, status, outputs, error, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence_id,
                    step_id,
                    status_value,
                    outputs_json,
                    result.error,
                    timestamp,
                ),
            )
            await self._db.commit()

            await self._checkpoint_if_needed()

    async def get_result(self, step_id: str) -> StepResult | None:
        """Retrieve a step execution result.

        Args:
            step_id: Unique identifier for the step.

        Returns:
            StepResult if found, None otherwise.

        Raises:
            RuntimeError: If not connected to database.
        """
        async with self._lock:
            if self._db is None:
                raise RuntimeError("Not connected to database")

            async with self._db.execute(
                "SELECT status, outputs, error FROM results WHERE step_id = ?",
                (step_id,),
            ) as cursor:
                row = await cursor.fetchone()

            if row is None:
                return None

            status_str, outputs_json, error = row
            status = StepStatus(status_str)
            outputs: dict[str, Any] = json.loads(outputs_json)

            return StepResult(status=status, outputs=outputs, error=error)

    async def get_sequence_results(self, sequence_id: str) -> list[StepResult]:
        """Retrieve all results for a sequence.

        Args:
            sequence_id: Identifier for the sequence.

        Returns:
            List of StepResult objects, ordered by timestamp.

        Raises:
            RuntimeError: If not connected to database.
        """
        async with self._lock:
            if self._db is None:
                raise RuntimeError("Not connected to database")

            async with self._db.execute(
                """
                SELECT status, outputs, error
                FROM results
                WHERE sequence_id = ?
                ORDER BY timestamp ASC
                """,
                (sequence_id,),
            ) as cursor:
                rows = await cursor.fetchall()

            results = []
            for row in rows:
                status_str, outputs_json, error = row
                status = StepStatus(status_str)
                outputs: dict[str, Any] = json.loads(outputs_json)
                results.append(StepResult(status=status, outputs=outputs, error=error))

            return results

    async def __aenter__(self) -> SQLiteCache:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()
