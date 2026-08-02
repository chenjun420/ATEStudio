"""Config watcher — monitors JetStream KV for worker config changes.

Subscribes to the ``ate-configs`` KV bucket, watching for changes to keys
matching ``workers.{worker_id}.>``. When a config value changes, the
registered callback is invoked with the config key (without the worker
prefix) and the new value.

Uses nats-py's async ``kv.watch()`` API — no threads, fully async. The watch
loop runs as an ``asyncio.Task`` and is cancelled on ``stop()``.

Typical detection latency: <2 seconds (NATS delivers KV changes via a
push subscription with no polling).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import nats
from nats.aio.client import Client as NatsClient

logger = logging.getLogger(__name__)

# Must match ConfigDistributionService.CONFIG_KV_BUCKET
_CONFIG_KV_BUCKET: str = "ate-configs"
_WORKER_KEY_PREFIX: str = "workers"

# Type alias for the config change callback.
# Callable[[config_key, value], Awaitable[None]]
ConfigChangeCallback = Callable[[str, str], Awaitable[None]]


class ConfigWatcher:
    """Watches the ``ate-configs`` KV bucket for worker config changes.

    On :meth:`start`, connects to NATS and begins watching for changes to
    keys matching ``workers.{worker_id}.>``. Each change invokes the
    registered callback with ``(config_key, value)`` where ``config_key``
    is the key without the ``workers.{worker_id}.`` prefix.

    The watcher is fully async — no threads. The watch loop runs as an
    ``asyncio.Task`` and is cancelled on :meth:`stop`.

    Initial values: the nats-py ``kv.watch()`` delivers current values of
    matching keys first (as PUT entries), then a ``None`` marker to signal
    end of initial state, then ongoing changes. The callback is invoked for
    each initial value AND each subsequent change.

    Delete operations (KV ``DEL`` / ``PURGE``) are skipped — the callback
    is only invoked for value updates (PUT).
    """

    def __init__(
        self,
        nats_url: str = "nats://localhost:4222",
        worker_id: str = "",
        on_config_change: ConfigChangeCallback | None = None,
    ) -> None:
        self._nats_url = nats_url
        self._worker_id = worker_id
        self._on_config_change = on_config_change
        self._nc: NatsClient | None = None
        self._watcher: Any = None
        self._watch_task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def worker_id(self) -> str:
        """The worker identifier being watched."""
        return self._worker_id

    async def start(self, nc: NatsClient | None = None) -> None:
        """Connect to NATS and start watching for config changes.

        Args:
            nc: Optional pre-connected NATS client. If ``None``, a new
                connection is made to ``nats_url``.

        Raises:
            RuntimeError: If already running or ``worker_id`` is empty.
        """
        if self._running:
            raise RuntimeError("ConfigWatcher already running")
        if not self._worker_id:
            raise RuntimeError("worker_id must be set before starting")

        if nc is not None:
            self._nc = nc
        else:
            self._nc = await nats.connect(self._nats_url)

        js = self._nc.jetstream()
        kv = await js.key_value(_CONFIG_KV_BUCKET)

        # Watch all keys under workers.{worker_id}.>
        # The ">" wildcard matches one or more trailing subject tokens,
        # so it catches multi-segment config keys like "instrument.osc.rate".
        key_filter = f"{_WORKER_KEY_PREFIX}.{self._worker_id}.>"
        self._watcher = await kv.watch(keys=key_filter)

        self._running = True
        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info(
            "ConfigWatcher started for worker '%s' (filter=%s)",
            self._worker_id, key_filter,
        )

    async def _watch_loop(self) -> None:
        """Iterate over KV watch entries and invoke callbacks.

        Handles three entry types from the nats-py KeyWatcher:
        - ``None`` — end-of-initial-values marker (skip, continue)
        - ``KeyValue.Entry`` with ``operation=None`` — PUT (invoke callback)
        - ``KeyValue.Entry`` with ``operation`` set — DEL/PURGE (skip)
        """
        prefix = f"{_WORKER_KEY_PREFIX}.{self._worker_id}."
        try:
            async for entry in self._watcher:
                if not self._running:
                    break
                # None marks the end of initial values delivery.
                if entry is None:
                    continue
                # Skip delete/purge operations — callback is for value updates only.
                if entry.operation is not None:
                    logger.debug(
                        "Skipping config %s for key '%s' (worker=%s)",
                        entry.operation, entry.key, self._worker_id,
                    )
                    continue
                key: str = entry.key
                if not key.startswith(prefix):
                    continue
                config_key = key[len(prefix):]
                value = entry.value.decode("utf-8") if entry.value else ""
                logger.debug(
                    "Config change: worker=%s key=%s revision=%s",
                    self._worker_id, config_key, entry.revision,
                )
                if self._on_config_change is not None:
                    try:
                        await self._on_config_change(config_key, value)
                    except Exception:
                        logger.exception(
                            "Config change callback failed for key '%s'",
                            config_key,
                        )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("ConfigWatcher loop error for worker '%s'", self._worker_id)

    async def stop(self) -> None:
        """Stop watching, unsubscribe, and close the NATS connection.

        Safe to call multiple times. Cancels the watch task, unsubscribes
        the KV watcher, and closes the NATS client (if we created it).
        """
        self._running = False

        if self._watcher is not None:
            try:
                await self._watcher.stop()
            except Exception:
                logger.debug("Error stopping KV watcher (ignored)", exc_info=True)
            self._watcher = None

        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None

        if self._nc is not None:
            try:
                await self._nc.close()
            except Exception:
                logger.debug("Error closing NATS connection (ignored)", exc_info=True)
            self._nc = None

        logger.info("ConfigWatcher stopped for worker '%s'", self._worker_id)
