"""Core type definitions for ATE Platform.

This module re-exports types from the shared module for backward compatibility.
All types are defined in src/shared/types.py.
"""

from shared.types import (
    Condition,
    ExecuteTask,
    ExecutionContext,
    LoopIterationResult,
    LoopResult,
    StepResult,
    StepStatus,
    VariableValue,
)

__all__ = [
    "StepStatus",
    "StepResult",
    "Condition",
    "VariableValue",
    "ExecutionContext",
    "ExecuteTask",
    "LoopIterationResult",
    "LoopResult",
]
