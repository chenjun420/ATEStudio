"""SSE Bridge — bridges NATS JetStream messages to asyncio.Queue per run_id.

In local mode (NATS unavailable), events published via publish_event()
go directly to the asyncio.Queue, enabling SSE streaming without NATS.
When NATS is available, events are also published to JetStream for
cross-process delivery and Last-Event-ID replay.

TEMS A4 category support:
- SSE `event:` line is set to the event's category (event, measurement, alarm)
- This enables frontend clients to filter by category using EventSource

Reference counting:
- get_or_create_queue() increments refcount for each active SSE client
- remove_queue() decrements refcount; queue is deleted only at 0
- This allows multiple SSE clients for the same run_id

Local-mode heartbeat:
- get_local_heartbeat() yields keep-alive events every 15s when NATS unavailable
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from nats.aio.client import Client as NatsClient

from shared.events import EVENT_TYPE_CATEGORIES, EventType

logger = logging.getLogger(__name__)

# Mapping from EventType value string to SSE category string
# Used when the bridge receives event_type as a string (from API endpoints)
_EVENT_TYPE_TO_SSE_CATEGORY: dict[str, str] = {
    et.value: EVENT_TYPE_CATEGORIES[et].value
    for et in EVENT_TYPE_CATEGORIES
}

# Replay constants
_REPLAY_BATCH_SIZE: int = 100
_REPLAY_FETCH_TIMEOUT: float = 2.0

# Heartbeat interval (seconds)
_HEARTBEAT_INTERVAL: float = 15.0


class SSEBridge:
    """Bridges NATS JetStream messages to asyncio.Queue per run_id for SSE streaming.

    Works in two modes:
    - Local mode (nats_available=False): Events go directly to asyncio.Queue.
    - NATS mode (nats_available=True): Events are published to JetStream AND
      pushed to local queue for same-process SSE clients.

    Queues are reference-counted to support multiple SSE clients per run_id.
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
        self._refcounts: dict[str, int] = {}
        self._subscriptions: dict[str, Any] = {}
        self._event_counter: int = 0
        # 独立流队列（如 topology-stream）：key = "{run_id}:{stream}"，
        # 与主事件队列隔离，避免多 SSE 客户端竞争消费。
        self._stream_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._stream_refcounts: dict[str, int] = {}

    @property
    def nats_available(self) -> bool:
        """Check whether NATS client is connected and available."""
        return self._nc is not None and self._nc.is_connected

    def get_or_create_queue(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        """Get existing queue for run_id or create a new one.

        Increments the reference count for this run_id each
        time it is called, enabling multi-client support.

        Args:
            run_id: The execution run identifier.

        Returns:
            asyncio.Queue bound to this run_id.
        """
        if run_id not in self._queues:
            self._queues[run_id] = asyncio.Queue(maxsize=1000)
            self._refcounts[run_id] = 0
        self._refcounts[run_id] += 1
        logger.debug(
            f"Queue refcount for {run_id}: {self._refcounts[run_id]}"
        )
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
                    queue = self._queues.get(run_id)
                    if queue is not None:
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
        Paginates in batches of _REPLAY_BATCH_SIZE (100) until no more events,
        then fails over to a pull subscription for additional messages.
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

            # Use pull subscription for replay with pagination loop
            psub = await js.pull_subscribe(subject, durable=f"replay_{run_id}")
            try:
                total_yielded = 0
                while True:
                    try:
                        msgs = await psub.fetch(
                            batch=_REPLAY_BATCH_SIZE, timeout=_REPLAY_FETCH_TIMEOUT
                        )
                        if not msgs:
                            # Empty batch — no more messages to replay
                            break

                        batch_yielded = 0
                        for msg in msgs:
                            metadata = msg.metadata
                            if metadata and metadata.sequence.stream >= start_seq:
                                data = json.loads(msg.data.decode())
                                yield data
                                batch_yielded += 1
                            await msg.ack()

                        total_yielded += batch_yielded

                        # If batch was smaller than requested, we've reached the end
                        if len(msgs) < _REPLAY_BATCH_SIZE:
                            break

                    except asyncio.TimeoutError:
                        # No more messages available (timeout waiting for batch)
                        break

                if total_yielded > 0:
                    logger.info(
                        f"Replayed {total_yielded} missed events for {run_id} "
                        f"starting from seq {start_seq}"
                    )

            finally:
                await psub.unsubscribe()

        except Exception as e:
            logger.warning(f"Failed to replay from JetStream for {run_id}: {e}")

    async def get_local_heartbeat(
        self, run_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Generate keep-alive events in local mode.

        When NATS is unavailable, yields keep-alive events every
        _HEARTBEAT_INTERVAL (15s) so SSE connections don't time out.

        Yields:
            dict with comment="keep-alive" and empty data for SSE keep-alive.
        """
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            yield {"comment": "keep-alive", "data": {}}

    async def publish_event(
        self, run_id: str, event_type: str, data: dict[str, Any]
    ) -> None:
        """Publish an event to NATS and also push to local queue.

        In local mode, only pushes to the local asyncio.Queue.
        In NATS mode, also publishes to JetStream subject
        ate.status.{run_id}.{event_type}.

        The SSE `event:` line is set to the TEMS A4 category derived from
        the event_type (event, measurement, or alarm). If the event_type
        is not recognized, defaults to "event".

        Args:
            run_id: The execution run identifier.
            event_type: The event type (e.g., EXECUTION_STARTED, MEASUREMENT_RECORDED).
            data: The event payload.
        """
        self._event_counter += 1
        event_id = f"{run_id}-{self._event_counter}"

        # Derive SSE category from event type string
        sse_category = _EVENT_TYPE_TO_SSE_CATEGORY.get(event_type, "event")

        event: dict[str, Any] = {
            "id": event_id,
            "type": event_type,
            "category": sse_category,
            "run_id": run_id,
            "data": data,
            "timestamp": time.time(),
        }

        # Always push to local queue (works in both modes)
        # Create queue if it doesn't exist yet (publish-before-subscribe scenario)
        if run_id not in self._queues:
            self._queues[run_id] = asyncio.Queue(maxsize=1000)
            self._refcounts[run_id] = 0
        queue = self._queues[run_id]
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

    async def push_to_queue_only(
        self, run_id: str, event: dict[str, Any]
    ) -> None:
        """Push a raw event dict into the local SSE queue without republishing.

        Used by consumers that already received the event from NATS (e.g.
        ExecutionStatusRelay) — avoids re-publishing to JetStream, which
        would create an infinite relay loop. The event is delivered
        verbatim to SSE clients via events_for_run().

        Args:
            run_id: The execution run identifier.
            event: The raw event dict to enqueue (no envelope added).
        """
        if run_id not in self._queues:
            self._queues[run_id] = asyncio.Queue(maxsize=1000)
            self._refcounts[run_id] = 0
        queue = self._queues[run_id]
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            queue.put_nowait(event)
            logger.warning(f"Queue full for {run_id}, dropped oldest event")

    def get_stream_queue(self, run_id: str, stream: str) -> asyncio.Queue[dict[str, Any]]:
        """Get an isolated per-stream queue (e.g. "topology").

        Independent from the main events queue so multiple SSE endpoints
        (/events and /topology-stream) can subscribe concurrently without
        competing for messages.

        Args:
            run_id: The execution run identifier.
            stream: Stream name (e.g. "topology").

        Returns:
            asyncio.Queue bound to this (run_id, stream) pair.
        """
        key = f"{run_id}:{stream}"
        if key not in self._stream_queues:
            self._stream_queues[key] = asyncio.Queue(maxsize=1000)
            self._stream_refcounts[key] = 0
        self._stream_refcounts[key] += 1
        return self._stream_queues[key]

    async def publish_stream_event(
        self, run_id: str, stream: str, event_type: str, data: dict[str, Any],
    ) -> None:
        """Publish a stream-scoped event to its isolated queue (and NATS).

        The SSE `event:` line matches ``event_type`` verbatim so clients can
        addEventListener('instrument' / 'link' / 'relay' / 'measurement' / 'fault').

        Args:
            run_id: The execution run identifier.
            stream: Stream name (e.g. "topology").
            event_type: SSE event type (e.g. "instrument").
            data: The event payload.
        """
        self._event_counter += 1
        event_id = f"{run_id}-{self._event_counter}"
        event: dict[str, Any] = {
            "id": event_id,
            "type": event_type,
            "category": event_type,
            "run_id": run_id,
            "data": data,
            "timestamp": time.time(),
        }

        key = f"{run_id}:{stream}"
        if key not in self._stream_queues:
            self._stream_queues[key] = asyncio.Queue(maxsize=1000)
            self._stream_refcounts[key] = 0
        queue = self._stream_queues[key]
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            queue.put_nowait(event)
            logger.warning(f"Stream queue full for {key}, dropped oldest event")

        # NATS：发布到 ate.status.{run_id}.{stream}.{event_type}
        if self.nats_available:
            try:
                js = self._nc.jetstream()  # type: ignore[union-attr]
                subject = f"ate.status.{run_id}.{stream}.{event_type}"
                await js.publish(subject, json.dumps(event).encode())
            except Exception as e:
                logger.warning(f"Failed to publish stream event to NATS for {run_id}: {e}")

    def remove_stream_queue(self, run_id: str, stream: str) -> None:
        """Decrement stream queue refcount; clean up at zero."""
        key = f"{run_id}:{stream}"
        current = self._stream_refcounts.get(key, 0)
        if current <= 0:
            return
        self._stream_refcounts[key] = current - 1
        if self._stream_refcounts[key] <= 0:
            self._stream_queues.pop(key, None)
            self._stream_refcounts.pop(key, None)
            logger.debug(f"Stream queue removed for {key}")

    def remove_queue(self, run_id: str) -> None:
        """Decrement queue reference count and clean up when it reaches zero.

        Only removes the queue and subscription when all SSE clients
        for this run_id have disconnected (refcount reaches 0).

        Args:
            run_id: The execution run identifier.
        """
        current = self._refcounts.get(run_id, 0)
        if current <= 0:
            # Already at zero or never created — nothing to do
            return

        self._refcounts[run_id] = current - 1
        logger.debug(
            f"Queue refcount for {run_id}: {self._refcounts[run_id]} "
            f"(after disconnect)"
        )

        if self._refcounts[run_id] == 0:
            self._queues.pop(run_id, None)
            self._refcounts.pop(run_id, None)

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
        self._refcounts.clear()
        self._subscriptions.clear()

    async def events_for_run(
        self, run_id: str
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream events for a run to an SSE client (async generator).

        Phase 1/2: synchronously drain events already queued for ``run_id``
        via ``get_nowait()`` so buffered events arrive immediately (no
        NATS dependency). Phase 3: block on ``queue.get()`` waiting for
        live events pushed via ``publish_event()`` or
        ``push_to_queue_only()``.

        The generator terminates when ``run_id`` is not registered (no
        queue exists) after the initial drain.

        Args:
            run_id: The execution run identifier.

        Yields:
            Raw event dicts in FIFO order.
        """
        queue = self._queues.get(run_id)
        if queue is None:
            return

        # Phase 1/2: drain already-queued events synchronously.
        while True:
            try:
                event = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            yield event

        # Phase 3: live events.
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            yield event

    async def close(self) -> None:
        """Release all queues and subscriptions (alias for cleanup)."""
        await self.cleanup()

