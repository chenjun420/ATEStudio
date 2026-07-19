"""Core type definitions for ATE Platform.

This module defines the fundamental types used throughout the platform:
- StepStatus: Enum representing the execution state of a test step
- StepResult: Data container for step execution outcomes
- Condition: Preconditions for step execution
- VariableValue: TypedDict for test variable values
"""

from dataclasses import dataclass, field
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
