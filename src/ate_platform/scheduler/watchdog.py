"""WatchDog health monitor for ATE Platform ScannerScheduler.

This module provides an independent asyncio task that monitors the
_scan_loop heartbeat counter to detect scheduler freezes and deadlocks.

Key features:
- Heartbeat monitoring: checks if heartbeat_counter increments each interval
- Heartbeat lost detection: 3 consecutive missed heartbeats → CRITICAL alarm
- Deadlock detection: 100 consecutive checks with no progress → DEADLOCK alarm
- Emergency shutdown: calls emergency_shutdown_callback on heartbeat loss
- Clean start/stop lifecycle: asyncio task created on start, cancelled on stop
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .event_bus import EventBus

# Import event types at module level (not TYPE_CHECKING — used at runtime)
from shared.events import (  # noqa: E402
    DeadlockDetectedData,
    EventType,
    HeartbeatLostData,
)

logger = logging.getLogger(__name__)


class WatchDog:
    """Independent asyncio health monitor for the scanner scheduler.

    The WatchDog runs in its own asyncio task, periodically checking
    a shared heartbeat counter. If the counter stops incrementing,
    it assumes the scan loop is frozen and triggers alarms + shutdown.

    Architecture:
        - Polls heartbeat_counter every scan_interval seconds
        - Tracks consecutive missed increments
        - 3 misses → HEARTBEAT_LOST alarm + emergency_shutdown_callback()
        - 100 consecutive no-progress checks → DEADLOCK_DETECTED alarm
        - Completely in-process (no external dependencies)

    Example:
        >>> watchdog = WatchDog(
        ...     heartbeat_counter=lambda: scheduler._heartbeat,
        ...     scan_interval=5.0,
        ...     event_bus=event_bus,
        ...     emergency_shutdown_callback=scheduler._handle_emergency_shutdown,
        ... )
        >>> watchdog.start()
        >>> # ... scheduler runs ...
        >>> await watchdog.stop()
    """

    # Number of consecutive missed heartbeats before HEARTBEAT_LOST alarm
    HEARTBEAT_LOST_THRESHOLD: int = 3

    # Number of consecutive no-progress checks before DEADLOCK_DETECTED alarm
    DEADLOCK_THRESHOLD: int = 100

    def __init__(
        self,
        heartbeat_counter: "callable[[], int]",
        scan_interval: float = 5.0,
        event_bus: "EventBus | None" = None,
        emergency_shutdown_callback: "callable[[], object] | None" = None,
    ) -> None:
        """Initialize the WatchDog.

        Args:
            heartbeat_counter: Callable that returns the current heartbeat value.
                Called on each watchdog check iteration.
            scan_interval: Seconds between watchdog checks (default 5.0).
            event_bus: Optional EventBus for publishing alarm events.
            emergency_shutdown_callback: Optional async or sync callable
                invoked on heartbeat loss. Called with no arguments.
        """
        self._heartbeat_counter = heartbeat_counter
        self._scan_interval = scan_interval
        self._event_bus = event_bus
        self._emergency_shutdown_callback = emergency_shutdown_callback

        # Runtime state
        self._running: bool = False
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

        # Heartbeat tracking
        self._last_heartbeat: int = 0
        self._consecutive_misses: int = 0

        # Deadlock tracking (no-progress across watchdog checks)
        self._consecutive_no_progress: int = 0
        self._last_progress_heartbeat: int = 0

    def start(self) -> None:
        """Start the watchdog monitoring task.

        Creates an independent asyncio task that loops every
        scan_interval seconds checking the heartbeat counter.

        Safe to call multiple times — subsequent calls are ignored
        if already running.
        """
        if self._running:
            return

        self._running = True
        self._stop_event.clear()

        # Snapshot the current heartbeat so we don't alarm immediately
        self._last_heartbeat = self._heartbeat_counter()
        self._last_progress_heartbeat = self._last_heartbeat
        self._consecutive_misses = 0
        self._consecutive_no_progress = 0

        self._task = asyncio.create_task(self._watchdog_loop())

        logger.info(
            "WatchDog started with interval %.3fs (heartbeat_lost=%d, deadlock=%d)",
            self._scan_interval,
            self.HEARTBEAT_LOST_THRESHOLD,
            self.DEADLOCK_THRESHOLD,
        )

    async def stop(self) -> None:
        """Stop the watchdog monitoring task.

        Cancels the watchdog task and waits for it to complete.
        Safe to call even if not running.
        """
        if not self._running:
            return

        self._running = False
        self._stop_event.set()

        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                if self._task is not None:
                    self._task.cancel()
                    try:
                        await self._task
                    except asyncio.CancelledError:
                        pass
            self._task = None

        logger.info("WatchDog stopped")

    async def _watchdog_loop(self) -> None:
        """Main watchdog monitoring loop.

        Runs every scan_interval seconds. On each iteration:
        1. Reads the current heartbeat counter
        2. If unchanged → increment consecutive_misses
        3. If 3 consecutive misses → HEARTBEAT_LOST alarm + emergency shutdown
        4. If 100 consecutive no-progress → DEADLOCK_DETECTED alarm
        5. If changed → reset miss counters, update last_heartbeat
        """
        while self._running and not self._stop_event.is_set():
            try:
                current_heartbeat = self._heartbeat_counter()

                if current_heartbeat == self._last_heartbeat:
                    # Heartbeat has NOT incremented — potential freeze
                    self._consecutive_misses += 1
                    self._consecutive_no_progress += 1

                    # Check heartbeat lost threshold (3 consecutive misses)
                    if self._consecutive_misses >= self.HEARTBEAT_LOST_THRESHOLD:
                        await self._handle_heartbeat_lost()

                    # Check deadlock threshold (100 consecutive no-progress)
                    if self._consecutive_no_progress >= self.DEADLOCK_THRESHOLD:
                        await self._handle_deadlock()
                else:
                    # Heartbeat incremented — scanner is alive
                    if self._consecutive_misses > 0:
                        logger.debug(
                            "WatchDog: heartbeat recovered after %d misses",
                            self._consecutive_misses,
                        )
                    self._consecutive_misses = 0
                    self._consecutive_no_progress = 0
                    self._last_heartbeat = current_heartbeat
                    self._last_progress_heartbeat = current_heartbeat
            except Exception as e:
                logger.error("WatchDog check error: %s", e)

            # Wait for next interval or stop signal
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._scan_interval,
                )
                # stop_event was set — exit loop
                break
            except TimeoutError:
                # Normal timeout — continue to next check
                pass

    async def _handle_heartbeat_lost(self) -> None:
        """Handle heartbeat lost alarm.

        Logs CRITICAL, publishes HEARTBEAT_LOST alarm event, and calls
        the emergency shutdown callback (if configured).
        """
        logger.critical(
            "WatchDog: HEARTBEAT LOST — scan loop frozen after %d missed checks "
            "(last heartbeat: %d, interval: %.1fs)",
            self._consecutive_misses,
            self._last_heartbeat,
            self._scan_interval,
        )

        # Publish HEARTBEAT_LOST alarm event
        if self._event_bus is not None:
            event_data = asdict(HeartbeatLostData(
                last_heartbeat=self._last_heartbeat,
                missed_checks=self._consecutive_misses,
                scan_interval=self._scan_interval,
                severity="critical",
                recoverable=False,
            ))
            await self._event_bus.publish(
                EventType.HEARTBEAT_LOST,
                event_data,
            )

        # Call emergency shutdown callback
        if self._emergency_shutdown_callback is not None:
            try:
                result = self._emergency_shutdown_callback()
                # If the callback is a coroutine, await it
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.exception(
                    "WatchDog: emergency shutdown callback failed: %s", e,
                )

    async def _handle_deadlock(self) -> None:
        """Handle deadlock detection alarm.

        Published DEADLOCK_DETECTED alarm event when 100 consecutive
        watchdog checks show no progress (heartbeat counter unchanged).
        """
        logger.warning(
            "WatchDog: DEADLOCK DETECTED — %d consecutive checks with no progress "
            "(last progress heartbeat: %d)",
            self._consecutive_no_progress,
            self._last_progress_heartbeat,
        )

        # Publish DEADLOCK_DETECTED alarm event
        if self._event_bus is not None:
            event_data = asdict(DeadlockDetectedData(
                pending_steps=[],
                consecutive_scans=self._consecutive_no_progress,
                severity="critical",
                recoverable=False,
            ))
            await self._event_bus.publish(
                EventType.DEADLOCK_DETECTED,
                event_data,
            )

        # Reset deadlock counter to allow continued monitoring
        self._consecutive_no_progress = 0
        self._last_progress_heartbeat = self._heartbeat_counter()

    @property
    def is_running(self) -> bool:
        """Whether the watchdog task is currently running."""
        return self._running

    @property
    def consecutive_misses(self) -> int:
        """Number of consecutive missed heartbeat checks."""
        return self._consecutive_misses

    @property
    def consecutive_no_progress(self) -> int:
        """Number of consecutive checks with no heartbeat progress."""
        return self._consecutive_no_progress