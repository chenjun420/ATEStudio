"""Execution recording and replay for ATE Platform.

Provides:
- RecordedEvent: Pydantic model for a single recorded execution event.
- ExecutionRecorder: Writes JSONL events to the
  ``ate.execution.{session_id}.events`` JetStream stream.
- ReplayExecutor: Replays recorded events in timestamp order with
  configurable time acceleration.
"""

from ate_platform.recorder.execution_recorder import ExecutionRecorder
from ate_platform.recorder.replay_executor import ReplayExecutor
from ate_platform.recorder.types import RecordedEvent, RecordedEventType

__all__ = [
    "ExecutionRecorder",
    "RecordedEvent",
    "RecordedEventType",
    "ReplayExecutor",
]
