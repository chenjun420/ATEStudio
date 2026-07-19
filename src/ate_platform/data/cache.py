"""SQLite-based cache for step execution results.

This module provides async SQLite caching with WAL mode for concurrent access.
Supports storing and retrieving StepResult objects with automatic checkpointing.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import aiosqlite

from ate_platform.types import StepResult, StepStatus


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

    __slots__ = ("_db_path", "_db", "_lock", "_checkpoint_page_threshold")

    def __init__(self, db_path: str, checkpoint_page_threshold: int = 1000) -> None:
        """Initialize the cache.

        Args:
            db_path: Path to SQLite database file, or ":memory:" for in-memory DB.
            checkpoint_page_threshold: WAL checkpoint trigger threshold (default: 1000 pages).
        """
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._checkpoint_page_threshold = checkpoint_page_threshold

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
        async with self._lock:
            if self._db is not None:
                await self._db.close()
                self._db = None

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
