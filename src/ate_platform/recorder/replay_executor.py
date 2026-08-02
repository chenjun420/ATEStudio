"""ReplayExecutor - replays recorded execution events in timestamp order.

Reads recorded events from the ``ate.execution.{session_id}.events``
JetStream stream (or accepts an in-memory list for testing), sorts them
by timestamp, and yields them with time-accurate delays so the caller
can visualize the original execution timing.

The executor supports time acceleration via ``speed_multiplier``:
- 1.0 = real-time (1s between events = 1s sleep)
- 2.0 = 2x speed (1s between events = 0.5s sleep)
- 5.0 = 5x speed
- 10.0 = 10x speed

The caller receives each event via an async iterator and is responsible
for side effects (e.g., highlighting graph edges, updating UI). The
executor only handles timing and ordering.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol

from ate_platform.recorder.types import RecordedEvent, RecordedEventType

if TYPE_CHECKING:
    from nats import JetStreamContext
    from nats.aio.client import Client as NatsClient

logger = logging.getLogger(__name__)

# Subject template - matches ExecutionRecorder.
_RECORDING_SUBJECT_TEMPLATE: str = "ate.execution.{session_id}.events"

# Fetch batch size when reading from JetStream.
_REPLAY_FETCH_BATCH: int = 100

# Fetch timeout in seconds (idle polling).
_REPLAY_FETCH_TIMEOUT: float = 2.0

# Minimum delay clamp to prevent zero-length sleeps when events share
# the same timestamp or are extremely close.
_MIN_DELAY_SECONDS: float = 0.0

# Cap on per-event delay to avoid very long sleeps when there are large
# gaps in the recording. Events separated by more than this are clamped.
_MAX_DELAY_SECONDS: float = 60.0


class ReplayCallback(Protocol):
    """Protocol for replay event callbacks.

    A callback receives each :class:`RecordedEvent` as it is replayed.
    Implementations can highlight graph edges, update UI, collect
    metrics, etc.
    """

    async def __call__(self, event: RecordedEvent) -> None: ...


class ReplayExecutor:
    """Replays recorded execution events in timestamp order.

    The executor can read events from two sources:
    1. JetStream: ``pull_subscribe`` on ``ate.execution.{session_id}.events``
    2. In-memory: a pre-loaded list of RecordedEvent (for testing or
       when events are already loaded).

    Events are sorted by ``timestamp`` before replay. Time between
    consecutive events is scaled by ``speed_multiplier`` and the
    executor sleeps for the scaled duration before yielding each event.

    Usage (JetStream source)::

        executor = ReplayExecutor(session_id="run-123", nats_client=nc)
        async for event in executor.replay(speed_multiplier=2.0):
            highlight_edge(event.step_id)

    Usage (in-memory source)::

        executor = ReplayExecutor(session_id="run-123")
        async for event in executor.replay_from_events(events, speed_multiplier=5.0):
            highlight_edge(event.step_id)
    """

    def __init__(
        self,
        session_id: str,
        nats_client: NatsClient | None = None,
    ) -> None:
        """Initialize the replay executor.

        Args:
            session_id: The execution session to replay.
            nats_client: Optional NATS client for JetStream source. If
                None, only :meth:`replay_from_events` (in-memory) works.
        """
        self._session_id = session_id
        self._nc = nats_client
        self._subject = _RECORDING_SUBJECT_TEMPLATE.format(session_id=session_id)
        self._cancelled = False
        self._pause_event: asyncio.Event = asyncio.Event()
        self._pause_event.set()  # Not paused at start

    @property
    def session_id(self) -> str:
        """The execution session identifier being replayed."""
        return self._session_id

    @property
    def subject(self) -> str:
        """The JetStream subject being read from."""
        return self._subject

    @property
    def is_paused(self) -> bool:
        """Whether the replay is currently paused."""
        return not self._pause_event.is_set()

    def cancel(self) -> None:
        """Signal the replay to stop after the current event.

        The async iterator will raise ``StopAsyncIteration`` on the
        next iteration, terminating the ``async for`` loop cleanly.
        Also clears the pause flag so a paused replay can terminate.
        """
        self._cancelled = True
        self._pause_event.set()

    def pause(self) -> None:
        """Pause the replay after the current event.

        Subsequent event emission blocks until :meth:`resume` is called.
        Idempotent: calling pause() when already paused is a no-op.
        """
        self._pause_event.clear()

    def resume(self) -> None:
        """Resume a paused replay.

        Clears the pause flag so the replay loop continues from where
        it stopped. Idempotent: calling resume() when not paused is a no-op.
        """
        self._pause_event.set()

    def _get_jetstream(self) -> JetStreamContext:
        """Get the JetStream context from the NATS client.

        Raises:
            RuntimeError: If no NATS client was provided.
        """
        if self._nc is None:
            raise RuntimeError(
                "No NATS client - use replay_from_events() for in-memory replay"
            )
        return self._nc.jetstream()  # type: ignore[no-any-return]

    async def replay(
        self,
        speed_multiplier: float = 1.0,
        callback: ReplayCallback | None = None,
    ) -> list[RecordedEvent]:
        """Replay events from JetStream in timestamp order.

        Pulls all events from the ``ate.execution.{session_id}.events``
        subject, sorts them by timestamp, and yields each with a
        time-accurate delay scaled by ``speed_multiplier``.

        Args:
            speed_multiplier: Time acceleration factor (1.0 = real-time,
                2.0 = 2x, 5.0 = 5x, 10.0 = 10x). Must be > 0.
            callback: Optional async callback invoked for each event.

        Returns:
            List of all replayed events in timestamp order.

        Raises:
            ValueError: If speed_multiplier <= 0.
            RuntimeError: If no NATS client was provided.
        """
        if speed_multiplier <= 0:
            raise ValueError(f"speed_multiplier must be > 0, got {speed_multiplier}")
        events = await self._load_events_from_jetstream()
        return await self._replay_sorted(events, speed_multiplier, callback)

    async def replay_from_events(
        self,
        events: list[RecordedEvent],
        speed_multiplier: float = 1.0,
        callback: ReplayCallback | None = None,
    ) -> list[RecordedEvent]:
        """Replay a pre-loaded list of events in timestamp order.

        Sorts the events by timestamp, then yields each with a
        time-accurate delay scaled by ``speed_multiplier``.

        Args:
            events: Pre-loaded list of RecordedEvent instances.
            speed_multiplier: Time acceleration factor (1.0 = real-time).
            callback: Optional async callback invoked for each event.

        Returns:
            List of all replayed events in timestamp order.

        Raises:
            ValueError: If speed_multiplier <= 0.
        """
        if speed_multiplier <= 0:
            raise ValueError(f"speed_multiplier must be > 0, got {speed_multiplier}")
        return await self._replay_sorted(list(events), speed_multiplier, callback)

    async def replay_iter(
        self,
        speed_multiplier: float = 1.0,
    ) -> AsyncIterator[RecordedEvent]:
        """Async iterator: yield events from JetStream with time delays.

        Equivalent to :meth:`replay` but as an async iterator for
        ``async for`` usage. Does not invoke a callback.

        Args:
            speed_multiplier: Time acceleration factor.

        Yields:
            RecordedEvent instances in timestamp order.
        """
        if speed_multiplier <= 0:
            raise ValueError(f"speed_multiplier must be > 0, got {speed_multiplier}")
        events = await self._load_events_from_jetstream()
        sorted_events = self._sort_events(events)
        async for event in self._iter_with_delays(sorted_events, speed_multiplier):
            yield event

    async def replay_from_events_iter(
        self,
        events: list[RecordedEvent],
        speed_multiplier: float = 1.0,
    ) -> AsyncIterator[RecordedEvent]:
        """Async iterator: yield pre-loaded events with time delays.

        Args:
            events: Pre-loaded list of RecordedEvent instances.
            speed_multiplier: Time acceleration factor.

        Yields:
            RecordedEvent instances in timestamp order.
        """
        if speed_multiplier <= 0:
            raise ValueError(f"speed_multiplier must be > 0, got {speed_multiplier}")
        sorted_events = self._sort_events(list(events))
        async for event in self._iter_with_delays(sorted_events, speed_multiplier):
            yield event

    async def replay_sse(
        self,
        speed_multiplier: float = 1.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield SSE-formatted dicts from JetStream for frontend edge highlighting.

        Reads events from the ``ate.execution.{session_id}.events`` JetStream
        subject, sorts by timestamp, and yields each as a dict shaped for
        ``sse_starlette`` consumption. Each yielded dict has keys:
        ``event``, ``data`` (JSON string), and ``id``.

        Honors pause/resume/cancel. Time between events is scaled by
        ``speed_multiplier``.

        Args:
            speed_multiplier: Time acceleration factor (1.0 = real-time).

        Yields:
            Dicts with keys ``event`` (str), ``data`` (str JSON), ``id`` (str).

        Raises:
            ValueError: If speed_multiplier <= 0.
            RuntimeError: If no NATS client was provided.
        """
        if speed_multiplier <= 0:
            raise ValueError(f"speed_multiplier must be > 0, got {speed_multiplier}")
        events = await self._load_events_from_jetstream()
        sorted_events = self._sort_events(events)
        index = 0
        async for event in self._iter_with_delays(sorted_events, speed_multiplier):
            index += 1
            yield self._event_to_sse_dict(event, index)

    async def replay_sse_from_events(
        self,
        events: list[RecordedEvent],
        speed_multiplier: float = 1.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield SSE-formatted dicts from a pre-loaded event list.

        Args:
            events: Pre-loaded list of RecordedEvent instances.
            speed_multiplier: Time acceleration factor.

        Yields:
            Dicts with keys ``event`` (str), ``data`` (str JSON), ``id`` (str).

        Raises:
            ValueError: If speed_multiplier <= 0.
        """
        if speed_multiplier <= 0:
            raise ValueError(f"speed_multiplier must be > 0, got {speed_multiplier}")
        sorted_events = self._sort_events(list(events))
        index = 0
        async for event in self._iter_with_delays(sorted_events, speed_multiplier):
            index += 1
            yield self._event_to_sse_dict(event, index)

    @staticmethod
    def _event_to_sse_dict(event: RecordedEvent, index: int) -> dict[str, Any]:
        """Convert a RecordedEvent into an SSE-compatible dict.

        The ``event`` field carries the recorded event type so the
        frontend can filter/highlight graph edges. The ``data`` field
        is a JSON string of the full event payload (step_id included).

        Args:
            event: The recorded event.
            index: Sequential index for the SSE event id.

        Returns:
            Dict with ``event``, ``data``, and ``id`` keys.
        """
        payload = event.model_dump(mode="json")
        return {
            "event": event.event_type.value,
            "data": json.dumps(payload, default=str),
            "id": f"{event.session_id}-replay-{index}",
        }

    async def _load_events_from_jetstream(self) -> list[RecordedEvent]:
        """Load all events for this session from JetStream.

        Uses a ephemeral pull subscription to fetch all messages from
        the subject, then unsubscribes.

        Returns:
            List of RecordedEvent instances (unsorted).
        """
        js = self._get_jetstream()
        events: list[RecordedEvent] = []
        psub = await js.pull_subscribe(self._subject)
        try:
            while True:
                try:
                    msgs = await psub.fetch(
                        batch=_REPLAY_FETCH_BATCH, timeout=_REPLAY_FETCH_TIMEOUT
                    )
                except TimeoutError:
                    break
                if not msgs:
                    break
                for msg in msgs:
                    try:
                        line = msg.data.decode("utf-8")
                        events.append(RecordedEvent.from_jsonl(line))
                    except Exception as e:
                        logger.warning(
                            "Failed to parse replay event for session '%s': %s",
                            self._session_id, e,
                        )
                    await msg.ack()
                if len(msgs) < _REPLAY_FETCH_BATCH:
                    break
        finally:
            await psub.unsubscribe()

        logger.info(
            "Loaded %d events for replay (session='%s')",
            len(events), self._session_id,
        )
        return events

    @staticmethod
    def _sort_events(events: list[RecordedEvent]) -> list[RecordedEvent]:
        """Sort events by timestamp (stable - preserves insertion order for ties)."""
        return sorted(events, key=lambda e: e.timestamp)

    @staticmethod
    def _compute_delay(
        prev_ts: Any,
        curr_ts: Any,
        speed_multiplier: float,
    ) -> float:
        """Compute the scaled delay between two timestamps.

        Args:
            prev_ts: Previous event timestamp (datetime).
            curr_ts: Current event timestamp (datetime).
            speed_multiplier: Time acceleration factor.

        Returns:
            Delay in seconds, clamped to [0, _MAX_DELAY_SECONDS].
        """
        if prev_ts is None:
            return _MIN_DELAY_SECONDS
        delta = float((curr_ts - prev_ts).total_seconds())
        if delta <= 0:
            return _MIN_DELAY_SECONDS
        scaled = delta / speed_multiplier
        return min(scaled, _MAX_DELAY_SECONDS)

    async def _wait_if_paused(self) -> None:
        """Block while the replay is paused.

        Awaits the internal ``asyncio.Event`` which is cleared on
        :meth:`pause` and set on :meth:`resume` or :meth:`cancel`.
        Returns immediately if not paused.
        """
        await self._pause_event.wait()

    async def _replay_sorted(
        self,
        events: list[RecordedEvent],
        speed_multiplier: float,
        callback: ReplayCallback | None,
    ) -> list[RecordedEvent]:
        """Replay sorted events with delays, invoking callback if provided.

        Returns the full sorted list (even if cancelled mid-way).
        Honors pause/resume: blocks on ``_wait_if_paused`` before each event.
        """
        sorted_events = self._sort_events(events)
        prev_ts: Any = None
        replayed: list[RecordedEvent] = []

        for event in sorted_events:
            if self._cancelled:
                break
            await self._wait_if_paused()
            if self._cancelled:
                break
            delay = self._compute_delay(prev_ts, event.timestamp, speed_multiplier)
            if delay > _MIN_DELAY_SECONDS:
                await asyncio.sleep(delay)
            replayed.append(event)
            if callback is not None:
                try:
                    await callback(event)
                except Exception:
                    logger.exception(
                        "Replay callback error for session '%s'", self._session_id
                    )
            prev_ts = event.timestamp

        logger.info(
            "Replayed %d/%d events for session '%s' (speed=%.1fx)",
            len(replayed), len(sorted_events), self._session_id, speed_multiplier,
        )
        return replayed

    async def _iter_with_delays(
        self,
        sorted_events: list[RecordedEvent],
        speed_multiplier: float,
    ) -> AsyncIterator[RecordedEvent]:
        """Yield sorted events with time-accurate delays.

        Honors pause/resume: blocks on ``_wait_if_paused`` before each event.
        """
        prev_ts: Any = None
        for event in sorted_events:
            if self._cancelled:
                break
            await self._wait_if_paused()
            if self._cancelled:
                break
            delay = self._compute_delay(prev_ts, event.timestamp, speed_multiplier)
            if delay > _MIN_DELAY_SECONDS:
                await asyncio.sleep(delay)
            yield event
            prev_ts = event.timestamp

    @staticmethod
    def compute_step_durations(
        events: list[RecordedEvent],
    ) -> dict[str, float]:
        """Compute per-step duration from step_transition events.

        Pairs STEP_TRANSITION events by step_id: the time between a
        transition to "RUNNING" and the next transition from that step
        is the step's duration.

        Args:
            events: Recorded events (will be sorted internally).

        Returns:
            Mapping of step_id -> duration in seconds.
        """
        sorted_events = ReplayExecutor._sort_events(list(events))
        durations: dict[str, float] = {}
        start_times: dict[str, Any] = {}

        for event in sorted_events:
            if event.event_type != RecordedEventType.STEP_TRANSITION:
                continue
            if event.step_id is None:
                continue
            to_status = event.data.get("to_status", "")
            if to_status == "RUNNING":
                start_times[event.step_id] = event.timestamp
            elif event.step_id in start_times:
                start = start_times.pop(event.step_id)
                delta = (event.timestamp - start).total_seconds()
                if delta > 0:
                    durations[event.step_id] = delta

        return durations

    @staticmethod
    def compute_diff(
        original: list[RecordedEvent],
        replayed: list[RecordedEvent],
    ) -> dict[str, Any]:
        """Compute a diff between original and replayed event sequences.

        Compares two event lists by (step_id, event_type, data) to
        identify additions, removals, and value changes. Used by the
        frontend ReplayDiffViewer to visualize differences.

        Args:
            original: The original recorded events.
            replayed: The replayed events (e.g., from a second run).

        Returns:
            Dict with keys:
            - ``added``: events in replayed but not original.
            - ``removed``: events in original but not replayed.
            - ``changed``: events with same key but different data.
            - ``summary``: {original_count, replayed_count, added, removed, changed}.
        """
        original_sorted = ReplayExecutor._sort_events(list(original))
        replayed_sorted = ReplayExecutor._sort_events(list(replayed))

        def _event_key(e: RecordedEvent) -> tuple[str, str]:
            return (e.step_id or "", e.event_type.value)

        original_map: dict[tuple[str, str], RecordedEvent] = {}
        for e in original_sorted:
            original_map[_event_key(e)] = e

        replayed_map: dict[tuple[str, str], RecordedEvent] = {}
        for e in replayed_sorted:
            replayed_map[_event_key(e)] = e

        original_keys = set(original_map.keys())
        replayed_keys = set(replayed_map.keys())

        added: list[dict[str, Any]] = [
            replayed_map[k].model_dump(mode="json") for k in sorted(replayed_keys - original_keys)
        ]
        removed: list[dict[str, Any]] = [
            original_map[k].model_dump(mode="json") for k in sorted(original_keys - replayed_keys)
        ]
        changed: list[dict[str, Any]] = []
        for k in sorted(original_keys & replayed_keys):
            o = original_map[k]
            r = replayed_map[k]
            if o.data != r.data:
                changed.append({
                    "key": list(k),
                    "original": o.model_dump(mode="json"),
                    "replayed": r.model_dump(mode="json"),
                })

        return {
            "added": added,
            "removed": removed,
            "changed": changed,
            "summary": {
                "original_count": len(original_sorted),
                "replayed_count": len(replayed_sorted),
                "added": len(added),
                "removed": len(removed),
                "changed": len(changed),
            },
        }

    @staticmethod
    def events_to_jsonl(events: list[RecordedEvent]) -> str:
        """Serialize a list of events as JSONL (one JSON object per line)."""
        return "\n".join(e.to_jsonl() for e in events)

    @staticmethod
    def events_from_jsonl(text: str) -> list[RecordedEvent]:
        """Parse JSONL text into a list of RecordedEvent instances.

        Skips empty lines. Raises on malformed lines.
        """
        events: list[RecordedEvent] = []
        for line in text.strip().splitlines():
            if line.strip():
                events.append(RecordedEvent.from_jsonl(line))
        return events

    @staticmethod
    def serialize_for_api(events: list[RecordedEvent]) -> list[dict[str, Any]]:
        """Serialize events as a list of JSON-compatible dicts (for API responses)."""
        return [e.model_dump(mode="json") for e in ReplayExecutor._sort_events(list(events))]

    @staticmethod
    def parse_payload_data(raw: str | bytes) -> dict[str, Any]:
        """Parse a JSON payload string/bytes into a dict.

        Helper for JetStream message deserialization.
        """
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        result: dict[str, Any] = json.loads(raw)
        return result
