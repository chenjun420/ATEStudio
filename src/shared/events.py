"""Event types for ATE Platform.

This module defines event-related types:
- EventType: Enum of supported event types
- Event: Data container for event messages
- Typed event data classes for each EventType
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
        STEP_STARTED: A step has started execution
        STEP_COMPLETED: A step has completed execution
        LOOP_ITERATION_STARTED: A loop iteration has started
        LOOP_ITERATION_COMPLETED: A loop iteration has completed
        EXECUTION_STARTED: Plan execution has started
        EXECUTION_COMPLETED: Plan execution has completed
    """

    STEP_STATUS_CHANGED = "STEP_STATUS_CHANGED"
    VARIABLE_CHANGED = "VARIABLE_CHANGED"
    RESOURCE_RELEASED = "RESOURCE_RELEASED"
    TIMER_EXPIRED = "TIMER_EXPIRED"
    EXTERNAL_CMD = "EXTERNAL_CMD"
    STEP_STARTED = "STEP_STARTED"
    STEP_COMPLETED = "STEP_COMPLETED"
    LOOP_ITERATION_STARTED = "LOOP_ITERATION_STARTED"
    LOOP_ITERATION_COMPLETED = "LOOP_ITERATION_COMPLETED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"


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
class VariableChangedData:
    """Data for VARIABLE_CHANGED events.

    Attributes:
        name: Variable name with scope prefix (e.g. 'scope.voltage')
        old_value: Previous value (None if variable was created)
        new_value: New value
        run_id: Execution run identifier
    """

    name: str
    old_value: Any = None
    new_value: Any = None
    run_id: str | None = None


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


# Mapping from EventType to its typed data class
EVENT_DATA_CLASSES: dict[EventType, type] = {
    EventType.STEP_STATUS_CHANGED: StepStatusChangedData,
    EventType.STEP_STARTED: StepStartedData,
    EventType.STEP_COMPLETED: StepCompletedData,
    EventType.VARIABLE_CHANGED: VariableChangedData,
    EventType.RESOURCE_RELEASED: ResourceReleasedData,
    EventType.TIMER_EXPIRED: TimerExpiredData,
    EventType.EXTERNAL_CMD: ExternalCmdData,
    EventType.LOOP_ITERATION_STARTED: LoopIterationStartedData,
    EventType.LOOP_ITERATION_COMPLETED: LoopIterationCompletedData,
    EventType.EXECUTION_STARTED: ExecutionStartedData,
    EventType.EXECUTION_COMPLETED: ExecutionCompletedData,
}