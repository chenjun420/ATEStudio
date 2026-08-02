"""Health monitoring service — polls worker KV and persists heartbeat history.

A background task that periodically (every 30s by default) reads all worker
keys from the ``ate-workers`` JetStream KV bucket, determines each worker's
online/offline status based on heartbeat age, and persists a snapshot record
to the ``worker_heartbeats`` database table for dashboard time-series display.

Offline detection: if a worker's last heartbeat (KV entry ``created``
timestamp) is older than ``offline_threshold`` seconds (default 30s), the
worker is marked ``offline`` and a warning is logged. Workers whose KV keys
have already expired (TTL=30s) are simply not seen by the poll — no record
is written for them.

Lifecycle::

    monitor = HealthMonitorService(nc, async_session_factory)
    await monitor.start()
    ...
    await monitor.stop()

The service is standalone — it is NOT auto-started in ``main.py``. Wiring
into the application lifespan is a future task.

Per AGENTS.md section 7: if the KV bucket is unreachable due to a connection
error, operations raise ``RuntimeError`` — no silent degradation.
``NotFoundError`` (bucket does not exist) is handled gracefully (no records
written, warning logged).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from nats.aio.client import Client as NatsClient
from nats.js.errors import KeyNotFoundError, NoKeysError, NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ate_cloud.models.worker_heartbeat import WorkerHeartbeat

logger = logging.getLogger(__name__)

# KV bucket for worker heartbeats (TTL=30s, created by StreamManager).
WORKER_KV_BUCKET: str = "ate-workers"

# Key prefix: workers.{worker_id}
_WORKER_KEY_PREFIX: str = "workers."

# Default poll interval (seconds).
_DEFAULT_POLL_INTERVAL: float = 30.0

# Default offline threshold: a worker is offline if its last heartbeat
# is older than this many seconds.
_DEFAULT_OFFLINE_THRESHOLD: float = 30.0


class HealthMonitorService:
    """Background health monitor that polls worker KV and persists heartbeats.

    Every ``poll_interval`` seconds, reads all ``workers.*`` keys from the
    ``ate-workers`` KV bucket, checks each worker's heartbeat age, and
    writes a :class:`WorkerHeartbeat` record to the database.

    Attributes:
        poll_interval: Seconds between polls (default 30).
        offline_threshold: Seconds after which a heartbeat is considered stale (default 30).
    """

    def __init__(
        self,
        nats_client: NatsClient,
        session_factory: async_sessionmaker[AsyncSession],
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        offline_threshold: float = _DEFAULT_OFFLINE_THRESHOLD,
    ) -> None:
        self._nc = nats_client
        self._session_factory = session_factory
        self._poll_interval = poll_interval
        self._offline_threshold = offline_threshold
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        """Start the background polling loop.

        Creates an :class:`asyncio.Task` that runs ``_poll_loop`` until
        :meth:`stop` is called. Safe to call once; calling twice without
        stopping in between raises ``RuntimeError``.
        """
        if self._running:
            raise RuntimeError("HealthMonitorService already started")
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "HealthMonitorService started (poll_interval=%ss, offline_threshold=%ss)",
            self._poll_interval,
            self._offline_threshold,
        )

    async def stop(self) -> None:
        """Stop the background polling loop and wait for cleanup.

        Safe to call multiple times. Cancels the poll task and awaits
        its termination.
        """
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("HealthMonitorService stopped")

    async def _poll_loop(self) -> None:
        """Main polling loop — runs until :meth:`stop` is called."""
        while self._running:
            try:
                async with self._session_factory() as session:
                    await self.check_once(session)
            except Exception as e:
                logger.warning("Health monitor poll failed: %s", e)
            await asyncio.sleep(self._poll_interval)

    async def check_once(self, session: AsyncSession) -> int:
        """Perform a single health check poll and persist records.

        Reads all worker keys from the KV bucket, determines each worker's
        status, and writes :class:`WorkerHeartbeat` records to the database
        via the provided session. The caller is responsible for committing
        (or the session's autoflush/autocommit configuration handles it).

        Args:
            session: An async database session for writing heartbeat records.

        Returns:
            Number of heartbeat records persisted.

        Raises:
            RuntimeError: If the KV bucket is unreachable (not if missing).
        """
        kv = await self._get_kv()
        if kv is None:
            return 0

        try:
            keys = await kv.keys()
        except NoKeysError:
            return 0
        except NotFoundError:
            return 0

        now = datetime.now(UTC)
        threshold = timedelta(seconds=self._offline_threshold)
        records: list[WorkerHeartbeat] = []

        for key in keys:
            if not key.startswith(_WORKER_KEY_PREFIX):
                continue
            try:
                entry = await kv.get(key)
            except KeyNotFoundError:
                continue  # Key expired between keys() and get()

            worker_id = key[len(_WORKER_KEY_PREFIX):]
            metadata = _parse_metadata(entry.value)

            heartbeat_time = entry.created
            if heartbeat_time is not None:
                if heartbeat_time.tzinfo is None:
                    heartbeat_time = heartbeat_time.replace(tzinfo=UTC)
                age = now - heartbeat_time
            else:
                heartbeat_time = now
                age = timedelta(0)

            if age > threshold:
                status = "offline"
                logger.warning(
                    "Worker '%s' offline — last heartbeat %ss ago",
                    worker_id,
                    int(age.total_seconds()),
                )
            else:
                status = "online"

            records.append(WorkerHeartbeat(
                id=str(uuid.uuid4()),
                worker_id=worker_id,
                hostname=metadata.get("hostname", ""),
                status=status,
                capabilities=metadata.get("capabilities", []),
                current_tasks=metadata.get("current_tasks", 0),
                recorded_at=heartbeat_time,
            ))

        for record in records:
            session.add(record)

        if records:
            await session.commit()
            logger.info("Health monitor persisted %d heartbeat records", len(records))

        return len(records)

    async def _get_kv(self) -> Any:
        """Get the ``ate-workers`` KV bucket handle.

        Returns:
            The nats-py KeyValue handle, or ``None`` if the bucket does
            not exist (graceful handling).

        Raises:
            RuntimeError: If the KV bucket is unreachable for any reason
                other than ``NotFoundError``.
        """
        js = self._nc.jetstream()
        try:
            return await js.key_value(WORKER_KV_BUCKET)
        except NotFoundError:
            logger.warning("KV bucket '%s' does not exist", WORKER_KV_BUCKET)
            return None
        except Exception as e:
            raise RuntimeError(
                f"KV bucket '{WORKER_KV_BUCKET}' not available: {e}"
            ) from e


def _parse_metadata(raw_value: bytes | None) -> dict[str, Any]:
    """Parse worker metadata from a KV entry's raw value.

    Args:
        raw_value: Raw KV value (JSON-encoded metadata bytes).

    Returns:
        Parsed metadata dict, or empty dict if parsing fails.
    """
    if not raw_value:
        return {}
    try:
        decoded: str = raw_value.decode("utf-8")
        result: dict[str, Any] = json.loads(decoded)
        return result
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("Failed to parse worker metadata: %s", e)
        return {}
