# allow: SIZE_OK — single responsibility module, all methods serve LeafNodeRunner.
"""LeafNodeRunner — wraps JetStreamWorker for edge autonomy with WAN-aware buffering.

Connects to a local NATS leaf node (which provides local JetStream persistence)
and monitors WAN connectivity to the upstream cloud NATS. When WAN drops, local
task processing continues uninterrupted; messages destined for the cloud are
buffered locally. On WAN reconnect, buffered messages are replayed upstream via
sync_backlog().

Architecture:
    Worker → local NATS (leaf node) → [WAN] → cloud NATS

The leaf node itself handles message persistence and upstream sync at the
server level. LeafNodeRunner adds application-level WAN awareness: it maintains
a separate remote connection for direct cloud communication, detects WAN status
by attempting core NATS publishes, and buffers messages that cannot be sent
upstream immediately.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import nats
from nats.aio.client import Client as NatsClient

from .jetstream_worker import JetStreamWorker

logger = logging.getLogger(__name__)

_WAN_CHECK_SUBJECT = "ate.wan-check"
_WAN_CHECK_INTERVAL: float = 30.0
_WAN_FLUSH_TIMEOUT: int = 5
_MAX_RECONNECT_ATTEMPTS = -1  # infinite
_RECONNECT_TIME_WAIT: float = 2.0


@dataclass
class BufferedMessage:
    """Message buffered while WAN is disconnected, pending upstream replay."""

    subject: str
    data: bytes


class LeafNodeRunner:
    """Wraps JetStreamWorker for edge autonomy with WAN-aware upstream buffering.

    The runner connects to a local NATS leaf node (passed to the worker for
    local task processing) and separately monitors WAN connectivity to the
    cloud NATS. When WAN is down, upstream messages are buffered; on reconnect,
    sync_backlog() replays them.

    Args:
        local_url: NATS URL of the local leaf node.
        remote_url: NATS URL of the upstream cloud NATS.
        worker: JetStreamWorker instance to wrap. If None, a default worker
            connected to local_url is created.
    """

    def __init__(
        self,
        local_url: str = "nats://localhost:4222",
        remote_url: str = "nats://cloud-nats:4222",
        worker: JetStreamWorker | None = None,
    ) -> None:
        self._local_url = local_url
        self._remote_url = remote_url
        self._worker = worker if worker is not None else JetStreamWorker(nats_url=local_url)
        self._local_nc: NatsClient | None = None
        self._remote_nc: NatsClient | None = None
        self._buffer: list[BufferedMessage] = []
        self._wan_connected: bool = False
        self._wan_check_task: asyncio.Task[None] | None = None
        self._syncing: bool = False
        self._running: bool = False

    @property
    def worker(self) -> JetStreamWorker:
        """The wrapped JetStreamWorker instance."""
        return self._worker

    @property
    def is_wan_connected(self) -> bool:
        """Whether the WAN (upstream cloud NATS) is currently reachable.

        Returns the cached result of the last WAN check. The check is
        performed by attempting a core NATS publish to a remote subject
        and catching errors. The check runs periodically in the background
        and on every publish_upstream() call.
        """
        return self._wan_connected

    @property
    def buffer_size(self) -> int:
        """Number of messages currently buffered waiting for WAN reconnect."""
        return len(self._buffer)

    async def start(self) -> None:
        """Connect to local and remote NATS, start worker and WAN monitor.

        Connects to the local leaf node first (required for worker operation),
        then attempts the remote cloud connection. If the remote is unreachable,
        start() still succeeds — the runner operates in WAN-disconnected mode
        with message buffering.
        """
        # Connect to local NATS leaf node
        self._local_nc = await nats.connect(self._local_url)
        await self._worker.start(nc=self._local_nc)

        # Connect to remote cloud NATS for WAN monitoring
        await self._connect_remote()

        self._running = True
        self._wan_check_task = asyncio.create_task(self._wan_monitor_loop())
        logger.info(
            "LeafNodeRunner started (local=%s, remote=%s, wan_connected=%s)",
            self._local_url,
            self._remote_url,
            self._wan_connected,
        )

    async def _connect_remote(self) -> None:
        """Establish remote NATS connection with infinite auto-reconnect.

        On failure, sets wan_connected=False and leaves the remote connection
        as None. The nats-py client will continue reconnection attempts in
        the background if the initial connection succeeds but later drops.
        """
        try:
            self._remote_nc = await nats.connect(
                self._remote_url,
                allow_reconnect=True,
                max_reconnect_attempts=_MAX_RECONNECT_ATTEMPTS,
                reconnect_time_wait=_RECONNECT_TIME_WAIT,
                error_cb=self._on_remote_error,
                disconnected_cb=self._on_remote_disconnected,
                reconnected_cb=self._on_remote_reconnect,
                closed_cb=self._on_remote_closed,
            )
            self._wan_connected = bool(self._remote_nc.is_connected)
            logger.info("Connected to remote cloud NATS at %s", self._remote_url)
        except Exception as e:
            self._wan_connected = False
            self._remote_nc = None
            logger.warning("Failed to connect to remote cloud NATS: %s", e)

    async def _on_remote_reconnect(self) -> None:
        """Called by nats-py when the remote connection reconnects.

        Triggers sync_backlog() to replay any messages buffered during
        the WAN outage.
        """
        logger.info("WAN reconnected — triggering backlog sync")
        self._wan_connected = True
        await self.sync_backlog()

    async def _on_remote_disconnected(self) -> None:
        """Called by nats-py when the remote connection disconnects."""
        logger.warning("WAN disconnected — buffering upstream messages")
        self._wan_connected = False

    async def _on_remote_closed(self) -> None:
        """Called by nats-py when the remote connection is closed."""
        logger.warning("Remote NATS connection closed")
        self._wan_connected = False

    async def _on_remote_error(self, e: Exception) -> None:
        """Called by nats-py on remote connection errors."""
        logger.warning("Remote NATS error: %s", e)

    async def _wan_monitor_loop(self) -> None:
        """Background loop that periodically checks WAN connectivity.

        Runs every _WAN_CHECK_INTERVAL seconds. Uses _check_wan() which
        attempts a core NATS publish to detect connectivity.
        """
        while self._running:
            try:
                await asyncio.sleep(_WAN_CHECK_INTERVAL)
                await self._check_wan()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("WAN monitor error: %s", e)

    async def _check_wan(self) -> bool:
        """Check WAN status by attempting a core NATS publish to remote.

        Publishes a ping to _WAN_CHECK_SUBJECT and flushes. If the publish
        or flush raises an error, WAN is considered down.

        Returns:
            True if WAN is reachable, False otherwise.
        """
        if self._remote_nc is None or not self._remote_nc.is_connected:
            self._wan_connected = False
            return False
        try:
            await self._remote_nc.publish(_WAN_CHECK_SUBJECT, b"ping")
            await self._remote_nc.flush(timeout=_WAN_FLUSH_TIMEOUT)
            self._wan_connected = True
            return True
        except Exception as e:
            logger.warning("WAN check failed: %s", e)
            self._wan_connected = False
            return False

    async def publish_upstream(self, subject: str, data: bytes) -> bool:
        """Publish a message upstream to the cloud NATS.

        When WAN is connected, publishes directly to the remote NATS.
        When WAN is down, buffers the message for later replay via
        sync_backlog().

        Args:
            subject: NATS subject to publish to.
            data: Message payload as bytes.

        Returns:
            True if published directly, False if buffered.
        """
        if self._wan_connected and self._remote_nc is not None:
            try:
                await self._remote_nc.publish(subject, data)
                return True
            except Exception as e:
                logger.warning("Upstream publish failed, buffering: %s", e)
                self._wan_connected = False
        self._buffer.append(BufferedMessage(subject=subject, data=data))
        logger.debug(
            "Buffered message for subject %s (buffer_size=%d)",
            subject,
            len(self._buffer),
        )
        return False

    async def sync_backlog(self) -> int:
        """Replay buffered messages to the upstream cloud NATS.

        Called automatically on WAN reconnect via reconnected_cb. Can also
        be called manually to force a replay attempt.

        Messages that fail to publish are kept in the buffer for the next
        sync attempt. New messages arriving during sync are preserved.

        Returns:
            Number of messages successfully replayed.
        """
        if self._syncing:
            return 0
        if not self._buffer:
            return 0
        if self._remote_nc is None or not self._wan_connected:
            logger.debug("sync_backlog skipped — WAN not connected")
            return 0

        self._syncing = True
        try:
            pending = list(self._buffer)
            self._buffer.clear()

            replayed = 0
            failed: list[BufferedMessage] = []
            for msg in pending:
                try:
                    await self._remote_nc.publish(msg.subject, msg.data)
                    replayed += 1
                except Exception as e:
                    logger.warning("Backlog replay failed for %s: %s", msg.subject, e)
                    failed.append(msg)

            # Put back failed messages, preserving order with any new arrivals
            self._buffer = failed + self._buffer
            if failed:
                self._wan_connected = False
                logger.warning(
                    "Backlog sync partial: %d replayed, %d failed",
                    replayed,
                    len(failed),
                )
            else:
                logger.info("Backlog sync complete: %d messages replayed", replayed)
            return replayed
        finally:
            self._syncing = False

    async def stop(self) -> None:
        """Stop WAN monitor, stop worker, close all NATS connections.

        Cancels the background WAN check task, stops the wrapped worker
        (which closes the local NATS connection), and drains + closes the
        remote NATS connection.
        """
        self._running = False

        if self._wan_check_task is not None:
            self._wan_check_task.cancel()
            try:
                await self._wan_check_task
            except asyncio.CancelledError:
                pass
            self._wan_check_task = None

        await self._worker.stop()

        if self._remote_nc is not None:
            try:
                await self._remote_nc.drain()
                await self._remote_nc.close()
            except Exception as e:
                logger.warning("Error closing remote NATS: %s", e)
            self._remote_nc = None

        self._wan_connected = False
        logger.info("LeafNodeRunner stopped")
