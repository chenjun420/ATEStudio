"""Tests for RecordedEvent and RecordedEventType (recorder.types)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ate_platform.recorder.types import RecordedEvent, RecordedEventType


class TestRecordedEventType:
    """Tests for the RecordedEventType enum."""

    def test_enum_values(self) -> None:
        """All five event type variants exist with correct string values."""
        assert RecordedEventType.STEP_TRANSITION.value == "step_transition"
        assert RecordedEventType.MEASUREMENT_RESULT.value == "measurement_result"
        assert RecordedEventType.OPERATOR_INTERACTION.value == "operator_interaction"
        assert RecordedEventType.SCHEDULER_DECISION.value == "scheduler_decision"
        assert RecordedEventType.NATS_MESSAGE.value == "nats_message"

    def test_enum_is_str(self) -> None:
        """RecordedEventType is string-compatible for JSON serialization."""
        assert isinstance(RecordedEventType.STEP_TRANSITION, str)

    def test_enum_from_value(self) -> None:
        """Enum can be constructed from its string value."""
        assert RecordedEventType("measurement_result") == RecordedEventType.MEASUREMENT_RESULT


class TestRecordedEvent:
    """Tests for the RecordedEvent Pydantic model."""

    def test_create_with_required_fields(self) -> None:
        """RecordedEvent can be created with only event_type and session_id."""
        event = RecordedEvent(
            event_type=RecordedEventType.STEP_TRANSITION,
            session_id="run-1",
        )
        assert event.event_type == RecordedEventType.STEP_TRANSITION
        assert event.session_id == "run-1"
        assert event.step_id is None
        assert event.data == {}
        assert isinstance(event.timestamp, datetime)

    def test_create_with_all_fields(self) -> None:
        """RecordedEvent accepts all fields including step_id and data."""
        ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        event = RecordedEvent(
            timestamp=ts,
            event_type=RecordedEventType.MEASUREMENT_RESULT,
            session_id="run-2",
            step_id="step-5",
            data={"name": "voltage", "value": 3.3, "unit": "V"},
        )
        assert event.timestamp == ts
        assert event.step_id == "step-5"
        assert event.data["value"] == 3.3

    def test_timestamp_auto_generated(self) -> None:
        """timestamp defaults to current UTC time when omitted."""
        before = datetime.now(UTC)
        event = RecordedEvent(
            event_type=RecordedEventType.NATS_MESSAGE,
            session_id="run-3",
        )
        after = datetime.now(UTC)
        assert before <= event.timestamp <= after

    def test_session_id_must_be_non_empty(self) -> None:
        """session_id rejects empty strings."""
        with pytest.raises(ValidationError):
            RecordedEvent(
                event_type=RecordedEventType.STEP_TRANSITION,
                session_id="",
            )

    def test_to_jsonl_round_trip(self) -> None:
        """to_jsonl and from_jsonl preserve all fields."""
        original = RecordedEvent(
            event_type=RecordedEventType.OPERATOR_INTERACTION,
            session_id="run-rt",
            step_id="step-1",
            data={"action": "button_press"},
        )
        line = original.to_jsonl()
        assert "\n" not in line
        restored = RecordedEvent.from_jsonl(line)
        assert restored.event_type == original.event_type
        assert restored.session_id == original.session_id
        assert restored.step_id == original.step_id
        assert restored.data == original.data
        assert restored.timestamp == original.timestamp

    def test_from_jsonl_strips_whitespace(self) -> None:
        """from_jsonl handles trailing whitespace/newlines."""
        event = RecordedEvent(
            event_type=RecordedEventType.SCHEDULER_DECISION,
            session_id="run-ws",
            data={"decision": "skip"},
        )
        line = event.to_jsonl() + "  \n"
        restored = RecordedEvent.from_jsonl(line)
        assert restored.session_id == "run-ws"

    def test_from_jsonl_invalid_json_raises(self) -> None:
        """from_jsonl raises ValidationError on malformed input."""
        with pytest.raises(ValidationError):
            RecordedEvent.from_jsonl("not json")

    def test_to_jsonl_is_valid_json(self) -> None:
        """to_jsonl output is parseable by json.loads."""
        event = RecordedEvent(
            event_type=RecordedEventType.MEASUREMENT_RESULT,
            session_id="run-json",
            step_id="s1",
            data={"value": 42},
        )
        parsed = json.loads(event.to_jsonl())
        assert parsed["event_type"] == "measurement_result"
        assert parsed["data"]["value"] == 42
