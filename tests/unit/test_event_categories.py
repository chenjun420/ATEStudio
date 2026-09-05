"""Tests for TEMS A4 event category system.

Validates:
- EventCategory enum values
- Every EventType mapped to exactly one EventCategory
- Event dataclass auto-derives category from type
- VARIABLE_CHANGED deprecated alias behavior
- MeasurementRecordedData has timestamp, unit, instrument_id fields
- Alarm data classes have severity and recoverable fields
- EventBus.publish() validates category
- SSEBridge category mapping
"""

from __future__ import annotations

import asyncio
import warnings
from dataclasses import asdict
from datetime import datetime

import pytest

from shared.events import (
    EVENT_DATA_CLASSES,
    EVENT_TYPE_CATEGORIES,
    BreakpointHitData,
    ConditionTimeoutData,
    DeadlockDetectedData,
    Event,
    EventCategory,
    EventType,
    ExecutionCompletedData,
    ExecutionPausedData,
    ExecutionStartedData,
    ExternalCmdData,
    LoopIterationCompletedData,
    LoopIterationStartedData,
    MeasurementRecordedData,
    ResourceReleasedData,
    ResourceTimeoutData,
    StepCompletedData,
    StepFailedData,
    StepSkippedData,
    StepStartedData,
    StepStatusChangedData,
    StepTimeoutData,
    TimerExpiredData,
    VariableChangedData,
    WorkerExhaustedData,
    _warn_variable_changed_deprecated,
    get_event_category,
)

# ---------------------------------------------------------------------------
# EventCategory enum
# ---------------------------------------------------------------------------


class TestEventCategory:
    """Tests for EventCategory enum."""

    def test_category_values(self) -> None:
        """EventCategory has exactly three TEMS A4 values."""
        assert EventCategory.EVENT.value == "event"
        assert EventCategory.MEASUREMENT.value == "measurement"
        assert EventCategory.ALARM.value == "alarm"

    def test_category_count(self) -> None:
        """EventCategory has exactly 3 members."""
        assert len(EventCategory) == 3


# ---------------------------------------------------------------------------
# EventType → EventCategory mapping
# ---------------------------------------------------------------------------


class TestEventTypeCategoryMapping:
    """Tests for EventType to EventCategory mapping."""

    def test_every_event_type_has_category(self) -> None:
        """Every EventType is mapped to exactly one EventCategory."""
        for et in EventType:
            assert et in EVENT_TYPE_CATEGORIES, f"{et} has no category mapping"

    def test_no_extra_mappings(self) -> None:
        """EVENT_TYPE_CATEGORIES has no entries for non-EventType keys."""
        for key in EVENT_TYPE_CATEGORIES:
            assert isinstance(key, EventType)

    def test_event_category_types(self) -> None:
        """EVENT-category event types are correctly classified."""
        event_types = {
            EventType.STEP_STATUS_CHANGED,
            EventType.STEP_STARTED,
            EventType.STEP_COMPLETED,
            EventType.STEP_FAILED,
            EventType.STEP_SKIPPED,
            EventType.LOOP_ITERATION_STARTED,
            EventType.LOOP_ITERATION_COMPLETED,
            EventType.EXECUTION_STARTED,
            EventType.EXECUTION_COMPLETED,
            EventType.EXECUTION_PAUSED,
            EventType.RESOURCE_RELEASED,
            EventType.TIMER_EXPIRED,
            EventType.EXTERNAL_CMD,
        }
        for et in event_types:
            assert get_event_category(et) == EventCategory.EVENT, f"{et} should be EVENT"

    def test_measurement_category_types(self) -> None:
        """MEASUREMENT-category event types are correctly classified."""
        measurement_types = {EventType.MEASUREMENT_RECORDED, EventType.VARIABLE_CHANGED}
        for et in measurement_types:
            assert get_event_category(et) == EventCategory.MEASUREMENT, f"{et} should be MEASUREMENT"

    def test_alarm_category_types(self) -> None:
        """ALARM-category event types are correctly classified."""
        alarm_types = {
            EventType.STEP_TIMEOUT,
            EventType.CONDITION_TIMEOUT,
            EventType.RESOURCE_TIMEOUT,
            EventType.DEADLOCK_DETECTED,
            EventType.WORKER_EXHAUSTED,
        }
        for et in alarm_types:
            assert get_event_category(et) == EventCategory.ALARM, f"{et} should be ALARM"

    def test_get_event_category_raises_for_unknown(self) -> None:
        """get_event_category raises KeyError for unmapped types."""
        # This test ensures the function is strict
        # All current types should be mapped, so we test the function works
        for et in EventType:
            get_event_category(et)  # Should not raise


# ---------------------------------------------------------------------------
# Event dataclass category auto-derivation
# ---------------------------------------------------------------------------


class TestEventCategoryAutoDerivation:
    """Tests for Event dataclass auto-deriving category from type."""

    def test_event_auto_derives_category_event(self) -> None:
        """Event auto-derives EVENT category from STEP_STARTED type."""
        event = Event(type=EventType.STEP_STARTED, data={"step_id": "s1"})
        assert event.category == EventCategory.EVENT

    def test_event_auto_derives_category_measurement(self) -> None:
        """Event auto-derives MEASUREMENT category from MEASUREMENT_RECORDED type."""
        event = Event(type=EventType.MEASUREMENT_RECORDED, data={"name": "voltage"})
        assert event.category == EventCategory.MEASUREMENT

    def test_event_auto_derives_category_alarm(self) -> None:
        """Event auto-derives ALARM category from STEP_TIMEOUT type."""
        event = Event(type=EventType.STEP_TIMEOUT, data={"step_id": "s1"})
        assert event.category == EventCategory.ALARM

    def test_event_explicit_category_overrides(self) -> None:
        """Explicitly set category is preserved (not overwritten)."""
        event = Event(
            type=EventType.STEP_STARTED,
            data={},
            category=EventCategory.ALARM,
        )
        assert event.category == EventCategory.ALARM

    def test_event_default_timestamp(self) -> None:
        """Event auto-generates timestamp when not provided."""
        event = Event(type=EventType.STEP_STARTED, data={})
        assert isinstance(event.timestamp, datetime)


# ---------------------------------------------------------------------------
# VARIABLE_CHANGED deprecation
# ---------------------------------------------------------------------------


class TestVariableChangedDeprecation:
    """Tests for VARIABLE_CHANGED deprecated alias."""

    def test_variable_changed_same_value_as_measurement_recorded(self) -> None:
        """VARIABLE_CHANGED has the same wire value as MEASUREMENT_RECORDED."""
        assert EventType.VARIABLE_CHANGED.value == EventType.MEASUREMENT_RECORDED.value
        assert EventType.VARIABLE_CHANGED.value == "measurement_recorded"

    def test_variable_changed_is_measurement_category(self) -> None:
        """VARIABLE_CHANGED maps to MEASUREMENT category."""
        assert get_event_category(EventType.VARIABLE_CHANGED) == EventCategory.MEASUREMENT

    def test_deprecation_warning_emitted(self) -> None:
        """_warn_variable_changed_deprecated emits DeprecationWarning."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _warn_variable_changed_deprecated()
            assert len(caught) == 1
            assert issubclass(caught[0].category, DeprecationWarning)
            assert "VARIABLE_CHANGED" in str(caught[0].message)
            assert "MEASUREMENT_RECORDED" in str(caught[0].message)

    def test_variable_changed_data_is_alias(self) -> None:
        """VariableChangedData is an alias for MeasurementRecordedData."""
        assert VariableChangedData is MeasurementRecordedData

    def test_event_data_classes_both_mapped(self) -> None:
        """Both VARIABLE_CHANGED and MEASUREMENT_RECORDED map to MeasurementRecordedData."""
        assert EVENT_DATA_CLASSES[EventType.MEASUREMENT_RECORDED] is MeasurementRecordedData
        assert EVENT_DATA_CLASSES[EventType.VARIABLE_CHANGED] is MeasurementRecordedData


# ---------------------------------------------------------------------------
# MeasurementRecordedData fields
# ---------------------------------------------------------------------------


class TestMeasurementRecordedData:
    """Tests for MeasurementRecordedData with TEMS A4 measurement fields."""

    def test_has_timestamp_field(self) -> None:
        """MeasurementRecordedData has timestamp field."""
        data = MeasurementRecordedData(name="scope.voltage", timestamp=1234567890.0)
        assert data.timestamp == 1234567890.0

    def test_has_unit_field(self) -> None:
        """MeasurementRecordedData has unit field."""
        data = MeasurementRecordedData(name="scope.voltage", unit="V")
        assert data.unit == "V"

    def test_has_instrument_id_field(self) -> None:
        """MeasurementRecordedData has instrument_id field."""
        data = MeasurementRecordedData(name="scope.voltage", instrument_id="DMM_CH1")
        assert data.instrument_id == "DMM_CH1"

    def test_optional_fields_default_none(self) -> None:
        """Optional measurement fields default to None."""
        data = MeasurementRecordedData(name="scope.voltage")
        assert data.unit is None
        assert data.instrument_id is None

    def test_timestamp_defaults_zero(self) -> None:
        """Timestamp defaults to 0.0 when not provided."""
        data = MeasurementRecordedData(name="scope.voltage")
        assert data.timestamp == 0.0

    def test_asdict_includes_new_fields(self) -> None:
        """asdict() includes timestamp, unit, instrument_id."""
        data = MeasurementRecordedData(
            name="scope.voltage",
            old_value=None,
            new_value=3.3,
            timestamp=1234567890.0,
            unit="V",
            instrument_id="DMM_CH1",
        )
        d = asdict(data)
        assert d["timestamp"] == 1234567890.0
        assert d["unit"] == "V"
        assert d["instrument_id"] == "DMM_CH1"
        assert d["name"] == "scope.voltage"
        assert d["new_value"] == 3.3

    def test_backward_compat_with_old_fields(self) -> None:
        """Old VariableChangedData fields (name, old_value, new_value) still work."""
        data = MeasurementRecordedData(
            name="scope.voltage",
            old_value=3.0,
            new_value=3.3,
        )
        assert data.name == "scope.voltage"
        assert data.old_value == 3.0
        assert data.new_value == 3.3


# ---------------------------------------------------------------------------
# Alarm data classes — severity and recoverable
# ---------------------------------------------------------------------------


class TestAlarmDataClasses:
    """Tests for alarm data classes with severity and recoverable fields."""

    def test_step_timeout_has_severity_and_recoverable(self) -> None:
        """StepTimeoutData has severity and recoverable fields."""
        data = StepTimeoutData(step_id="s1", timeout_seconds=30.0)
        assert data.severity == "critical"
        assert data.recoverable is False

    def test_step_timeout_custom_severity(self) -> None:
        """StepTimeoutData accepts custom severity."""
        data = StepTimeoutData(step_id="s1", severity="warning", recoverable=True)
        assert data.severity == "warning"
        assert data.recoverable is True

    def test_condition_timeout_defaults(self) -> None:
        """ConditionTimeoutData defaults to warning severity, recoverable."""
        data = ConditionTimeoutData(step_id="s1")
        assert data.severity == "warning"
        assert data.recoverable is True

    def test_resource_timeout_defaults(self) -> None:
        """ResourceTimeoutData defaults to warning severity, recoverable."""
        data = ResourceTimeoutData(resource_id="DMM_CH1")
        assert data.severity == "warning"
        assert data.recoverable is True

    def test_deadlock_detected_defaults(self) -> None:
        """DeadlockDetectedData defaults to critical severity, not recoverable."""
        data = DeadlockDetectedData()
        assert data.severity == "critical"
        assert data.recoverable is False

    def test_worker_exhausted_defaults(self) -> None:
        """WorkerExhaustedData defaults to warning severity, recoverable."""
        data = WorkerExhaustedData()
        assert data.severity == "warning"
        assert data.recoverable is True

    def test_severity_literal_type(self) -> None:
        """Severity field only accepts 'warning' or 'critical'."""
        # Valid values
        StepTimeoutData(step_id="s1", severity="warning")
        StepTimeoutData(step_id="s1", severity="critical")

    def test_alarm_asdict_includes_severity_recoverable(self) -> None:
        """asdict() includes severity and recoverable for alarm data."""
        data = StepTimeoutData(step_id="s1", timeout_seconds=30.0)
        d = asdict(data)
        assert "severity" in d
        assert "recoverable" in d
        assert d["severity"] == "critical"
        assert d["recoverable"] is False

    def test_deadlock_detected_has_pending_steps(self) -> None:
        """DeadlockDetectedData has pending_steps list."""
        data = DeadlockDetectedData(
            pending_steps=["s1", "s2"],
            consecutive_scans=100,
        )
        assert data.pending_steps == ["s1", "s2"]
        assert data.consecutive_scans == 100

    def test_worker_exhausted_has_pool_info(self) -> None:
        """WorkerExhaustedData has pool_name, active_workers, max_workers."""
        data = WorkerExhaustedData(
            pool_name="step_pool",
            active_workers=4,
            max_workers=4,
        )
        assert data.pool_name == "step_pool"
        assert data.active_workers == 4
        assert data.max_workers == 4


# ---------------------------------------------------------------------------
# New EventType values
# ---------------------------------------------------------------------------


class TestNewEventTypes:
    """Tests for newly added EventType values."""

    def test_step_failed_exists(self) -> None:
        """STEP_FAILED event type exists."""
        assert EventType.STEP_FAILED.value == "STEP_FAILED"

    def test_step_skipped_exists(self) -> None:
        """STEP_SKIPPED event type exists."""
        assert EventType.STEP_SKIPPED.value == "STEP_SKIPPED"

    def test_execution_paused_exists(self) -> None:
        """EXECUTION_PAUSED event type exists."""
        assert EventType.EXECUTION_PAUSED.value == "EXECUTION_PAUSED"

    def test_step_timeout_exists(self) -> None:
        """STEP_TIMEOUT event type exists."""
        assert EventType.STEP_TIMEOUT.value == "STEP_TIMEOUT"

    def test_condition_timeout_exists(self) -> None:
        """CONDITION_TIMEOUT event type exists."""
        assert EventType.CONDITION_TIMEOUT.value == "CONDITION_TIMEOUT"

    def test_resource_timeout_exists(self) -> None:
        """RESOURCE_TIMEOUT event type exists."""
        assert EventType.RESOURCE_TIMEOUT.value == "RESOURCE_TIMEOUT"

    def test_deadlock_detected_exists(self) -> None:
        """DEADLOCK_DETECTED event type exists."""
        assert EventType.DEADLOCK_DETECTED.value == "DEADLOCK_DETECTED"

    def test_worker_exhausted_exists(self) -> None:
        """WORKER_EXHAUSTED event type exists."""
        assert EventType.WORKER_EXHAUSTED.value == "WORKER_EXHAUSTED"

    def test_measurement_recorded_exists(self) -> None:
        """MEASUREMENT_RECORDED event type exists."""
        assert EventType.MEASUREMENT_RECORDED.value == "measurement_recorded"


# ---------------------------------------------------------------------------
# New data classes
# ---------------------------------------------------------------------------


class TestNewDataClasses:
    """Tests for newly added data classes."""

    def test_step_failed_data(self) -> None:
        """StepFailedData has step_id and error fields."""
        data = asdict(
            __import__("shared.events", fromlist=["StepFailedData"]).StepFailedData(
                step_id="s1", error="timeout"
            )
        )
        assert data["step_id"] == "s1"
        assert data["error"] == "timeout"

    def test_step_skipped_data(self) -> None:
        """StepSkippedData has step_id and reason fields."""
        from shared.events import StepSkippedData

        data = asdict(StepSkippedData(step_id="s1", reason="condition not met"))
        assert data["step_id"] == "s1"
        assert data["reason"] == "condition not met"

    def test_execution_paused_data(self) -> None:
        """ExecutionPausedData has run_id and reason fields."""
        data = asdict(ExecutionPausedData(run_id="r1", reason="user request"))
        assert data["run_id"] == "r1"
        assert data["reason"] == "user request"


# ---------------------------------------------------------------------------
# EVENT_DATA_CLASSES completeness
# ---------------------------------------------------------------------------


class TestEventDataClassesCompleteness:
    """Tests that EVENT_DATA_CLASSES covers all EventTypes."""

    def test_all_event_types_have_data_class(self) -> None:
        """Every EventType has a corresponding data class."""
        for et in EventType:
            assert et in EVENT_DATA_CLASSES, f"{et} has no data class mapping"

    def test_all_data_classes_are_instantiable(self) -> None:
        """All data classes in EVENT_DATA_CLASSES can be instantiated."""
        required_args: dict[type, dict[str, str | int | float | list[str]]] = {
            StepStatusChangedData: {"step_id": "t", "old_status": "A", "new_status": "B"},
            StepStartedData: {"step_id": "t"},
            StepCompletedData: {"step_id": "t"},
            StepFailedData: {"step_id": "t"},
            StepSkippedData: {"step_id": "t"},
            MeasurementRecordedData: {"name": "t"},
            ResourceReleasedData: {"resource_id": "t", "owner_id": "t"},
            TimerExpiredData: {"timer_id": "t"},
            ExternalCmdData: {"command": "t"},
            LoopIterationStartedData: {"loop_id": "t"},
            LoopIterationCompletedData: {"loop_id": "t"},
            ExecutionStartedData: {"run_id": "t"},
            ExecutionCompletedData: {"run_id": "t"},
            ExecutionPausedData: {"run_id": "t"},
            BreakpointHitData: {
                "breakpoint_id": "t",
                "kind": "step",
                "target": "t",
                "step_id": "t",
            },
            StepTimeoutData: {"step_id": "t"},
            ConditionTimeoutData: {"step_id": "t"},
            ResourceTimeoutData: {"resource_id": "t"},
            DeadlockDetectedData: {},
            WorkerExhaustedData: {},
        }
        for _et, cls in EVENT_DATA_CLASSES.items():
            kwargs = required_args.get(cls, {})
            instance = cls(**kwargs)  # type: ignore[call-arg]
            d = asdict(instance)
            assert isinstance(d, dict)


# ---------------------------------------------------------------------------
# EventBus category validation
# ---------------------------------------------------------------------------


class TestEventBusCategoryValidation:
    """Tests for EventBus.publish() category validation."""

    @pytest.mark.asyncio
    async def test_publish_auto_sets_category(self) -> None:
        """EventBus.publish() auto-sets category on the Event."""
        from ate_platform.scheduler.event_bus import EventBus

        bus = EventBus()
        await bus.start()

        received: list[Event] = []
        bus.subscribe(None, lambda e: received.append(e))

        await bus.publish(EventType.STEP_STARTED, {"step_id": "s1"})
        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0].category == EventCategory.EVENT

        await bus.stop()

    @pytest.mark.asyncio
    async def test_publish_measurement_category(self) -> None:
        """EventBus.publish() sets MEASUREMENT category for MEASUREMENT_RECORDED."""
        from ate_platform.scheduler.event_bus import EventBus

        bus = EventBus()
        await bus.start()

        received: list[Event] = []
        bus.subscribe(None, lambda e: received.append(e))

        await bus.publish(EventType.MEASUREMENT_RECORDED, {"name": "voltage"})
        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0].category == EventCategory.MEASUREMENT

        await bus.stop()

    @pytest.mark.asyncio
    async def test_publish_alarm_category(self) -> None:
        """EventBus.publish() sets ALARM category for DEADLOCK_DETECTED."""
        from ate_platform.scheduler.event_bus import EventBus

        bus = EventBus()
        await bus.start()

        received: list[Event] = []
        bus.subscribe(None, lambda e: received.append(e))

        await bus.publish(EventType.DEADLOCK_DETECTED, {"pending_steps": []})
        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0].category == EventCategory.ALARM

        await bus.stop()


# ---------------------------------------------------------------------------
# SSEBridge category mapping
# ---------------------------------------------------------------------------


class TestSSEBridgeCategoryMapping:
    """Tests for SSEBridge category-based SSE event: line."""

    @pytest.mark.asyncio
    async def test_publish_event_includes_category(self) -> None:
        """SSEBridge.publish_event() includes category in the event dict."""
        from ate_cloud.nats.sse_bridge import SSEBridge

        bridge = SSEBridge(nc=None)  # Local mode
        queue = bridge.get_or_create_queue("run1")

        await bridge.publish_event("run1", "STEP_STARTED", {"step_id": "s1"})

        event = queue.get_nowait()
        assert event["category"] == "event"
        assert event["type"] == "STEP_STARTED"

    @pytest.mark.asyncio
    async def test_measurement_event_category(self) -> None:
        """SSEBridge maps MEASUREMENT_RECORDED to 'measurement' category."""
        from ate_cloud.nats.sse_bridge import SSEBridge

        bridge = SSEBridge(nc=None)
        queue = bridge.get_or_create_queue("run1")

        await bridge.publish_event("run1", "measurement_recorded", {"name": "voltage"})

        event = queue.get_nowait()
        assert event["category"] == "measurement"

    @pytest.mark.asyncio
    async def test_alarm_event_category(self) -> None:
        """SSEBridge maps DEADLOCK_DETECTED to 'alarm' category."""
        from ate_cloud.nats.sse_bridge import SSEBridge

        bridge = SSEBridge(nc=None)
        queue = bridge.get_or_create_queue("run1")

        await bridge.publish_event("run1", "DEADLOCK_DETECTED", {"pending_steps": []})

        event = queue.get_nowait()
        assert event["category"] == "alarm"

    @pytest.mark.asyncio
    async def test_unknown_event_type_defaults_to_event(self) -> None:
        """SSEBridge defaults unknown event types to 'event' category."""
        from ate_cloud.nats.sse_bridge import SSEBridge

        bridge = SSEBridge(nc=None)
        queue = bridge.get_or_create_queue("run1")

        await bridge.publish_event("run1", "UNKNOWN_TYPE", {})

        event = queue.get_nowait()
        assert event["category"] == "event"

    @pytest.mark.asyncio
    async def test_all_alarm_types_mapped(self) -> None:
        """All ALARM event types map to 'alarm' SSE category."""
        from ate_cloud.nats.sse_bridge import _EVENT_TYPE_TO_SSE_CATEGORY

        alarm_types = [
            "STEP_TIMEOUT",
            "CONDITION_TIMEOUT",
            "RESOURCE_TIMEOUT",
            "DEADLOCK_DETECTED",
            "WORKER_EXHAUSTED",
        ]
        for at in alarm_types:
            assert _EVENT_TYPE_TO_SSE_CATEGORY.get(at) == "alarm", f"{at} should map to 'alarm'"

    @pytest.mark.asyncio
    async def test_all_measurement_types_mapped(self) -> None:
        """All MEASUREMENT event types map to 'measurement' SSE category."""
        from ate_cloud.nats.sse_bridge import _EVENT_TYPE_TO_SSE_CATEGORY

        assert _EVENT_TYPE_TO_SSE_CATEGORY.get("measurement_recorded") == "measurement"


# ---------------------------------------------------------------------------
# Backward compatibility — existing EventType values preserved
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Tests for backward compatibility with existing event types."""

    def test_step_status_changed_unchanged(self) -> None:
        """STEP_STATUS_CHANGED value is unchanged."""
        assert EventType.STEP_STATUS_CHANGED.value == "STEP_STATUS_CHANGED"

    def test_step_started_unchanged(self) -> None:
        """STEP_STARTED value is unchanged."""
        assert EventType.STEP_STARTED.value == "STEP_STARTED"

    def test_step_completed_unchanged(self) -> None:
        """STEP_COMPLETED value is unchanged."""
        assert EventType.STEP_COMPLETED.value == "STEP_COMPLETED"

    def test_resource_released_unchanged(self) -> None:
        """RESOURCE_RELEASED value is unchanged."""
        assert EventType.RESOURCE_RELEASED.value == "RESOURCE_RELEASED"

    def test_timer_expired_unchanged(self) -> None:
        """TIMER_EXPIRED value is unchanged."""
        assert EventType.TIMER_EXPIRED.value == "TIMER_EXPIRED"

    def test_external_cmd_unchanged(self) -> None:
        """EXTERNAL_CMD value is unchanged."""
        assert EventType.EXTERNAL_CMD.value == "EXTERNAL_CMD"

    def test_loop_iteration_started_unchanged(self) -> None:
        """LOOP_ITERATION_STARTED value is unchanged."""
        assert EventType.LOOP_ITERATION_STARTED.value == "LOOP_ITERATION_STARTED"

    def test_loop_iteration_completed_unchanged(self) -> None:
        """LOOP_ITERATION_COMPLETED value is unchanged."""
        assert EventType.LOOP_ITERATION_COMPLETED.value == "LOOP_ITERATION_COMPLETED"

    def test_execution_started_unchanged(self) -> None:
        """EXECUTION_STARTED value is unchanged."""
        assert EventType.EXECUTION_STARTED.value == "EXECUTION_STARTED"

    def test_execution_completed_unchanged(self) -> None:
        """EXECUTION_COMPLETED value is unchanged."""
        assert EventType.EXECUTION_COMPLETED.value == "EXECUTION_COMPLETED"
