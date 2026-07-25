"""Scanner Scheduler for ATE Platform.

This module provides the core scanning scheduler that monitors
step conditions and notifies when steps become ready for execution.

Key features:
- Event-driven scanning loop (100ms default interval)
- Subscribes to VARIABLE_CHANGED, STEP_STATUS_CHANGED, RESOURCE_RELEASED
- Detects ready steps via StepRegistry.get_ready_steps()
- Emits STEP_STARTED events when conditions are met
- Deadlock detection (cyclic scan without progress)
- Loop-aware scanning: YamlLoop steps are executed via LoopExecutor
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

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
    system. It continuously scans registered steps to detect when their
    conditions are met, then emits STEP_READY events for the Executor.

    Architecture:
        - Subscribes to: VARIABLE_CHANGED, STEP_STATUS_CHANGED, RESOURCE_RELEASED
        - Uses: StepRegistry for state, ConditionEvaluator for checking
        - Emits: STEP_READY event when a step's conditions are met

    Thread Safety:
        All async operations use asyncio primitives.
        Dependencies (StepRegistry, etc.) have their own thread safety.

    Example:
        >>> scheduler = ScannerScheduler(event_bus, registry, evaluator, variable_space, resource_manager)
        >>> await scheduler.start()
        >>> # ... steps are registered and executed ...
        >>> await scheduler.stop()
    """

    # Default scan interval in seconds (100ms)
    DEFAULT_SCAN_INTERVAL: float = 0.1

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

    def _setup_event_handlers(self) -> None:
        """Set up event subscriptions."""
        # Variable changed handler
        def on_variable_changed(event: Event) -> None:
            # Variable change might affect conditions - will be picked up on next scan
            name = event.data.get("name", "unknown")
            logger.debug("Variable changed: %s", name)

        # Step status changed handler
        def on_step_status_changed(event: Event) -> None:
            step_id = event.data.get("step_id")
            new_status = event.data.get("new_status")

            # If step completed, remove from notified_ready to allow re-scan
            # (though typically steps aren't re-executed)
            if new_status in ("PASSED", "FAILED", "SKIPPED", "ERROR"):
                self._notified_ready.discard(step_id)

            logger.debug("Step status changed: %s -> %s", step_id, new_status)

        # Resource released handler
        def on_resource_released(event: Event) -> None:
            # Resource release might unblock waiting steps
            resource_id = event.data.get("resource_id", "unknown")
            logger.debug("Resource released: %s", resource_id)

        # Store handlers
        self._handlers[EventType.VARIABLE_CHANGED] = on_variable_changed
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
        """Main scanning loop.

        Continuously scans step conditions at regular intervals until stopped.
        """
        while self._running and not self._stop_event.is_set():
            try:
                await self._scan()
            except Exception as e:
                logger.error("Error during scan: %s", e)

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

    async def _scan(self) -> None:
        """Perform a single scan of all step conditions.

        Checks each pending step to see if its conditions are met,
        and emits STEP_STARTED events for newly ready steps.
        """
        from shared.events import StepStartedData

        # Get all steps that are ready to execute
        ready_steps = self._registry.get_ready_steps()

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

        # Emit STEP_STARTED for newly ready steps
        newly_ready = [s for s in ready_steps if s not in self._notified_ready]

        for step_id in newly_ready:
            # Get condition for the step (if any)
            condition = self._registry.get_condition(step_id)

            # Verify condition is actually met (double-check)
            if condition is not None and not self._check_condition(condition):
                continue

            # Mark as notified to avoid duplicate events
            self._notified_ready.add(step_id)

            # Emit STEP_STARTED event with normalized schema
            event_data = asdict(StepStartedData(
                step_id=step_id,
                condition=str(condition) if condition else None,
            ))
            await self._event_bus.publish(
                EventType.STEP_STARTED,
                event_data,
            )

            logger.debug("Step ready: %s", step_id)

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

        Logs a warning and emits an EXTERNAL_CMD event with deadlock details.
        """
        from shared.events import ExternalCmdData

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

        # Emit a deadlock event with normalized schema
        event_data = asdict(ExternalCmdData(
            command="DEADLOCK_DETECTED",
            payload={
                "pending_steps": pending_steps,
                "consecutive_scans": self._consecutive_no_progress,
            },
        ))
        await self._event_bus.publish(
            EventType.EXTERNAL_CMD,
            event_data,
        )

    def force_scan(self) -> None:
        """Force an immediate scan (non-blocking).

        Useful for triggering an immediate check after external state changes.
        """
        # The scan loop will naturally pick up changes on next iteration
        # This is a no-op that could be enhanced with an explicit trigger
        pass

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
        }
