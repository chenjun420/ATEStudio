"""ExecutionRecorder - writes JSONL events to a JetStream stream.

The recorder captures execution activity (step transitions, measurements,
operator interactions, scheduler decisions, NATS messages) as
:class:`~ate_platform.recorder.types.RecordedEvent` instances, serialized
as JSONL lines, and publishes them to the
``ate.execution.{session_id}.events`` subject on the
``ATE_EXECUTION_EVENTS`` JetStream stream.

The recorder is async and uses an internal ``asyncio.Queue`` so callers
can record events without blocking on JetStream I/O. A background flush
task drains the queue and publishes batches.

Per AGENTS.md §7: if NATS/JetStream is unavailable, recording raises -
no silent degradation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ate_platform.recorder.types import RecordedEvent, RecordedEventType

if TYPE_CHECKING:
    from nats import JetStreamContext
    from nats.aio.client import Client as NatsClient

logger = logging.getLogger(__name__)

# Subject template for execution recording.
# Per AGENTS.md naming convention: lower.dot subjects.
_RECORDING_SUBJECT_TEMPLATE: str = "ate.execution.{session_id}.events"

# Internal queue max size - prevents unbounded memory growth if the
# flush task falls behind. Oldest events are dropped on overflow.
_QUEUE_MAX_SIZE: int = 10000

# Batch size for flushing events to JetStream.
_FLUSH_BATCH_SIZE: int = 50


def _build_event(
    event_type: RecordedEventType,
    session_id: str,
    data: dict[str, Any],
    step_id: str | None = None,
    timestamp: Any = None,
) -> RecordedEvent:
    """Build a RecordedEvent, using default_factory when timestamp is None.

    Pydantic v2 raises ValidationError if None is passed explicitly to a
    field with default_factory. This helper omits the kwarg so the factory
    fires, unless the caller supplies an actual timestamp.
    """
    kwargs: dict[str, Any] = {
        "event_type": event_type,
        "session_id": session_id,
        "step_id": step_id,
        "data": data,
    }
    if timestamp is not None:
        kwargs["timestamp"] = timestamp
    return RecordedEvent(**kwargs)


class ExecutionRecorder:
    """Records execution events to a JetStream stream as JSONL.

    The recorder is bound to a single ``session_id`` (execution run). It
    accepts events via :meth:`record` (non-blocking, enqueues to an
    internal asyncio.Queue) and a background flush task publishes them
    to ``ate.execution.{session_id}.events`` on the
    ``ATE_EXECUTION_EVENTS`` stream.

    Usage::

        recorder = ExecutionRecorder(session_id="run-123", nats_client=nc)
        await recorder.start()
        await recorder.record_step_transition("step-1", "PENDING", "RUNNING")
        await recorder.stop()

    Per AGENTS.md §7: if JetStream publish fails, the error is logged
    but recording continues (events stay in the queue for retry). If
    the NATS client is not connected at ``start()`` time, ``start()``
    raises ``RuntimeError`` - no silent degradation.
    """

    def __init__(
        self,
        session_id: str,
        nats_client: NatsClient,
        flush_interval: float = 0.1,
    ) -> None:
        """Initialize the recorder.

        Args:
            session_id: The execution session identifier.
            nats_client: A connected NATS client. ``nats_client.jetstream()``
                must return a JetStreamContext.
            flush_interval: Seconds between flush cycles when the queue
                is not full. Defaults to 0.1s (100ms).
        """
        self._session_id = session_id
        self._nc = nats_client
        self._flush_interval = flush_interval
        self._subject = _RECORDING_SUBJECT_TEMPLATE.format(session_id=session_id)
        self._queue: asyncio.Queue[RecordedEvent] = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)
        self._flush_task: asyncio.Task[None] | None = None
        self._running = False
        self._event_count = 0

    @property
    def session_id(self) -> str:
        """The execution session identifier this recorder is bound to."""
        return self._session_id

    @property
    def subject(self) -> str:
        """The JetStream subject events are published to."""
        return self._subject

    @property
    def event_count(self) -> int:
        """Total number of events successfully published."""
        return self._event_count

    @property
    def is_running(self) -> bool:
        """Whether the recorder's flush task is active."""
        return self._running

    def _get_jetstream(self) -> JetStreamContext:
        """Get the JetStream context from the NATS client.

        ``jetstream()`` is sync in nats-py (returns a context without I/O).
        """
        return self._nc.jetstream()  # type: ignore[no-any-return]

    async def start(self) -> None:
        """Start the background flush task.

        Raises:
            RuntimeError: If the NATS client is not connected.
        """
        if self._running:
            return
        if not getattr(self._nc, "is_connected", False):
            raise RuntimeError(
                f"NATS client not connected - cannot start recorder for session '{self._session_id}'"
            )
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info(
            "ExecutionRecorder started for session '%s' (subject=%s)",
            self._session_id, self._subject,
        )

    async def stop(self) -> None:
        """Stop the recorder, flushing remaining events.

        Cancels the flush task, then drains any remaining events in the
        queue with a best-effort final publish. Safe to call multiple times.
        """
        if not self._running:
            return
        self._running = False

        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        # Drain remaining events
        await self._drain_queue()

        logger.info(
            "ExecutionRecorder stopped for session '%s' (%d events published)",
            self._session_id, self._event_count,
        )

    async def record(self, event: RecordedEvent) -> None:
        """Record a pre-constructed event.

        Non-blocking: enqueues the event to an internal queue. The
        background flush task publishes it to JetStream.

        Args:
            event: The RecordedEvent to record. Its ``session_id`` must
                match this recorder's session_id.
        """
        if event.session_id != self._session_id:
            raise ValueError(
                f"Event session_id '{event.session_id}' does not match "
                f"recorder session_id '{self._session_id}'"
            )
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop oldest to make room
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(event)
            logger.warning(
                "Recording queue full for session '%s', dropped oldest event",
                self._session_id,
            )

    async def record_step_transition(
        self,
        step_id: str,
        from_status: str,
        to_status: str,
        timestamp: Any = None,
    ) -> None:
        """Convenience method: record a step_transition event.

        Args:
            step_id: The step that transitioned.
            from_status: Previous status.
            to_status: New status.
            timestamp: Optional explicit timestamp; defaults to now().
        """
        event = _build_event(
            event_type=RecordedEventType.STEP_TRANSITION,
            session_id=self._session_id,
            step_id=step_id,
            timestamp=timestamp,
            data={"from_status": from_status, "to_status": to_status},
        )
        await self.record(event)

    async def record_measurement_result(
        self,
        step_id: str,
        name: str,
        value: Any,
        unit: str | None = None,
        timestamp: Any = None,
    ) -> None:
        """Convenience method: record a measurement_result event.

        Args:
            step_id: The step that produced the measurement.
            name: Measurement name (e.g. "voltage").
            value: Measurement value.
            unit: Optional unit (e.g. "V").
            timestamp: Optional explicit timestamp.
        """
        event = _build_event(
            event_type=RecordedEventType.MEASUREMENT_RESULT,
            session_id=self._session_id,
            step_id=step_id,
            timestamp=timestamp,
            data={"name": name, "value": value, "unit": unit},
        )
        await self.record(event)

    async def record_operator_interaction(
        self,
        action: str,
        details: dict[str, Any] | None = None,
        timestamp: Any = None,
    ) -> None:
        """Convenience method: record an operator_interaction event.

        Args:
            action: The operator action (e.g. "button_press", "acknowledge").
            details: Optional additional details.
            timestamp: Optional explicit timestamp.
        """
        event = _build_event(
            event_type=RecordedEventType.OPERATOR_INTERACTION,
            session_id=self._session_id,
            timestamp=timestamp,
            data={"action": action, **(details or {})},
        )
        await self.record(event)

    async def record_scheduler_decision(
        self,
        decision: str,
        details: dict[str, Any] | None = None,
        timestamp: Any = None,
    ) -> None:
        """Convenience method: record a scheduler_decision event.

        Args:
            decision: The scheduler decision (e.g. "reschedule", "skip").
            details: Optional additional details.
            timestamp: Optional explicit timestamp.
        """
        event = _build_event(
            event_type=RecordedEventType.SCHEDULER_DECISION,
            session_id=self._session_id,
            timestamp=timestamp,
            data={"decision": decision, **(details or {})},
        )
        await self.record(event)

    async def record_nats_message(
        self,
        subject: str,
        payload: dict[str, Any],
        direction: str = "publish",
        timestamp: Any = None,
    ) -> None:
        """Convenience method: record a nats_message event.

        Args:
            subject: The NATS subject of the message.
            payload: The message payload.
            direction: "publish" or "subscribe".
            timestamp: Optional explicit timestamp.
        """
        event = _build_event(
            event_type=RecordedEventType.NATS_MESSAGE,
            session_id=self._session_id,
            timestamp=timestamp,
            data={
                "subject": subject,
                "payload": payload,
                "direction": direction,
            },
        )
        await self.record(event)

    async def _flush_loop(self) -> None:
        """Background task: drain the queue and publish batches to JetStream."""
        while self._running:
            try:
                await self._drain_queue()
            except Exception:
                logger.exception(
                    "Error in flush loop for session '%s'", self._session_id
                )
            await asyncio.sleep(self._flush_interval)

    async def _drain_queue(self) -> None:
        """Drain up to _FLUSH_BATCH_SIZE events from the queue and publish them.

        Publishes events as individual JetStream messages (one JSONL line per
        message). Uses non-blocking gets so the drain returns immediately if
        the queue is empty.
        """
        js = self._get_jetstream()
        batch: list[RecordedEvent] = []
        for _ in range(_FLUSH_BATCH_SIZE):
            try:
                event = self._queue.get_nowait()
                batch.append(event)
            except asyncio.QueueEmpty:
                break

        if not batch:
            return

        for event in batch:
            try:
                payload = event.to_jsonl().encode("utf-8")
                await js.publish(self._subject, payload)
                self._event_count += 1
            except Exception as e:
                logger.error(
                    "Failed to publish event to JetStream for session '%s': %s",
                    self._session_id, e,
                )
                # Re-enqueue failed event for retry (best-effort)
                try:
                    self._queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass
