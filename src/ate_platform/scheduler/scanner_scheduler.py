"""Scanner Scheduler for ATE Platform.

This module provides the core scanning scheduler that monitors
step conditions and notifies when steps become ready for execution.

Key features:
- Reactive dispatch: event handlers trigger immediate step evaluation
- Watchdog scan loop (5s default interval) as safety net only
- Subscribes to MEASUREMENT_RECORDED, STEP_STATUS_CHANGED, RESOURCE_RELEASED
- Detects ready steps via StepRegistry.get_ready_steps()
- Emits STEP_STARTED events when conditions are met
- Deadlock detection (cyclic scan without progress)
- Loop-aware scanning: YamlLoop steps are executed via LoopExecutor
- Dependency index for O(1) lookup of dependents
- Pending-dispatch deduplication prevents double-dispatch
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

# Sentinel for "no event loop available" in _schedule_dispatch
_NO_LOOP = object()


def _is_in_async_context() -> bool:
    """Check if we're currently inside an async event loop."""
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False

from ..types import Condition, LoopResult, StepStatus
from .condition_evaluator import ConditionEvaluator
from .event_bus import Event, EventBus, EventType
from .resource_manager import ResourceManager
from .step_registry import StepRegistry
from .variable_space import VariableSpace

if TYPE_CHECKING:
    from ..executor.step_executor import StepExecutor

logger = logging.getLogger(__name__)


class DeadlockDetectedError(Exception):
    """Raised when the scheduler detects a potential deadlock.

    A deadlock is detected when the scanner has scanned multiple times
    without any progress (no steps becoming ready and no steps executing).
    """
    pass


class ScannerScheduler:
    """Event-driven scanner that monitors step conditions and triggers execution.

    The ScannerScheduler is the core innovation of the ATE Platform scheduling
    system. It uses reactive dispatch — event handlers trigger immediate step
    evaluation when state changes — with a 5-second watchdog scan loop as a
    safety net for any events that might be missed.

    Architecture:
        - Reactive: on_step_status_changed → _evaluate_dependents → dispatch
        - Reactive: on_variable_changed → dispatch for variable-dependent steps
        - Reactive: on_resource_released → dispatch for resource-blocked steps
        - Watchdog: 5s scan loop runs _emergency_scan() only if no progress
        - Dedup: _pending_dispatch set prevents double-dispatch
        - Index: _dependency_index maps step/variable/resource → dependent steps

    Thread Safety:
        All async operations use asyncio primitives.
        Dependencies (StepRegistry, etc.) have their own thread safety.

    Example:
        >>> scheduler = ScannerScheduler(event_bus, registry, evaluator, variable_space, resource_manager)
        >>> await scheduler.start()
        >>> # ... steps are registered and executed ...
        >>> await scheduler.stop()
    """

    # Default scan interval in seconds (5s watchdog — reactive dispatch handles
    # the fast path; this is only a safety net)
    DEFAULT_SCAN_INTERVAL: float = 5.0

    # Deadlock detection threshold: max consecutive scans without progress
    DEADLOCK_THRESHOLD: int = 100

    def __init__(
        self,
        event_bus: EventBus,
        registry: StepRegistry,
        evaluator: ConditionEvaluator,
        variable_space: VariableSpace,
        resource_manager: ResourceManager,
        scan_interval: float = DEFAULT_SCAN_INTERVAL,
        step_executor: StepExecutor | None = None,
    ) -> None:
        """Initialize the scanner scheduler.

        Args:
            event_bus: EventBus for publishing STEP_READY events
            registry: StepRegistry for tracking step statuses
            evaluator: ConditionEvaluator for checking conditions
            variable_space: VariableSpace for variable resolution
            resource_manager: ResourceManager for resource availability
            scan_interval: Time between scans in seconds (default 100ms)
            step_executor: Optional StepExecutor for step execution.
                Defaults to ProcessStepExecutor if not provided.
        """
        self._event_bus: EventBus = event_bus
        self._registry: StepRegistry = registry
        self._evaluator: ConditionEvaluator = evaluator
        self._variable_space: VariableSpace = variable_space
        self._resource_manager: ResourceManager = resource_manager
        self._scan_interval: float = scan_interval

        # Step executor — defaults to ProcessStepExecutor
        if step_executor is not None:
            self._step_executor: StepExecutor = step_executor
        else:
            from ..executor.step_executor import ProcessStepExecutor

            self._step_executor = ProcessStepExecutor(
                max_workers=4,
                script_timeout=60.0,
                event_bus=event_bus,
            )

        # Runtime state
        self._running: bool = False
        self._scan_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

        # Deadlock detection
        self._consecutive_no_progress: int = 0
        self._last_ready_count: int = 0

        # Track steps that have already been notified as ready
        self._notified_ready: set[str] = set()

        # Pending dispatch deduplication: steps currently being dispatched
        # or scheduled for dispatch. Prevents double-dispatch from both
        # reactive handlers and the watchdog scan.
        self._pending_dispatch: set[str] = set()

        # Dependency index: maps source → set of step_ids that depend on it.
        # Built at compile_plan() time for O(1) reactive dispatch lookups.
        # Keys: step_id, variable_name (e.g. "scope.voltage"), resource_name
        self._dependency_index: dict[str, set[str]] = {}

        # Timestamp of last reactive dispatch — used by watchdog to detect
        # whether any progress happened since the last scan.
        self._last_dispatch_time: float = 0.0

        # Event handlers (stored for potential unsubscription)
        self._handlers: dict[EventType, Callable[[Event], None]] = {}

    async def start(self) -> None:
        """Start the scanning loop.

        Subscribes to relevant events and begins the scanning task.
        Safe to call multiple times - subsequent calls are ignored if already running.
        """
        if self._running:
            return

        self._running = True
        self._stop_event.clear()

        # Subscribe to events that might change step readiness
        self._setup_event_handlers()

        # Start the scanning task
        self._scan_task = asyncio.create_task(self._scan_loop())

        logger.info("ScannerScheduler started with interval %.3fs", self._scan_interval)

    async def stop(self) -> None:
        """Stop the scanning loop gracefully.

        Cancels the scanning task and waits for it to complete.
        Safe to call even if not running.
        """
        if not self._running:
            return

        self._running = False
        self._stop_event.set()

        # Unsubscribe from events
        self._teardown_event_handlers()

        if self._scan_task is not None:
            try:
                await asyncio.wait_for(self._scan_task, timeout=5.0)
            except TimeoutError:
                if self._scan_task is not None:
                    self._scan_task.cancel()
                    try:
                        await self._scan_task
                    except asyncio.CancelledError:
                        pass
            self._scan_task = None

        logger.info("ScannerScheduler stopped")

    def compile_plan(self, steps: list[tuple[str, Condition | None]]) -> None:
        """Build the dependency index from a list of (step_id, condition) pairs.

        Must be called after all steps are registered but before start().
        The index enables O(1) lookup of which steps depend on a given
        step, variable, or resource — powering reactive dispatch.

        Args:
            steps: List of (step_id, condition) tuples for all plan steps.
                condition may be None for unconditional steps.
        """
        self._dependency_index.clear()

        for step_id, condition in steps:
            if condition is None:
                continue

            # Step dependency: condition.step → this step
            if condition.step is not None:
                self._dependency_index.setdefault(condition.step, set()).add(step_id)

            # Variable dependency: parse ${...} references in expression
            if condition.expression is not None:
                import re
                for match in re.finditer(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)*)\}", condition.expression):
                    var_name = match.group(1)
                    self._dependency_index.setdefault(var_name, set()).add(step_id)

            # Resource dependency: resource_available → this step
            if condition.resource_available is not None:
                for resource_name in condition.resource_available:
                    self._dependency_index.setdefault(resource_name, set()).add(step_id)

        logger.debug(
            "Dependency index built: %d source keys, %d total edges",
            len(self._dependency_index),
            sum(len(v) for v in self._dependency_index.values()),
        )

    def _setup_event_handlers(self) -> None:
        """Set up event subscriptions with reactive dispatch wiring."""
        scheduler_self = self  # Capture for closures

        # Variable changed handler — triggers dispatch for variable-dependent steps
        def on_variable_changed(event: Event) -> None:
            name = event.data.get("name", "unknown")
            logger.debug("Measurement recorded: %s — evaluating dependents", name)
            scheduler_self._schedule_dispatch_for_key(name)

        # Step status changed handler — triggers _evaluate_dependents and dispatch
        def on_step_status_changed(event: Event) -> None:
            step_id = event.data.get("step_id")
            new_status = event.data.get("new_status")

            # If step completed, remove from notified_ready to allow re-scan
            if new_status in ("PASSED", "FAILED", "SKIPPED", "ERROR"):
                scheduler_self._notified_ready.discard(step_id)
                # Remove from pending_dispatch since it's done
                scheduler_self._pending_dispatch.discard(step_id)

            logger.debug("Step status changed: %s -> %s — evaluating dependents", step_id, new_status)
            # Trigger reactive dispatch for steps that depend on this step
            if step_id is not None:
                scheduler_self._schedule_dispatch_for_key(step_id)

        # Resource released handler — triggers dispatch for resource-blocked steps
        def on_resource_released(event: Event) -> None:
            resource_id = event.data.get("resource_id", "unknown")
            logger.debug("Resource released: %s — evaluating dependents", resource_id)
            scheduler_self._schedule_dispatch_for_key(resource_id)

        # Store handlers — MEASUREMENT_RECORDED replaces deprecated VARIABLE_CHANGED
        self._handlers[EventType.MEASUREMENT_RECORDED] = on_variable_changed
        self._handlers[EventType.STEP_STATUS_CHANGED] = on_step_status_changed
        self._handlers[EventType.RESOURCE_RELEASED] = on_resource_released

        # Subscribe
        for event_type, handler in self._handlers.items():
            self._event_bus.subscribe(event_type, handler)

    def _teardown_event_handlers(self) -> None:
        """Remove event subscriptions."""
        for event_type, handler in self._handlers.items():
            _ = self._event_bus.unsubscribe(event_type, handler)
        self._handlers.clear()

    async def _scan_loop(self) -> None:
        """Watchdog scanning loop (5s default interval).

        This is NOT the primary dispatch mechanism — reactive event handlers
        handle immediate dispatch. This loop runs as a safety net to catch
        any events that might have been missed or state changes not
        surfaced through the EventBus.

        Only runs _emergency_scan() if no progress has been detected
        since the last iteration (i.e., _last_dispatch_time hasn't changed).
        """
        import time

        while self._running and not self._stop_event.is_set():
            try:
                # Check if any reactive dispatch happened since last scan
                current_dispatch_time = self._last_dispatch_time
                await asyncio.sleep(0)  # Yield to let reactive handlers run

                if current_dispatch_time == self._last_dispatch_time and current_dispatch_time > 0:
                    # No progress since last scan — run emergency scan
                    await self._emergency_scan()
                elif current_dispatch_time == 0.0:
                    # First scan or no dispatch yet — do a full scan
                    await self._emergency_scan()
            except Exception as e:
                logger.error("Error during watchdog scan: %s", e)

            # Wait for next scan interval or stop signal
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._scan_interval
                )
                # If we reach here, stop_event was set
                break
            except TimeoutError:
                # Normal timeout, continue scanning
                pass

    async def _emergency_scan(self) -> None:
        """Emergency scan: full get_ready_steps() + dispatch.

        Called by the watchdog scan loop when no reactive progress has been
        detected. This is the fallback path for catching missed events.
        Also runs on the first iteration after start().
        """
        from shared.events import StepStartedData

        # Get all steps that are ready to execute
        ready_steps = self._registry.get_ready_steps(
            variable_space=self._variable_space,
            resource_manager=self._resource_manager,
        )

        # Check for progress (used in deadlock detection)
        if len(ready_steps) == self._last_ready_count:
            self._consecutive_no_progress += 1
        else:
            self._consecutive_no_progress = 0
            self._last_ready_count = len(ready_steps)

        # Deadlock detection
        if self._consecutive_no_progress >= self.DEADLOCK_THRESHOLD:
            await self._handle_potential_deadlock()
            return

        # Emit STEP_STARTED for newly ready steps (dedup via _pending_dispatch)
        for step_id in ready_steps:
            if step_id in self._notified_ready or step_id in self._pending_dispatch:
                continue

            # Get condition for the step (if any)
            condition = self._registry.get_condition(step_id)

            # Verify condition is actually met (double-check)
            if condition is not None and not self._check_condition(condition):
                continue

            # Mark as pending to prevent double-dispatch
            self._pending_dispatch.add(step_id)

            # Emit STEP_STARTED event with normalized schema
            event_data = asdict(StepStartedData(
                step_id=step_id,
                condition=str(condition) if condition else None,
            ))
            await self._event_bus.publish(
                EventType.STEP_STARTED,
                event_data,
            )

            # Mark as notified
            self._notified_ready.add(step_id)
            self._last_dispatch_time = __import__("time").time()

            logger.debug("Emergency scan dispatched: %s", step_id)

    def _schedule_dispatch_for_key(self, source_key: str) -> None:
        """Schedule reactive dispatch for steps depending on source_key.

        Called from event handlers (sync callbacks). Looks up the
        dependency index and schedules async dispatch for all dependent
        steps that are not already pending.

        Args:
            source_key: The step_id, variable name, or resource name
                that changed.
        """
        dependent_steps = self._dependency_index.get(source_key, set())
        if not dependent_steps:
            return

        for step_id in dependent_steps:
            # Skip if already dispatched or pending
            if step_id in self._notified_ready or step_id in self._pending_dispatch:
                continue

            self._pending_dispatch.add(step_id)

            # Schedule async dispatch — try to use the running event loop
            loop = self._get_event_loop()
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._dispatch_step(step_id), loop
                )
            elif _is_in_async_context():
                # We're in an async context — create a task
                _ = asyncio.create_task(self._dispatch_step(step_id))
            else:
                # No loop available — remove from pending, let watchdog catch it
                self._pending_dispatch.discard(step_id)
                logger.debug(
                    "No event loop for reactive dispatch of %s — deferring to watchdog",
                    step_id,
                )

    async def _dispatch_step(self, step_id: str) -> None:
        """Dispatch a single step via reactive path.

        Checks if the step is ready and emits STEP_STARTED if so.
        Removes from _pending_dispatch when done (success or failure).

        Uses _registry.get_ready_steps() rather than the stale
        _evaluator to check conditions. The registry builds a fresh
        ConditionEvaluator from current step statuses on each call.

        Args:
            step_id: The step to dispatch.
        """
        import time

        try:
            from shared.events import StepStartedData

            # Verify step is still pending (status might have changed)
            try:
                status = self._registry.get_status(step_id)
            except KeyError:
                return  # Step was unregistered

            if status.value != "PENDING":
                return  # No longer pending

            # Check if already notified (race with emergency scan)
            if step_id in self._notified_ready:
                return

            # Check if step is actually ready according to the registry.
            # The registry's get_ready_steps() builds a fresh ConditionEvaluator
            # from current step statuses — no stale evaluator risk.
            ready_steps = self._registry.get_ready_steps(
                variable_space=self._variable_space,
                resource_manager=self._resource_manager,
            )
            if step_id not in ready_steps:
                return  # Condition not met yet

            # Mark as notified
            self._notified_ready.add(step_id)
            self._last_dispatch_time = time.time()

            # Get condition for logging
            condition = self._registry.get_condition(step_id)

            # Emit STEP_STARTED event
            event_data = asdict(StepStartedData(
                step_id=step_id,
                condition=str(condition) if condition else None,
            ))
            await self._event_bus.publish(
                EventType.STEP_STARTED,
                event_data,
            )

            logger.debug("Reactive dispatch: %s", step_id)
        except Exception as e:
            logger.error("Reactive dispatch failed for %s: %s", step_id, e)
        finally:
            self._pending_dispatch.discard(step_id)

    def _get_event_loop(self) -> asyncio.AbstractEventLoop | None:
        """Get the event loop stored on the EventBus, if any.

        The EventBus stores a loop reference via set_event_loop().
        We use it for scheduling reactive dispatch from sync callbacks.
        """
        return getattr(self._event_bus, "_loop", None)

    def _check_condition(self, condition: Condition) -> bool:
        """Check if a condition is currently met.

        Args:
            condition: The condition to check

        Returns:
            True if the condition is met, False otherwise
        """
        try:
            return self._evaluator.evaluate(condition)
        except Exception:
            return False

    async def _handle_potential_deadlock(self) -> None:
        """Handle a potential deadlock situation.

        Logs a warning and emits a DEADLOCK_DETECTED alarm event with severity/recoverable.
        """
        from shared.events import DeadlockDetectedData

        all_steps = self._registry.get_all_steps()
        pending_steps = [
            sid for sid, status in all_steps.items()
            if status.value == "PENDING"
        ]

        logger.warning(
            "Potential deadlock detected: %d consecutive scans without progress. "
            "Pending steps: %s",
            self._consecutive_no_progress,
            pending_steps
        )

        # Reset the counter to allow continued operation
        self._consecutive_no_progress = 0

        # Emit a DEADLOCK_DETECTED alarm event with normalized schema
        event_data = asdict(DeadlockDetectedData(
            pending_steps=pending_steps,
            consecutive_scans=self._consecutive_no_progress,
            severity="critical",
            recoverable=False,
        ))
        await self._event_bus.publish(
            EventType.DEADLOCK_DETECTED,
            event_data,
        )

    def force_scan(self) -> None:
        """Force an immediate scan (non-blocking).

        Schedules an emergency scan on the event loop. Useful for
        triggering an immediate check after external state changes
        that don't go through the EventBus.
        """
        loop = self._get_event_loop()
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(self._emergency_scan(), loop)
        elif _is_in_async_context():
            _ = asyncio.create_task(self._emergency_scan())

    async def execute_loop_step(
        self,
        loop: Any,
        run_id: str | None = None,
    ) -> LoopResult:
        """Execute a YamlLoop step using LoopExecutor.

        When the scheduler encounters a YamlLoop step, this method creates
        a LoopExecutor and executes the loop. After completion, the loop
        step is marked as PASSED/FAILED in the StepRegistry and the result
        is stored in VariableSpace under loop.<loop_id>.result.

        Args:
            loop: The YamlLoop to execute
            run_id: Optional execution run identifier

        Returns:
            LoopResult with aggregated status and per-iteration results
        """
        from ..executor.loop_executor import LoopExecutor

        # Create a LoopExecutor with the scheduler's step executor
        loop_executor = LoopExecutor(
            executor=self._step_executor,
            event_bus=self._event_bus,
            variable_space=self._variable_space,
        )

        # Mark the loop step as RUNNING in the registry
        try:
            self._registry.update_status(loop.id, StepStatus.RUNNING)
        except KeyError:
            # Loop step not registered — register it first
            self._registry.register(loop.id)
            self._registry.update_status(loop.id, StepStatus.RUNNING)

        # Execute the loop
        result = await loop_executor.execute_loop(loop, run_id=run_id)

        # Update the registry with the loop's final status
        try:
            self._registry.update_status(loop.id, result.status)
        except KeyError:
            pass

        return result

    def get_status(self) -> dict[str, Any]:
        """Get current scheduler status for monitoring.

        Returns:
            Dictionary with status information
        """
        return {
            "running": self._running,
            "scan_interval": self._scan_interval,
            "consecutive_no_progress": self._consecutive_no_progress,
            "last_ready_count": self._last_ready_count,
            "notified_ready_count": len(self._notified_ready),
            "pending_dispatch_count": len(self._pending_dispatch),
            "dependency_index_size": len(self._dependency_index),
            "last_dispatch_time": self._last_dispatch_time,
        }
