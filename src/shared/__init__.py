"""Shared types for ATE Platform.

This module provides a unified import path for common types:
- StepStatus, StepResult, Condition, VariableValue, ExecutionMode, StepType from types
- EventType, Event from events
- ExecutionContext from types
- YamlStep, YamlLoop, YamlPlan, LoopType, ExecutionMode, StepType from dsl

Example:
    from shared import StepStatus, StepResult, EventType, YamlStep, YamlPlan, YamlLoop
"""

from shared.config_schema import (
    ConfigManifest,
    InstrumentConfig,
    StationConfig,
    parse_config_manifest,
    parse_station_config,
    serialize_config_manifest,
    serialize_station_config,
)
from shared.dsl import LoopType, YamlLoop, YamlPlan, YamlStep
from shared.events import (
    Event,
    EventType,
    ExecutionCompletedData,
    ExecutionStartedData,
    ExternalCmdData,
    LoopIterationCompletedData,
    LoopIterationStartedData,
    ResourceReleasedData,
    StepCompletedData,
    StepStartedData,
    StepStatusChangedData,
    TimerExpiredData,
    VariableChangedData,
)
from shared.types import (
    Condition,
    ExecuteTask,
    ExecutionContext,
    ExecutionMode,
    LoopIterationResult,
    LoopResult,
    StepResult,
    StepStatus,
    StepType,
    VariableValue,
)

__all__ = [
    "StepStatus",
    "StepResult",
    "Condition",
    "VariableValue",
    "ExecutionContext",
    "ExecuteTask",
    "ExecutionMode",
    "StepType",
    "LoopIterationResult",
    "LoopResult",
    "EventType",
    "Event",
    "StepStatusChangedData",
    "StepStartedData",
    "StepCompletedData",
    "VariableChangedData",
    "ResourceReleasedData",
    "TimerExpiredData",
    "ExternalCmdData",
    "LoopIterationStartedData",
    "LoopIterationCompletedData",
    "ExecutionStartedData",
    "ExecutionCompletedData",
    "YamlStep",
    "YamlLoop",
    "YamlPlan",
    "LoopType",
    "InstrumentConfig",
    "StationConfig",
    "ConfigManifest",
    "parse_station_config",
    "serialize_station_config",
    "parse_config_manifest",
    "serialize_config_manifest",
]
