"""Event bus for asynchronous event-driven communication in ATE Platform.

This module provides a publish-subscribe event system:
- EventType: Enum of supported event types
- Event: Data container for event messages
- EventBus: Async event bus with pub/sub support and wildcard subscriptions
"""

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EventType(Enum):
    """Enumeration of supported event types in the ATE Platform.

    Attributes:
        STEP_STATUS_CHANGED: A step's execution status has changed
        VARIABLE_CHANGED: A test variable has been modified
        RESOURCE_RELEASED: A resource has been released
        TIMER_EXPIRED: A timer has expired
        EXTERNAL_CMD: An external command has been received
    """

    STEP_STATUS_CHANGED = "STEP_STATUS_CHANGED"
    VARIABLE_CHANGED = "VARIABLE_CHANGED"
    RESOURCE_RELEASED = "RESOURCE_RELEASED"
    TIMER_EXPIRED = "TIMER_EXPIRED"
    EXTERNAL_CMD = "EXTERNAL_CMD"


@dataclass
class Event:
    """Container for event data.

    Attributes:
        type: The type of event
        data: The event payload
        timestamp: When the event was created (auto-generated if not provided)
    """

    type: EventType
    data: dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)


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

    Example:
        ```python
        bus = EventBus()

        # Subscribe to specific event type
        bus.subscribe(EventType.STEP_STATUS_CHANGED, my_handler)

        # Subscribe to all events (wildcard)
        bus.subscribe(None, my_logger)

        # Publish an event
        await bus.publish(EventType.STEP_STATUS_CHANGED, {"step": "test"})

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
            except Exception:
                # Log error but continue with other callbacks
                continue
