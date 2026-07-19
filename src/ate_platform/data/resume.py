"""Resume manager for reliable result uploads.

This module provides the ResumeManager class which handles:
- Reliable upload of step results to NATS
- Automatic retry with exponential backoff
- Recovery of pending uploads after restart
- Persistence of upload queue in SQLite
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ate_platform.types import StepResult

if TYPE_CHECKING:
    from ate_platform.data.cache import SQLiteCache
    from ate_platform.data.publisher import NATSPublisher

logger = logging.getLogger(__name__)


@dataclass
class PendingMessage:
    """Represents a message pending upload.

    Attributes:
        step_id: Unique identifier for the step
        result: The StepResult to upload
        sequence_id: Optional sequence ID
        retry_count: Number of failed upload attempts
    """

    step_id: str
    result: StepResult
    sequence_id: str | None = None
    retry_count: int = 0


class ResumeManager:
    """Manages reliable upload of step results with retry logic.

    Features:
        - Async upload loop with configurable batch interval
        - Exponential backoff retry (up to 3 attempts)
        - Recovery of pending uploads on startup
        - Persistence in SQLite upload_queue table

    Example:
        >>> cache = SQLiteCache("results.db")
        >>> publisher = NATSPublisher(["nats://localhost:4222"])
        >>> manager = ResumeManager(cache, publisher)
        >>> await manager.start()
        >>> await manager.upload_result(result)
        >>> await manager.stop()
    """

    def __init__(
        self,
        cache: SQLiteCache | None,
        publisher: NATSPublisher | None,
        max_retries: int = 3,
        base_backoff: float = 1.0,
        batch_interval: float = 0.1,
    ) -> None:
        """Initialize the resume manager.

        Args:
            cache: SQLite cache for persistence (can be None)
            publisher: NATS publisher for uploads (can be None)
            max_retries: Maximum number of retry attempts (default: 3)
            base_backoff: Base backoff time in seconds (default: 1.0)
            batch_interval: Interval between upload batches (default: 0.1)
        """
        self._cache = cache
        self._publisher = publisher
        self._pending: asyncio.Queue[PendingMessage] = asyncio.Queue()
        self._running = False
        self._upload_task: asyncio.Task | None = None
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._batch_interval = batch_interval

    async def start(self) -> None:
        """Start the upload loop and recover pending messages."""
        if self._running:
            logger.warning("ResumeManager already running")
            return

        self._running = True

        # Recover pending uploads from database
        await self.recover()

        # Start the upload loop
        self._upload_task = asyncio.create_task(self._upload_loop())
        logger.info("ResumeManager started")

    async def stop(self) -> None:
        """Stop the upload loop gracefully."""
        if not self._running:
            return

        self._running = False

        # Cancel upload task
        if self._upload_task is not None:
            self._upload_task.cancel()
            try:
                await self._upload_task
            except asyncio.CancelledError:
                pass
            self._upload_task = None

        # Process remaining pending messages before stop
        await self._process_remaining()

        logger.info("ResumeManager stopped")

    async def upload_result(
        self,
        result: StepResult,
        step_id: str,
        sequence_id: str | None = None,
    ) -> None:
        """Queue a result for upload.

        Args:
            result: The StepResult to upload
            step_id: Unique identifier for the step
            sequence_id: Optional sequence ID for grouping
        """
        message = PendingMessage(
            step_id=step_id,
            result=result,
            sequence_id=sequence_id,
            retry_count=0,
        )

        # Persist to database first
        await self._persist_message(message)

        # Add to in-memory queue
        await self._pending.put(message)
        logger.debug(f"Queued result for upload: step_id={step_id}")

    async def retry_pending(self) -> None:
        """Retry all pending failed messages.

        This method re-queues all messages from the upload_queue
        that have not exceeded max retries.
        """
        if self._cache is None:
            logger.warning("Cannot retry: cache not available")
            return

        try:
            # Get all pending messages from database
            messages = await self._load_pending_messages()

            retried = 0
            for msg in messages:
                if msg.retry_count < self._max_retries:
                    await self._pending.put(msg)
                    retried += 1

            logger.info(f"Re-queued {retried} messages for retry")

        except Exception as e:
            logger.error(f"Failed to retry pending messages: {e}")

    async def recover(self) -> None:
        """Recover pending uploads from the database.

        Loads all messages from upload_queue that haven't exceeded
        max retries and queues them for upload.
        """
        if self._cache is None:
            logger.debug("Skipping recovery: cache not available")
            return

        try:
            messages = await self._load_pending_messages()

            recovered = 0
            for msg in messages:
                await self._pending.put(msg)
                recovered += 1

            logger.info(f"Recovered {recovered} pending uploads")

        except Exception as e:
            logger.error(f"Failed to recover pending uploads: {e}")

    async def _upload_loop(self) -> None:
        """Background task that processes the upload queue."""
        while self._running:
            try:
                # Wait for a message with timeout to allow checking _running
                try:
                    message = await asyncio.wait_for(
                        self._pending.get(),
                        timeout=self._batch_interval,
                    )
                except TimeoutError:
                    continue

                # Attempt to upload
                success = await self._attempt_upload(message)

                if success:
                    # Remove from database on success
                    await self._remove_message(message.step_id)
                    logger.debug(f"Successfully uploaded: step_id={message.step_id}")
                else:
                    # Handle failure
                    if message.retry_count >= self._max_retries:
                        logger.error(
                            f"Max retries exceeded for step_id={message.step_id}"
                        )
                        await self._remove_message(message.step_id)
                    else:
                        # Update retry count and re-queue
                        message.retry_count += 1
                        await self._update_retry_count(message)

                        # Exponential backoff before re-queue
                        backoff = self._base_backoff * (2 ** (message.retry_count - 1))
                        await asyncio.sleep(backoff)

                        await self._pending.put(message)
                        logger.warning(
                            f"Upload failed, retry {message.retry_count}/{self._max_retries}: "
                            f"step_id={message.step_id}"
                        )

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in upload loop: {e}")
                await asyncio.sleep(1.0)  # Prevent tight error loop

    async def _attempt_upload(self, message: PendingMessage) -> bool:
        """Attempt to upload a message to NATS.

        Args:
            message: The message to upload

        Returns:
            True if upload succeeded, False otherwise
        """
        if self._publisher is None:
            logger.debug("Skipping upload: publisher not available")
            return False

        if not self._publisher.is_connected:
            logger.warning("Cannot upload: publisher not connected")
            return False

        try:
            # Build payload
            payload = self._build_payload(message)
            subject = f"ate.results.{message.step_id}"

            # Publish to NATS
            return await self._publisher.publish(subject, payload)

        except Exception as e:
            logger.error(f"Upload attempt failed: {e}")
            return False

    def _build_payload(self, message: PendingMessage) -> bytes:
        """Build JSON payload for upload.

        Args:
            message: The message to build payload for

        Returns:
            JSON-encoded bytes
        """
        data = {
            "step_id": message.step_id,
            "sequence_id": message.sequence_id,
            "status": message.result.status.value,
            "outputs": message.result.outputs,
            "error": message.result.error,
            "timestamp": datetime.now().isoformat(),
        }
        return json.dumps(data).encode("utf-8")

    async def _persist_message(self, message: PendingMessage) -> None:
        """Persist a message to the database.

        Args:
            message: The message to persist
        """
        if self._cache is None:
            return

        try:
            # Access the internal database connection
            # We need to directly insert into upload_queue table
            async with self._cache._lock:
                if self._cache._db is None:
                    return

                payload = self._build_payload(message)
                timestamp = datetime.now().isoformat()

                await self._cache._db.execute(
                    """
                    INSERT OR REPLACE INTO upload_queue
                    (payload, retry_count, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (payload.decode("utf-8"), message.retry_count, timestamp),
                )
                await self._cache._db.commit()

        except Exception as e:
            logger.error(f"Failed to persist message: {e}")

    async def _remove_message(self, step_id: str) -> None:
        """Remove a message from the database.

        Note: This removes based on step_id extracted from payload.

        Args:
            step_id: The step ID to remove
        """
        if self._cache is None:
            return

        try:
            async with self._cache._lock:
                if self._cache._db is None:
                    return

                # Delete entries where payload contains the step_id
                # Since we store full JSON, we match on the step_id field
                await self._cache._db.execute(
                    """
                    DELETE FROM upload_queue
                    WHERE json_extract(payload, '$.step_id') = ?
                    """,
                    (step_id,),
                )
                await self._cache._db.commit()

        except Exception as e:
            logger.error(f"Failed to remove message: {e}")

    async def _update_retry_count(self, message: PendingMessage) -> None:
        """Update retry count in the database.

        Args:
            message: The message with updated retry count
        """
        if self._cache is None:
            return

        try:
            async with self._cache._lock:
                if self._cache._db is None:
                    return

                payload = self._build_payload(message)

                await self._cache._db.execute(
                    """
                    UPDATE upload_queue
                    SET payload = ?, retry_count = ?
                    WHERE json_extract(payload, '$.step_id') = ?
                    """,
                    (payload.decode("utf-8"), message.retry_count, message.step_id),
                )
                await self._cache._db.commit()

        except Exception as e:
            logger.error(f"Failed to update retry count: {e}")

    async def _load_pending_messages(self) -> list[PendingMessage]:
        """Load all pending messages from the database.

        Returns:
            List of PendingMessage objects
        """
        if self._cache is None:
            return []

        messages: list[PendingMessage] = []

        try:
            async with self._cache._lock:
                if self._cache._db is None:
                    return []

                async with self._cache._db.execute(
                    """
                    SELECT payload, retry_count FROM upload_queue
                    ORDER BY created_at ASC
                    """
                ) as cursor:
                    rows = await cursor.fetchall()

                for row in rows:
                    payload_str, retry_count = row
                    try:
                        data = json.loads(payload_str)
                        result = StepResult(
                            status=data["status"],
                            outputs=data.get("outputs", {}),
                            error=data.get("error"),
                        )
                        messages.append(
                            PendingMessage(
                                step_id=data["step_id"],
                                result=result,
                                sequence_id=data.get("sequence_id"),
                                retry_count=retry_count,
                            )
                        )
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning(f"Skipping invalid payload: {e}")

        except Exception as e:
            logger.error(f"Failed to load pending messages: {e}")

        return messages

    async def _process_remaining(self) -> None:
        """Process remaining messages in the queue before shutdown."""
        while not self._pending.empty():
            try:
                message = self._pending.get_nowait()
                success = await self._attempt_upload(message)

                if success:
                    await self._remove_message(message.step_id)
                else:
                    # Keep in database for next startup
                    logger.warning(
                        f"Could not upload before shutdown: step_id={message.step_id}"
                    )

            except asyncio.QueueEmpty:
                break
            except Exception as e:
                logger.error(f"Error processing remaining messages: {e}")
                break
