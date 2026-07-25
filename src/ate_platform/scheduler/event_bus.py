"""Event bus for asynchronous event-driven communication in ATE Platform.

This module provides a publish-subscribe event system:
- EventType: Enum of supported event types (imported from shared.events)
- Event: Data container for event messages (imported from shared.events)
- EventBus: Async event bus with pub/sub support and wildcard subscriptions
- Thread-safe sync-to-async bridge for publishing from synchronous code
"""

import asyncio
import inspect
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from shared.events import Event, EventType

logger = logging.getLogger(__name__)

# Type alias for callback functions (sync or async)
Callback = Callable[[Event], None] | Callable[[Event], Any]


class _StopSentinel:
    """Sentinel class for signaling event loop stop."""

    pass


class EventBus:
    """Asynchronous publish-subscribe event bus.

    Supports:
    - Typed events via EventType enum
    - Wildcard subscriptions (subscribe to all events)
    - Both sync and async callbacks
    - Graceful shutdown
    - Thread-safe sync-to-async bridge via publish_sync() and set_event_loop()

    Example:
        ```python
        bus = EventBus()

        # Subscribe to specific event type
        bus.subscribe(EventType.STEP_STATUS_CHANGED, my_handler)

        # Subscribe to all events (wildcard)
        bus.subscribe(None, my_logger)

        # Publish an event (async)
        await bus.publish(EventType.STEP_STATUS_CHANGED, {"step": "test"})

        # Publish from synchronous code (thread-safe)
        bus.set_event_loop(asyncio.get_running_loop())
        bus.publish_sync(EventType.STEP_STATUS_CHANGED, {"step": "test"})

        # Start/stop the event loop
        await bus.start()
        await bus.stop()
        ```
    """

    def __init__(self) -> None:
        """Initialize the event bus."""
        self._subscribers: dict[EventType | None, list[Callback]] = {}
        self._queue: asyncio.Queue[Event | _StopSentinel] = asyncio.Queue()
        self._running: bool = False
        self._task: asyncio.Task[None] | None = None
        # Sentinel object for stop signal
        self._stop_sentinel = _StopSentinel()
        # Thread-safe bridge: stored event loop reference for sync publishing
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_lock: threading.Lock = threading.Lock()
        # Pending queue for events published before loop is available
        self._pending_queue: list[tuple[EventType, dict[str, Any]]] = []
        # Statistics counters
        self._published: int = 0
        self._delivered: int = 0
        self._dropped: int = 0

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Store a reference to the event loop for thread-safe sync publishing.

        Call this from the async context that owns the event loop.
        After this, publish_sync() can be called from any thread.

        Drains any events queued by publish_sync() calls made before the loop
        was available, scheduling them for delivery on the newly set loop.

        Args:
            loop: The running event loop to use for scheduling publishes
        """
        with self._loop_lock:
            self._loop = loop
            pending = list(self._pending_queue)
            self._pending_queue.clear()

        # Drain pending events outside the lock to avoid holding it during async work
        for event_type, data in pending:
            asyncio.run_coroutine_threadsafe(self.publish(event_type, data), loop)

    def publish_sync(self, event_type: EventType, data: dict[str, Any]) -> None:
        """Publish an event from synchronous (non-async) code, thread-safe.

        Uses the event loop reference set via set_event_loop() to schedule
        the async publish on the correct loop. If no loop reference is set,
        attempts to discover the running loop. If no loop is available at all,
        the event is silently dropped (logged as a warning).

        This method is safe to call from any thread, including worker threads
        and multiprocessing callbacks.

        Args:
            event_type: The type of event
            data: The event payload
        """
        loop: asyncio.AbstractEventLoop | None = None
        with self._loop_lock:
            loop = self._loop

        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.publish(event_type, data), loop
            )
            return

        # Fallback: try to get the running loop from the current thread
        try:
            running_loop = asyncio.get_running_loop()
            # We're in an async context — schedule directly
            _ = asyncio.create_task(self.publish(event_type, data))
            return
        except RuntimeError:
            pass

        # No loop available — queue for later delivery
        with self._loop_lock:
            self._pending_queue.append((event_type, data))
        logger.info(
            "EventBus.publish_sync(): no event loop available, "
            "queuing event type=%s for later delivery. "
            "Call set_event_loop() to flush pending events.",
            event_type.value,
        )

    def subscribe(self, event_type: EventType | None, callback: Callback) -> None:
        """Subscribe to events of a specific type or all events.

        Args:
            event_type: The event type to subscribe to, or None for all events (wildcard)
            callback: The callback function to invoke (sync or async)

        Raises:
            TypeError: If callback is not callable
        """
        if not callable(callback):
            raise TypeError("callback must be callable")

        if event_type not in self._subscribers:
            self._subscribers[event_type] = []

        self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: EventType | None, callback: Callback) -> bool:
        """Unsubscribe a callback from an event type.

        Args:
            event_type: The event type to unsubscribe from, or None for wildcard
            callback: The callback to remove

        Returns:
            True if the callback was found and removed, False otherwise
        """
        if event_type not in self._subscribers:
            return False

        try:
            self._subscribers[event_type].remove(callback)
            return True
        except ValueError:
            return False

    async def publish(self, event_type: EventType, data: dict[str, Any]) -> None:
        """Publish an event to all subscribers.

        The event is queued and will be processed asynchronously by the event loop.

        Args:
            event_type: The type of event
            data: The event payload
        """
        self._published += 1
        event = Event(type=event_type, data=data)
        await self._queue.put(event)

    async def start(self) -> None:
        """Start the event processing loop.

        Creates an async task that continuously processes events from the queue.
        Safe to call multiple times - subsequent calls are ignored if already running.
        """
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._process_events())

    async def stop(self) -> None:
        """Gracefully stop the event processing loop.

        Waits for all queued events to be processed before returning.
        Safe to call even if not running.
        """
        if not self._running:
            return

        self._running = False

        # Signal the queue to stop by putting a sentinel object
        await self._queue.put(self._stop_sentinel)

        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                _ = self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            self._task = None

    @property
    def stats(self) -> dict[str, int]:
        """Return event bus statistics.

        Returns:
            Dict with keys: published (total events published),
            delivered (total successful subscriber deliveries),
            dropped (subscriber deliveries that raised exceptions),
            pending (events queued in _pending_queue awaiting loop).
        """
        return {
            "published": self._published,
            "delivered": self._delivered,
            "dropped": self._dropped,
            "pending": len(self._pending_queue),
        }

    async def _process_events(self) -> None:
        """Internal method to process events from the queue."""
        while True:
            try:
                item = await self._queue.get()

                # Check for stop signal first (sentinel object)
                if isinstance(item, _StopSentinel):
                    self._queue.task_done()
                    break

                # Item is an Event
                await self._dispatch_event(item)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception:
                # Log error but continue processing
                continue

    async def _dispatch_event(self, event: Event) -> None:
        """Dispatch an event to all relevant subscribers.

        Args:
            event: The event to dispatch
        """
        # Get specific subscribers for this event type
        specific_callbacks = self._subscribers.get(event.type, [])

        # Get wildcard subscribers (None key)
        wildcard_callbacks = self._subscribers.get(None, [])

        # Combine all callbacks to invoke
        all_callbacks = specific_callbacks + wildcard_callbacks

        for callback in all_callbacks:
            try:
                result = callback(event)
                # If callback is async, await it
                if inspect.isawaitable(result):
                    await result
                self._delivered += 1
            except Exception:
                cb_name = getattr(callback, "__name__", str(callback))
                logger.exception(
                    "EventBus: subscriber '%s' raised exception "
                    "while handling event type=%s",
                    cb_name,
                    event.type.value,
                )
                self._dropped += 1