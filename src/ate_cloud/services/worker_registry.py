"""Worker registry service — reads worker heartbeats from JetStream KV.

The ``ate-workers`` KV bucket (TTL=30s) is written by
:class:`~ate_platform.scheduler.jetstream_worker.JetStreamWorker` every
15 seconds. Each key ``workers.{worker_id}`` holds a JSON payload with
hostname, capabilities, and task capacity. The per-key TTL means a key
auto-expires if the worker stops heartbeating — key existence is the
online/offline signal.

Bucket/key conventions (per T7 ISA-95 standardization):
    - Bucket: ``ate-workers`` (lower-kebab)
    - Key:    ``workers.{worker_id}`` (lower.dot)

Per AGENTS.md section 7: if the KV bucket is unavailable due to a
connection error, operations raise ``RuntimeError`` — no silent
degradation. ``NotFoundError`` (bucket does not exist) is handled
gracefully per the task spec (empty list / None / offline).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from nats.aio.client import Client as NatsClient
from nats.js.errors import KeyNotFoundError, NoKeysError, NotFoundError

from ate_cloud.schemas.worker import WorkerHealthResponse, WorkerInfo

logger = logging.getLogger(__name__)

# KV bucket for worker heartbeats (TTL=30s, created by StreamManager).
WORKER_KV_BUCKET: str = "ate-workers"

# Key prefix: workers.{worker_id}
_WORKER_KEY_PREFIX: str = "workers."


class WorkerRegistryService:
    """Reads registered workers from the ``ate-workers`` JetStream KV bucket.

    The service is stateless — each method opens the KV bucket fresh.
    The NATS client is the only dependency, matching the pattern
    established by :class:`~ate_cloud.services.config_distribution.ConfigDistributionService`.
    """

    def __init__(self, nats_client: NatsClient) -> None:
        self._nc = nats_client

    async def _get_kv(self) -> Any:
        """Get the ``ate-workers`` KV bucket handle.

        Returns:
            The nats-py KeyValue handle.

        Raises:
            NotFoundError: If the bucket does not exist (caller handles).
            RuntimeError: If the KV bucket is unreachable for any other reason.
        """
        js = self._nc.jetstream()
        try:
            return await js.key_value(WORKER_KV_BUCKET)
        except NotFoundError:
            raise  # Propagated to caller for graceful handling.
        except Exception as e:
            raise RuntimeError(
                f"KV bucket '{WORKER_KV_BUCKET}' not available: {e}"
            ) from e

    async def list_workers(self) -> list[WorkerInfo]:
        """List all registered workers from the KV bucket.

        Returns:
            List of :class:`WorkerInfo` for every key in the bucket.
            Empty list if the bucket does not exist or has no keys.

        Raises:
            RuntimeError: If the KV bucket is unreachable (not if missing).
        """
        try:
            kv = await self._get_kv()
        except NotFoundError:
            logger.warning("KV bucket '%s' does not exist", WORKER_KV_BUCKET)
            return []
        try:
            keys = await kv.keys()
        except NoKeysError:
            return []
        except NotFoundError:
            return []

        workers: list[WorkerInfo] = []
        for key in keys:
            if not key.startswith(_WORKER_KEY_PREFIX):
                continue
            try:
                entry = await kv.get(key)
            except KeyNotFoundError:
                continue  # Key expired between keys() and get()
            worker_id = key[len(_WORKER_KEY_PREFIX):]
            worker = _build_worker_info(worker_id, entry.value, entry.created)
            workers.append(worker)
        return workers

    async def get_worker(self, worker_id: str) -> WorkerInfo | None:
        """Get a single worker by ID.

        Args:
            worker_id: Unique worker identifier.

        Returns:
            :class:`WorkerInfo` if the worker's key exists, ``None`` otherwise.

        Raises:
            RuntimeError: If the KV bucket is unreachable (not if missing).
        """
        try:
            kv = await self._get_kv()
        except NotFoundError:
            return None
        key = f"{_WORKER_KEY_PREFIX}{worker_id}"
        try:
            entry = await kv.get(key)
        except KeyNotFoundError:
            return None
        return _build_worker_info(worker_id, entry.value, entry.created)

    async def get_worker_health(self, worker_id: str) -> WorkerHealthResponse:
        """Get the health status of a worker.

        A worker is ``online`` if its KV key exists (heartbeated within
        the 30s TTL). Otherwise it is ``offline``.

        Args:
            worker_id: Unique worker identifier.

        Returns:
            :class:`WorkerHealthResponse` with status ``online`` or ``offline``.

        Raises:
            RuntimeError: If the KV bucket is unreachable (not if missing).
        """
        worker = await self.get_worker(worker_id)
        if worker is None:
            return WorkerHealthResponse(
                status="offline",
                worker_info=None,
                last_heartbeat_timestamp=None,
            )
        return WorkerHealthResponse(
            status="online",
            worker_info=worker,
            last_heartbeat_timestamp=worker.last_heartbeat,
        )


def _build_worker_info(
    worker_id: str,
    raw_value: bytes | None,
    created: datetime | None,
) -> WorkerInfo:
    """Build a WorkerInfo from a KV entry's raw value and timestamp.

    Args:
        worker_id: Worker ID (derived from the KV key).
        raw_value: Raw KV value (JSON-encoded metadata bytes).
        created: KV entry creation timestamp (last heartbeat time).

    Returns:
        Parsed :class:`WorkerInfo`.
    """
    metadata: dict[str, Any] = {}
    if raw_value:
        decoded: str = raw_value.decode("utf-8")
        metadata = json.loads(decoded)
    return WorkerInfo(
        worker_id=worker_id,
        hostname=metadata.get("hostname", ""),
        capabilities=metadata.get("capabilities", []),
        max_concurrent_tasks=metadata.get("max_concurrent_tasks", 0),
        current_tasks=metadata.get("current_tasks", 0),
        last_heartbeat=created,
    )
