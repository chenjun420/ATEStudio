"""YAML DSL type definitions for ATE Platform.

This module defines data structures for YAML DSL:
- LoopType: Enum for loop types (FOR, WHILE, FOREACH)
- ExecutionMode: Enum for execution modes (SERIAL, PARALLEL)
- StepType: Enum for step types (SCRIPT, LOOP, CALL)
- YamlStep: Represents a single step in the execution plan
- YamlLoop: Represents a loop construct in the execution plan
- YamlPlan: Represents the complete execution plan from YAML
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LoopType(Enum):
    """Enumeration of loop types.

    Attributes:
        FOR: Counted loop with explicit range
        WHILE: Conditional loop with break condition
        FOREACH: Iterate over a collection
    """

    FOR = "FOR"
    WHILE = "WHILE"
    FOREACH = "FOREACH"


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
class YamlStep:
    """Represents a single step in the execution plan.

    Attributes:
        id: Unique identifier for the step
        script: Path or name of the script to execute
        params: Parameters passed to the script
        preconditions: List of step IDs that must complete before this step
        resources: Resource requirements for this step
        timeout: Maximum execution time in seconds
        retry: Number of retry attempts on failure
        on_fail: Action to take on failure (e.g. 'stop', 'skip', 'ignore')
        export_outputs: Whether to export step outputs to plan-level scope
        skip_if: Expression that, if True, causes this step to be skipped
        skip_reason: Human-readable reason logged when step is skipped
    """

    id: str
    script: str
    params: dict[str, Any] = field(default_factory=dict)
    preconditions: list[str] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)
    timeout: int = 60
    retry: int = 0
    on_fail: str | None = None
    export_outputs: bool = False
    skip_if: str | None = None
    skip_reason: str | None = None


@dataclass
class YamlLoop:
    """Represents a loop construct in the execution plan.

    A loop contains nested steps (and optionally other loops) that are
    executed repeatedly based on the loop type and conditions.

    Attributes:
        id: Unique identifier for the loop
        loop_type: Type of loop (FOR, WHILE, FOREACH)
        steps: Nested steps or loops to execute in each iteration
        count: Number of iterations (for FOR loops)
        condition: Break condition expression (for WHILE loops)
        collection: Variable name holding the collection (for FOREACH loops)
        iterator_var: Variable name for the current item (for FOREACH loops)
        execution_mode: Whether nested steps run serially or in parallel
        max_iterations: Safety limit on iterations (prevents infinite loops)
        skip_if: Expression that, if True, causes this loop to be skipped
        skip_reason: Human-readable reason logged when loop is skipped
    """

    id: str
    loop_type: LoopType
    steps: list[YamlStep | YamlLoop] = field(default_factory=list)
    count: int | None = None
    condition: str | None = None
    collection: str | None = None
    iterator_var: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.SERIAL
    max_iterations: int = 1000
    skip_if: str | None = None
    skip_reason: str | None = None


@dataclass
class YamlPlan:
    """Represents the complete execution plan from YAML.

    Attributes:
        name: Name of the test plan
        version: Version of the test plan
        scope: Scope variables as a dictionary (supports both dict and string for backward compat)
        max_concurrency: Maximum number of concurrent steps
        steps: List of steps and/or loops in the plan
    """

    name: str
    version: str
    scope: dict[str, Any] = field(default_factory=dict)
    max_concurrency: int = 1
    steps: list[YamlStep | YamlLoop] = field(default_factory=list)