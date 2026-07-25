"""Core type definitions for ATE Platform.

This module defines the fundamental types used throughout the platform:
- StepStatus: Enum representing the execution state of a test step
- StepResult: Data container for step execution outcomes
- Condition: Preconditions for step execution
- VariableValue: TypedDict for test variable values
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, TypedDict


class StepStatus(Enum):
    """Enumeration of possible step execution states.

    Attributes:
        PENDING: Step has not started execution
        RUNNING: Step is currently executing
        PASSED: Step completed successfully
        FAILED: Step failed during execution
        SKIPPED: Step was skipped due to conditions
        ERROR: Step encountered an unexpected error
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


class ExecutionMode(Enum):
    """Enumeration of execution modes for steps within a loop.

    Attributes:
        SERIAL: Steps execute one after another
        PARALLEL: Steps execute concurrently
    """

    SERIAL = "SERIAL"
    PARALLEL = "PARALLEL"


class StepType(Enum):
    """Enumeration of step types.

    Attributes:
        SCRIPT: A script execution step
        LOOP: A loop construct containing nested steps
        CALL: A call to another plan or sub-routine
    """

    SCRIPT = "SCRIPT"
    LOOP = "LOOP"
    CALL = "CALL"


@dataclass
class StepResult:
    """Container for step execution results.

    Attributes:
        status: The final status of the step execution
        outputs: Dictionary of named outputs produced by the step
        error: Error message if step failed or errored, None otherwise
    """

    status: StepStatus
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class Condition:
    """Precondition for step execution.

    A condition specifies when a step should execute based on:
    - Another step's status
    - A boolean expression
    - Resource availability

    Attributes:
        step: Name of step to check status (optional)
        status: Expected status of referenced step (optional)
        expression: Boolean expression to evaluate (optional)
        resource_available: List of required resources (optional)
    """

    step: str | None = None
    status: str | None = None
    expression: str | None = None
    resource_available: list[str] | None = None


class VariableValue(TypedDict):
    """TypedDict for test variable values.

    Attributes:
        name: The variable name
        value: The variable value
        scope: The scope where the variable is valid (e.g., 'test', 'step', 'global')
    """

    name: str
    value: Any
    scope: str


@dataclass
class ExecuteTask:
    """Describes a single script execution task for batch processing.

    Attributes:
        script_path: Path to the Python script to execute
        params: Parameters to pass to the script
        step_id: Optional step identifier (auto-generated if not provided)
        timeout: Optional timeout override in seconds
        run_id: Optional execution run identifier
    """

    script_path: str
    params: dict[str, Any]
    step_id: str | None = None
    timeout: float | None = None
    run_id: str | None = None


@dataclass
class LoopIterationResult:
    """Container for a single loop iteration's execution result.

    Attributes:
        iteration: Zero-based iteration index
        status: The final status of this iteration
        outputs: Dictionary of named outputs produced by the iteration
        error: Error message if iteration failed or errored, None otherwise
        duration: Wall-clock duration of this iteration in seconds
    """

    iteration: int
    status: StepStatus
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration: float = 0.0


@dataclass
class LoopResult:
    """Container for the overall result of a loop execution.

    Attributes:
        loop_id: The loop identifier from the YamlLoop
        loop_type: The loop type string (FOR, WHILE, FOREACH)
        total_iterations: Total number of iterations executed
        passed: Number of iterations that passed
        failed: Number of iterations that failed or errored
        iteration_results: List of per-iteration results
        status: Aggregate status — PASSED if all iterations passed, FAILED otherwise
        duration: Total wall-clock duration of the loop in seconds
    """

    loop_id: str
    loop_type: str
    total_iterations: int = 0
    passed: int = 0
    failed: int = 0
    iteration_results: list[LoopIterationResult] = field(default_factory=list)
    status: StepStatus = StepStatus.PASSED
    duration: float = 0.0
    error: str | None = None


@dataclass
class ExecutionContext:
    """Tracks the current execution context for a plan run.

    Attributes:
        run_id: Unique identifier for this execution run
        sequence_id: Identifier for the test sequence being executed
        started_at: Timestamp when execution started
        status: Current execution status string
    """

    run_id: str
    sequence_id: str | None = None
    started_at: datetime | None = None
    status: str = "RUNNING"