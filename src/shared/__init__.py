"""Shared types for ATE Platform.

This module provides a unified import path for common types:
- StepStatus, StepResult, Condition, VariableValue from types
- EventType, Event from events
- YamlStep, YamlPlan from dsl

Example:
    from shared import StepStatus, StepResult, EventType, YamlStep, YamlPlan
"""

from shared.dsl import YamlPlan, YamlStep
from shared.events import Event, EventType
from shared.types import Condition, StepResult, StepStatus, VariableValue

__all__ = [
    "StepStatus",
    "StepResult",
    "Condition",
    "VariableValue",
    "EventType",
    "Event",
    "YamlStep",
    "YamlPlan",
]