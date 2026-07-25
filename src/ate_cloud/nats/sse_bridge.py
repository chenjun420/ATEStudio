"""SSE Bridge — bridges NATS JetStream messages to asyncio.Queue per run_id.

In local mode (NATS unavailable), events published via publish_event()
go directly to the asyncio.Queue, enabling SSE streaming without NATS.
When NATS is available, events are also published to JetStream for
cross-process delivery and Last-Event-ID replay.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from nats.aio.client import Client as NatsClient

logger = logging.getLogger(__name__)


class SSEBridge:
    """Bridges NATS JetStream messages to asyncio.Queue per run_id for SSE streaming.

    Works in two modes:
    - Local mode (nats_available=False): Events go directly to asyncio.Queue.
    - NATS mode (nats_available=True): Events are published to JetStream AND
      pushed to local queue for same-process SSE clients.

    The bridge is designed to be attached to app.state.sse_bridge and shared
    across all SSE connections.
    """

    def __init__(self, nc: NatsClient | None = None) -> None:
        """Initialize the SSE bridge.

        Args:
            nc: Optional NATS client. If None or not connected, operates in local mode.
        """
        self._nc = nc
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._subscriptions: dict[str, Any] = {}
        self._event_counter: int = 0

    @property
    def nats_available(self) -> bool:
        """Check whether NATS client is connected and available."""
        return self._nc is not None and self._nc.is_connected

    def get_or_create_queue(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        """Get existing queue for run_id or create a new one.

        Args:
            run_id: The execution run identifier.

        Returns:
            asyncio.Queue bound to this run_id.
        """
        if run_id not in self._queues:
            self._queues[run_id] = asyncio.Queue(maxsize=1000)
        return self._queues[run_id]

    async def start_subscription(self, run_id: str) -> None:
        """Start a NATS push subscription filtered by run_id.

        Subscribes to ate.status.{run_id}.> and pushes messages
        into the queue for this run_id. No-op in local mode.

        Args:
            run_id: The execution run identifier.
        """
        if not self.nats_available:
            logger.debug(f"NATS not available, skipping subscription for {run_id}")
            return

        if run_id in self._subscriptions:
            logger.debug(f"Subscription already active for {run_id}")
            return

        try:
            js = self._nc.jetstream()  # type: ignore[union-attr]
            subject = f"ate.status.{run_id}.>"

            async def callback(msg: Any) -> None:
                try:
                    data = json.loads(msg.data.decode())
                    queue = self.get_or_create_queue(run_id)
                    await queue.put(data)
                    await msg.ack()
                except Exception as e:
                    logger.error(f"Error processing NATS message for {run_id}: {e}")
                    await msg.nak()

            sub = await js.subscribe(subject=subject, queue="ate_sse_bridge", cb=callback)
            self._subscriptions[run_id] = sub
            logger.info(f"Started NATS subscription for {run_id}")

        except Exception as e:
            logger.warning(f"Failed to start NATS subscription for {run_id}: {e}")

    async def replay_from_jetstream(
        self, run_id: str, last_event_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Replay events from JetStream after the given event ID.

        Uses JetStream consumer with start_sequence to replay missed events.
        Yields nothing in local mode (no persistent store to replay from).

        Args:
            run_id: The execution run identifier.
            last_event_id: The last event ID received by the client.

        Yields:
            Event dictionaries for missed events.
        """
        if not self.nats_available:
            logger.debug(f"NATS not available, cannot replay for {run_id}")
            return

        try:
            js = self._nc.jetstream()  # type: ignore[union-attr]
            subject = f"ate.status.{run_id}.>"

            # Parse sequence number from event ID (format: "{run_id}-{seq}")
            try:
                _, seq_str = last_event_id.rsplit("-", 1)
                start_seq = int(seq_str) + 1
            except (ValueError, IndexError):
                logger.warning(f"Invalid Last-Event-ID format: {last_event_id}, skipping replay")
                return

            # Use pull subscription for replay
            psub = await js.pull_subscribe(subject, durable=f"replay_{run_id}")
            try:
                # Fetch messages starting from the sequence after last_event_id
                msgs = await psub.fetch(batch=100, timeout=2.0)
                for msg in msgs:
                    metadata = await msg.metadata()
                    if metadata and metadata.sequence.stream >= start_seq:
                        data = json.loads(msg.data.decode())
                        yield data
                    await msg.ack()
            except asyncio.TimeoutError:
                logger.debug(f"No replay messages available for {run_id}")
            finally:
                await psub.unsubscribe()

        except Exception as e:
            logger.warning(f"Failed to replay from JetStream for {run_id}: {e}")

    async def publish_event(
        self, run_id: str, event_type: str, data: dict[str, Any]
    ) -> None:
        """Publish an event to NATS and also push to local queue.

        In local mode, only pushes to the local asyncio.Queue.
        In NATS mode, also publishes to JetStream subject
        ate.status.{run_id}.{event_type}.

        Args:
            run_id: The execution run identifier.
            event_type: The event type (e.g., EXECUTION_STARTED).
            data: The event payload.
        """
        self._event_counter += 1
        event_id = f"{run_id}-{self._event_counter}"

        event: dict[str, Any] = {
            "id": event_id,
            "type": event_type,
            "run_id": run_id,
            "data": data,
            "timestamp": time.time(),
        }

        # Always push to local queue (works in both modes)
        queue = self.get_or_create_queue(run_id)
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop oldest event to make room
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            queue.put_nowait(event)
            logger.warning(f"Queue full for {run_id}, dropped oldest event")

        # Publish to NATS if available
        if self.nats_available:
            try:
                js = self._nc.jetstream()  # type: ignore[union-attr]
                subject = f"ate.status.{run_id}.{event_type}"
                await js.publish(subject, json.dumps(event).encode())
            except Exception as e:
                logger.warning(f"Failed to publish event to NATS for {run_id}: {e}")

    def remove_queue(self, run_id: str) -> None:
        """Clean up queue and subscription when no more SSE clients.

        Args:
            run_id: The execution run identifier.
        """
        self._queues.pop(run_id, None)

        sub = self._subscriptions.pop(run_id, None)
        if sub is not None:
            try:
                asyncio.get_event_loop().create_task(sub.unsubscribe())
            except Exception:
                pass

    async def cleanup(self) -> None:
        """Clean up all queues and subscriptions on shutdown."""
        for run_id in list(self._subscriptions.keys()):
            sub = self._subscriptions.pop(run_id, None)
            if sub is not None:
                try:
                    await sub.unsubscribe()
                except Exception:
                    pass

        self._queues.clear()
        self._subscriptions.clear()
