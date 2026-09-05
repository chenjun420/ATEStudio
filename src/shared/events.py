"""Event types for ATE Platform.

This module defines event-related types aligned with TEMS A4 categories:
- EventCategory: TEMS A4 classification (EVENT, MEASUREMENT, ALARM)
- EventType: Enum of supported event types, each mapped to a category
- Event: Data container for event messages with category field
- Typed event data classes for each EventType
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal


class EventCategory(Enum):
    """TEMS A4-aligned event classification.

    Every EventType belongs to exactly one category:
    - EVENT: Step lifecycle events (started, completed, failed, skipped, etc.)
    - MEASUREMENT: Instrument readings and variable recordings
    - ALARM: Timeout, deadlock, and resource exhaustion conditions
    """

    EVENT = "event"
    MEASUREMENT = "measurement"
    ALARM = "alarm"


class EventType(Enum):
    """Enumeration of supported event types in the ATE Platform.

    Each event type is mapped to an EventCategory via EVENT_TYPE_CATEGORIES.

    Attributes:
        STEP_STATUS_CHANGED: A step's execution status has changed (EVENT)
        STEP_STARTED: A step has started execution (EVENT)
        STEP_COMPLETED: A step has completed execution (EVENT)
        STEP_FAILED: A step has failed execution (EVENT)
        STEP_SKIPPED: A step was skipped (EVENT)
        STEP_TIMEOUT: A step timed out (ALARM)
        MEASUREMENT_RECORDED: A measurement/variable has been recorded (MEASUREMENT)
        VARIABLE_CHANGED: Deprecated alias for MEASUREMENT_RECORDED (MEASUREMENT)
        RESOURCE_RELEASED: A resource has been released (EVENT)
        RESOURCE_TIMEOUT: A resource acquisition timed out (ALARM)
        CONDITION_TIMEOUT: A condition wait timed out (ALARM)
        TIMER_EXPIRED: A timer has expired (EVENT)
        EXTERNAL_CMD: An external command has been received (EVENT)
        LOOP_ITERATION_STARTED: A loop iteration has started (EVENT)
        LOOP_ITERATION_COMPLETED: A loop iteration has completed (EVENT)
        EXECUTION_STARTED: Plan execution has started (EVENT)
        EXECUTION_COMPLETED: Plan execution has completed (EVENT)
        EXECUTION_PAUSED: Plan execution has been paused (EVENT)
        BREAKPOINT_HIT: An edge-evaluated breakpoint suspended the run (EVENT)
        DEADLOCK_DETECTED: A deadlock was detected (ALARM)
        WORKER_EXHAUSTED: A worker pool is exhausted (ALARM)
        HEARTBEAT_LOST: The scan loop heartbeat was lost (ALARM)
    """

    # EVENT category — step lifecycle
    STEP_STATUS_CHANGED = "STEP_STATUS_CHANGED"
    STEP_STARTED = "STEP_STARTED"
    STEP_COMPLETED = "STEP_COMPLETED"
    STEP_FAILED = "STEP_FAILED"
    STEP_SKIPPED = "STEP_SKIPPED"
    LOOP_ITERATION_STARTED = "LOOP_ITERATION_STARTED"
    LOOP_ITERATION_COMPLETED = "LOOP_ITERATION_COMPLETED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    EXECUTION_PAUSED = "EXECUTION_PAUSED"
    BREAKPOINT_HIT = "BREAKPOINT_HIT"
    RESOURCE_RELEASED = "RESOURCE_RELEASED"
    TIMER_EXPIRED = "TIMER_EXPIRED"
    EXTERNAL_CMD = "EXTERNAL_CMD"

    # MEASUREMENT category — instrument readings / variable recordings
    MEASUREMENT_RECORDED = "measurement_recorded"
    VARIABLE_CHANGED = "measurement_recorded"  # Deprecated alias — same wire value

    # ALARM category — timeout / exhaustion / deadlock
    STEP_TIMEOUT = "STEP_TIMEOUT"
    CONDITION_TIMEOUT = "CONDITION_TIMEOUT"
    RESOURCE_TIMEOUT = "RESOURCE_TIMEOUT"
    DEADLOCK_DETECTED = "DEADLOCK_DETECTED"
    WORKER_EXHAUSTED = "WORKER_EXHAUSTED"
    HEARTBEAT_LOST = "HEARTBEAT_LOST"
    ALARM_RAISED = "ALARM_RAISED"

    @classmethod
    def _missing_(cls, value: object) -> EventType | None:
        """Handle VARIABLE_CHANGED deprecation when accessed by string value.

        When code looks up EventType("measurement_recorded"), Python returns
        the first member with that value — MEASUREMENT_RECORDED. This is correct.
        The VARIABLE_CHANGED alias exists only for source-level backward compat.
        """
        return None


# Category mapping: every EventType → exactly one EventCategory
EVENT_TYPE_CATEGORIES: dict[EventType, EventCategory] = {
    # EVENT category
    EventType.STEP_STATUS_CHANGED: EventCategory.EVENT,
    EventType.STEP_STARTED: EventCategory.EVENT,
    EventType.STEP_COMPLETED: EventCategory.EVENT,
    EventType.STEP_FAILED: EventCategory.EVENT,
    EventType.STEP_SKIPPED: EventCategory.EVENT,
    EventType.LOOP_ITERATION_STARTED: EventCategory.EVENT,
    EventType.LOOP_ITERATION_COMPLETED: EventCategory.EVENT,
    EventType.EXECUTION_STARTED: EventCategory.EVENT,
    EventType.EXECUTION_COMPLETED: EventCategory.EVENT,
    EventType.EXECUTION_PAUSED: EventCategory.EVENT,
    EventType.BREAKPOINT_HIT: EventCategory.EVENT,
    EventType.RESOURCE_RELEASED: EventCategory.EVENT,
    EventType.TIMER_EXPIRED: EventCategory.EVENT,
    EventType.EXTERNAL_CMD: EventCategory.EVENT,
    # MEASUREMENT category
    EventType.MEASUREMENT_RECORDED: EventCategory.MEASUREMENT,
    EventType.VARIABLE_CHANGED: EventCategory.MEASUREMENT,
    # ALARM category
    EventType.STEP_TIMEOUT: EventCategory.ALARM,
    EventType.CONDITION_TIMEOUT: EventCategory.ALARM,
    EventType.RESOURCE_TIMEOUT: EventCategory.ALARM,
    EventType.DEADLOCK_DETECTED: EventCategory.ALARM,
    EventType.WORKER_EXHAUSTED: EventCategory.ALARM,
    EventType.HEARTBEAT_LOST: EventCategory.ALARM,
    EventType.ALARM_RAISED: EventCategory.ALARM,
}


def get_event_category(event_type: EventType) -> EventCategory:
    """Get the TEMS A4 category for an event type.

    Args:
        event_type: The event type to look up.

    Returns:
        The EventCategory for this event type.

    Raises:
        KeyError: If the event type has no category mapping.
    """
    return EVENT_TYPE_CATEGORIES[event_type]


def _warn_variable_changed_deprecated() -> None:
    """Emit a DeprecationWarning for VARIABLE_CHANGED usage."""
    warnings.warn(
        "EventType.VARIABLE_CHANGED is deprecated — use EventType.MEASUREMENT_RECORDED instead. "
        "VARIABLE_CHANGED will be removed in a future release.",
        DeprecationWarning,
        stacklevel=3,
    )


@dataclass
class Event:
    """Container for event data with TEMS A4 category.

    Attributes:
        type: The type of event
        category: TEMS A4 category (auto-derived from type if not provided)
        data: The event payload
        timestamp: When the event was created (auto-generated if not provided)
    """

    type: EventType
    data: dict[str, Any]
    category: EventCategory = field(default=None)  # type: ignore[assignment]
    timestamp: datetime = field(default_factory=datetime.now)

    def __post_init__(self) -> None:
        """Auto-derive category from event type if not explicitly set."""
        if self.category is None:
            self.category = get_event_category(self.type)


# ---------------------------------------------------------------------------
# Typed event data classes — one per EventType
# ---------------------------------------------------------------------------


@dataclass
class StepStatusChangedData:
    """Data for STEP_STATUS_CHANGED events.

    Attributes:
        step_id: The step whose status changed
        old_status: Previous status value string
        new_status: New status value string
        run_id: Execution run identifier
    """

    step_id: str
    old_status: str
    new_status: str
    run_id: str | None = None


@dataclass
class StepStartedData:
    """Data for STEP_STARTED events.

    Attributes:
        step_id: The step that started
        condition: Optional condition that triggered the step
        run_id: Execution run identifier
    """

    step_id: str
    condition: str | None = None
    run_id: str | None = None


@dataclass
class StepCompletedData:
    """Data for STEP_COMPLETED events.

    Attributes:
        step_id: The step that completed
        status: Final status value string
        error: Error message if step failed
        run_id: Execution run identifier
    """

    step_id: str
    status: str = "PASSED"
    error: str | None = None
    run_id: str | None = None


@dataclass
class StepFailedData:
    """Data for STEP_FAILED events.

    Attributes:
        step_id: The step that failed
        error: Error message describing the failure
        run_id: Execution run identifier
    """

    step_id: str
    error: str | None = None
    run_id: str | None = None


@dataclass
class StepSkippedData:
    """Data for STEP_SKIPPED events.

    Attributes:
        step_id: The step that was skipped
        reason: Optional reason for skipping
        run_id: Execution run identifier
    """

    step_id: str
    reason: str | None = None
    run_id: str | None = None


@dataclass
class MeasurementRecordedData:
    """Data for MEASUREMENT_RECORDED events (TEMS A4 measurement category).

    Replaces VariableChangedData with additional instrument metadata.

    Attributes:
        name: Variable name with scope prefix (e.g. 'scope.voltage')
        old_value: Previous value (None if variable was created)
        new_value: New value
        timestamp: Measurement timestamp as Unix epoch float
        unit: Measurement unit (e.g. 'V', 'A', 'Ω') or None
        instrument_id: Instrument that produced the measurement (e.g. 'DMM_CH1') or None
        run_id: Execution run identifier
    """

    name: str
    old_value: Any = None
    new_value: Any = None
    timestamp: float = 0.0
    unit: str | None = None
    instrument_id: str | None = None
    run_id: str | None = None


# Backward-compatible alias — code referencing VariableChangedData still works
VariableChangedData = MeasurementRecordedData


@dataclass
class ResourceReleasedData:
    """Data for RESOURCE_RELEASED events.

    Attributes:
        resource_id: The resource that was released
        owner_id: The owner that held the resource
        run_id: Execution run identifier
    """

    resource_id: str
    owner_id: str
    run_id: str | None = None


@dataclass
class TimerExpiredData:
    """Data for TIMER_EXPIRED events.

    Attributes:
        timer_id: The timer that expired
        duration: Timer duration in seconds
        run_id: Execution run identifier
    """

    timer_id: str
    duration: float = 0.0
    run_id: str | None = None


@dataclass
class ExternalCmdData:
    """Data for EXTERNAL_CMD events.

    Attributes:
        command: The command name or type
        payload: Command-specific payload
        run_id: Execution run identifier
    """

    command: str
    payload: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None


@dataclass
class LoopIterationStartedData:
    """Data for LOOP_ITERATION_STARTED events.

    Attributes:
        loop_id: The loop identifier
        iteration: Current iteration number (0-based)
        total_iterations: Total number of iterations (None if unknown)
        run_id: Execution run identifier
    """

    loop_id: str
    iteration: int = 0
    total_iterations: int | None = None
    run_id: str | None = None


@dataclass
class LoopIterationCompletedData:
    """Data for LOOP_ITERATION_COMPLETED events.

    Attributes:
        loop_id: The loop identifier
        iteration: Completed iteration number
        total_iterations: Total number of iterations (None if unknown)
        run_id: Execution run identifier
    """

    loop_id: str
    iteration: int = 0
    total_iterations: int | None = None
    run_id: str | None = None


@dataclass
class ExecutionStartedData:
    """Data for EXECUTION_STARTED events.

    Attributes:
        run_id: Execution run identifier
        plan_name: Name of the plan being executed
        sequence_id: Sequence identifier
    """

    run_id: str
    plan_name: str | None = None
    sequence_id: str | None = None


@dataclass
class ExecutionCompletedData:
    """Data for EXECUTION_COMPLETED events.

    Attributes:
        run_id: Execution run identifier
        plan_name: Name of the plan that was executed
        status: Final execution status
        duration_seconds: Total execution duration
    """

    run_id: str
    plan_name: str | None = None
    status: str = "COMPLETED"
    duration_seconds: float = 0.0


@dataclass
class ExecutionPausedData:
    """Data for EXECUTION_PAUSED events.

    Attributes:
        run_id: Execution run identifier
        reason: Optional reason for the pause
    """

    run_id: str
    reason: str | None = None


@dataclass
class BreakpointHitData:
    """Data for BREAKPOINT_HIT events (edge-evaluated breakpoints, T39/T40).

    Emitted by the edge scheduler when an armed breakpoint suspends a run,
    carrying the variable/step snapshot captured at the hit for inspection.

    Attributes:
        breakpoint_id: Identifier of the breakpoint that fired.
        kind: Breakpoint kind (step / instrument_call / variable_change /
            condition).
        target: Match target (step id / resource.method / scope.key / "*").
        step_id: The step about to dispatch when the breakpoint fired.
        variables: Snapshot of the current variable space for inspection.
        run_id: Execution run identifier.
    """

    breakpoint_id: str
    kind: str
    target: str
    step_id: str
    variables: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None


# ---------------------------------------------------------------------------
# Alarm data classes — severity + recoverable fields
# ---------------------------------------------------------------------------


@dataclass
class StepTimeoutData:
    """Data for STEP_TIMEOUT alarm events.

    Attributes:
        step_id: The step that timed out
        timeout_seconds: Configured timeout duration
        severity: Alarm severity level
        recoverable: Whether the condition is recoverable
        run_id: Execution run identifier
    """

    step_id: str
    timeout_seconds: float = 0.0
    severity: Literal["warning", "critical"] = "critical"
    recoverable: bool = False
    run_id: str | None = None


@dataclass
class ConditionTimeoutData:
    """Data for CONDITION_TIMEOUT alarm events.

    Attributes:
        step_id: The step whose condition timed out
        condition: The condition expression that was not met
        timeout_seconds: How long the condition was waited for
        severity: Alarm severity level
        recoverable: Whether the condition is recoverable
        run_id: Execution run identifier
    """

    step_id: str
    condition: str | None = None
    timeout_seconds: float = 0.0
    severity: Literal["warning", "critical"] = "warning"
    recoverable: bool = True
    run_id: str | None = None


@dataclass
class ResourceTimeoutData:
    """Data for RESOURCE_TIMEOUT alarm events.

    Attributes:
        resource_id: The resource that could not be acquired
        owner_id: The owner that held the resource (if known)
        timeout_seconds: How long the acquisition was attempted
        severity: Alarm severity level
        recoverable: Whether the condition is recoverable
        run_id: Execution run identifier
    """

    resource_id: str
    owner_id: str | None = None
    timeout_seconds: float = 0.0
    severity: Literal["warning", "critical"] = "warning"
    recoverable: bool = True
    run_id: str | None = None


@dataclass
class DeadlockDetectedData:
    """Data for DEADLOCK_DETECTED alarm events.

    Attributes:
        pending_steps: List of step IDs that are still pending
        consecutive_scans: Number of consecutive scans without progress
        severity: Alarm severity level
        recoverable: Whether the condition is recoverable
        run_id: Execution run identifier
    """

    pending_steps: list[str] = field(default_factory=list)
    consecutive_scans: int = 0
    severity: Literal["warning", "critical"] = "critical"
    recoverable: bool = False
    run_id: str | None = None


@dataclass
class WorkerExhaustedData:
    """Data for WORKER_EXHAUSTED alarm events.

    Attributes:
        pool_name: Name of the exhausted worker pool
        active_workers: Number of currently active workers
        max_workers: Maximum worker pool size
        severity: Alarm severity level
        recoverable: Whether the condition is recoverable
        deadlock_risk: Whether deadlock risk was detected (blocked resources
            are held by currently-running workers)
        blocked_resources: Resources the step needs but are held by others
        holding_workers: step_ids of workers that hold the blocked resources
        run_id: Execution run identifier
    """

    pool_name: str = "default"
    active_workers: int = 0
    max_workers: int = 0
    severity: Literal["warning", "critical"] = "warning"
    recoverable: bool = True
    deadlock_risk: bool = False
    blocked_resources: list[str] = field(default_factory=list)
    holding_workers: list[str] = field(default_factory=list)
    run_id: str | None = None


@dataclass
class HeartbeatLostData:
    """Data for HEARTBEAT_LOST alarm events.

    Emitted when the WatchDog detects that the scan loop has stopped
    incrementing its heartbeat counter — indicating the scheduler's
    main loop is frozen or stuck.

    Attributes:
        last_heartbeat: The heartbeat counter value when lost was detected
        missed_checks: Number of consecutive missed heartbeat checks
        scan_interval: The configured scan_interval of the watchdog
        severity: Alarm severity level
        recoverable: Whether the condition is recoverable
        run_id: Execution run identifier
    """

    last_heartbeat: int = 0
    missed_checks: int = 0
    scan_interval: float = 0.0
    severity: Literal["warning", "critical"] = "critical"
    recoverable: bool = False
    run_id: str | None = None


@dataclass
class AlarmRaisedData:
    """Data for ALARM_RAISED alarm events.

    Emitted when an SPC processor raises a process-control alert (e.g.
    Ppk below threshold or a Western Electric rule violation) and forwards
    it to the failure index.

    Attributes:
        alarm_id: Unique identifier for the raised alarm.
        product_type: Product type the alarm applies to.
        measurement_name: Measurement name that triggered the alarm.
        rule: Rule that fired (e.g. 'Ppk_below_1.00', 'WE1_beyond_3sigma').
        severity: 'warning' or 'critical'.
        message: Human-readable description.
        value: The measured value that triggered the alarm.
        sample_count: Number of samples seen when the alarm fired.
    """

    alarm_id: str = ""
    product_type: str = ""
    measurement_name: str = ""
    rule: str = ""
    severity: Literal["warning", "critical"] = "warning"
    message: str = ""
    value: float | None = None
    sample_count: int = 0


# Mapping from EventType to its typed data class
EVENT_DATA_CLASSES: dict[EventType, type] = {
    # EVENT category
    EventType.STEP_STATUS_CHANGED: StepStatusChangedData,
    EventType.STEP_STARTED: StepStartedData,
    EventType.STEP_COMPLETED: StepCompletedData,
    EventType.STEP_FAILED: StepFailedData,
    EventType.STEP_SKIPPED: StepSkippedData,
    EventType.RESOURCE_RELEASED: ResourceReleasedData,
    EventType.TIMER_EXPIRED: TimerExpiredData,
    EventType.EXTERNAL_CMD: ExternalCmdData,
    EventType.LOOP_ITERATION_STARTED: LoopIterationStartedData,
    EventType.LOOP_ITERATION_COMPLETED: LoopIterationCompletedData,
    EventType.EXECUTION_STARTED: ExecutionStartedData,
    EventType.EXECUTION_COMPLETED: ExecutionCompletedData,
    EventType.EXECUTION_PAUSED: ExecutionPausedData,
    EventType.BREAKPOINT_HIT: BreakpointHitData,
    # MEASUREMENT category
    EventType.MEASUREMENT_RECORDED: MeasurementRecordedData,
    EventType.VARIABLE_CHANGED: MeasurementRecordedData,
    # ALARM category
    EventType.STEP_TIMEOUT: StepTimeoutData,
    EventType.CONDITION_TIMEOUT: ConditionTimeoutData,
    EventType.RESOURCE_TIMEOUT: ResourceTimeoutData,
    EventType.DEADLOCK_DETECTED: DeadlockDetectedData,
    EventType.WORKER_EXHAUSTED: WorkerExhaustedData,
    EventType.HEARTBEAT_LOST: HeartbeatLostData,
    EventType.ALARM_RAISED: AlarmRaisedData,
}
