"""Unit tests for StepRegistry in ATE Platform.

Tests cover:
- Basic register/update/get_status operations
- Condition-based readiness checking
- Thread safety
- Event publishing
"""

import asyncio
import threading
from typing import Any

import pytest

from ate_platform.scheduler.event_bus import EventBus, EventType
from ate_platform.scheduler.step_registry import StepRegistry
from ate_platform.types import Condition, StepStatus


class TestStepRegistryRegister:
    """Tests for StepRegistry register functionality."""

    def test_register_basic(self) -> None:
        """Should register a step with PENDING status."""
        registry = StepRegistry()

        registry.register("step1")

        assert registry.get_status("step1") == StepStatus.PENDING

    def test_register_with_condition(self) -> None:
        """Should register a step with a condition."""
        registry = StepRegistry()

        condition = Condition(step="step0", status="PASSED")
        registry.register("step1", condition)

        assert registry.get_status("step1") == StepStatus.PENDING
        assert registry.get_condition("step1") == condition

    def test_register_duplicate_raises(self) -> None:
        """Should raise ValueError for duplicate registration."""
        registry = StepRegistry()

        registry.register("step1")

        with pytest.raises(ValueError, match="already registered"):
            registry.register("step1")

    def test_register_empty_id_raises(self) -> None:
        """Should raise ValueError for empty step_id."""
        registry = StepRegistry()

        with pytest.raises(ValueError, match="cannot be empty"):
            registry.register("")

        with pytest.raises(ValueError, match="cannot be empty"):
            registry.register("   ")


class TestStepRegistryUpdateStatus:
    """Tests for StepRegistry update_status functionality."""

    def test_update_status_basic(self) -> None:
        """Should update step status."""
        registry = StepRegistry()

        registry.register("step1")
        registry.update_status("step1", StepStatus.RUNNING)

        assert registry.get_status("step1") == StepStatus.RUNNING

    def test_update_status_multiple_times(self) -> None:
        """Should allow multiple status updates."""
        registry = StepRegistry()

        registry.register("step1")
        registry.update_status("step1", StepStatus.RUNNING)
        registry.update_status("step1", StepStatus.PASSED)

        assert registry.get_status("step1") == StepStatus.PASSED

    def test_update_status_unregistered_raises(self) -> None:
        """Should raise KeyError for unregistered step."""
        registry = StepRegistry()

        with pytest.raises(KeyError, match="not registered"):
            registry.update_status("step1", StepStatus.RUNNING)


class TestStepRegistryGetStatus:
    """Tests for StepRegistry get_status functionality."""

    def test_get_status_returns_correct_value(self) -> None:
        """Should return the current status."""
        registry = StepRegistry()

        registry.register("step1")
        registry.update_status("step1", StepStatus.FAILED)

        assert registry.get_status("step1") == StepStatus.FAILED

    def test_get_status_unregistered_raises(self) -> None:
        """Should raise KeyError for unregistered step."""
        registry = StepRegistry()

        with pytest.raises(KeyError, match="not registered"):
            registry.get_status("unknown_step")


class TestStepRegistryGetReadySteps:
    """Tests for StepRegistry get_ready_steps functionality."""

    def test_get_ready_steps_empty_registry(self) -> None:
        """Should return empty list for empty registry."""
        registry = StepRegistry()

        assert registry.get_ready_steps() == []

    def test_get_ready_steps_pending_without_condition(self) -> None:
        """Should return PENDING steps without conditions."""
        registry = StepRegistry()

        registry.register("step1")
        registry.register("step2")

        ready = registry.get_ready_steps()

        assert "step1" in ready
        assert "step2" in ready

    def test_get_ready_steps_non_pending_excluded(self) -> None:
        """Should exclude non-PENDING steps."""
        registry = StepRegistry()

        registry.register("step1")
        registry.register("step2")
        registry.update_status("step1", StepStatus.RUNNING)

        ready = registry.get_ready_steps()

        assert "step1" not in ready
        assert "step2" in ready

    def test_get_ready_steps_condition_satisfied(self) -> None:
        """Should include steps with satisfied conditions."""
        registry = StepRegistry()

        registry.register("step1")
        registry.register("step2", Condition(step="step1", status="PASSED"))

        # Initially step2 should not be ready
        ready = registry.get_ready_steps()
        assert "step2" not in ready

        # Update step1 to PASSED
        registry.update_status("step1", StepStatus.PASSED)

        # Now step2 should be ready
        ready = registry.get_ready_steps()
        assert "step2" in ready

    def test_get_ready_steps_condition_not_satisfied(self) -> None:
        """Should exclude steps with unsatisfied conditions."""
        registry = StepRegistry()

        registry.register("step1")
        registry.register("step2", Condition(step="step1", status="PASSED"))

        ready = registry.get_ready_steps()

        assert "step2" not in ready

    def test_get_ready_steps_multiple_conditions(self) -> None:
        """Should handle multiple steps with different conditions."""
        registry = StepRegistry()

        registry.register("step1")
        registry.register("step2", Condition(step="step1", status="PASSED"))
        registry.register("step3")
        registry.register("step4", Condition(step="step2", status="PASSED"))

        # Initially only step1 and step3 are ready
        ready = registry.get_ready_steps()
        assert set(ready) == {"step1", "step3"}

        # Complete step1
        registry.update_status("step1", StepStatus.PASSED)
        ready = registry.get_ready_steps()
        assert "step2" in ready

        # Complete step2
        registry.update_status("step2", StepStatus.PASSED)
        ready = registry.get_ready_steps()
        assert "step4" in ready


class TestStepRegistryThreadSafety:
    """Tests for thread safety of StepRegistry."""

    def test_concurrent_register(self) -> None:
        """Should handle concurrent registrations safely."""
        registry = StepRegistry()
        errors: list[Exception] = []

        def register_steps(prefix: str, count: int) -> None:
            try:
                for i in range(count):
                    registry.register(f"{prefix}_{i}")
            except ValueError:
                # Expected for duplicates
                pass
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=register_steps, args=(f"thread{i}", 100))
            for i in range(10)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have no unexpected errors
        assert len(errors) == 0

        # Should have registered some steps
        steps = registry.get_all_steps()
        assert len(steps) > 0

    def test_concurrent_update_status(self) -> None:
        """Should handle concurrent status updates safely."""
        registry = StepRegistry()

        # Register steps
        for i in range(10):
            registry.register(f"step{i}")

        errors: list[Exception] = []

        def update_statuses(offset: int) -> None:
            try:
                for i in range(10):
                    registry.update_status(f"step{i}", StepStatus.RUNNING)
                    registry.update_status(f"step{i}", StepStatus.PASSED)
            except KeyError:
                # Should not happen
                pass
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=update_statuses, args=(i,)) for i in range(10)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have no errors
        assert len(errors) == 0

        # All steps should be PASSED
        for i in range(10):
            assert registry.get_status(f"step{i}") == StepStatus.PASSED

    def test_concurrent_read_write(self) -> None:
        """Should handle concurrent reads and writes safely."""
        registry = StepRegistry()

        registry.register("step1")

        read_count = 0
        write_count = 0
        lock = threading.Lock()

        def read_status() -> None:
            nonlocal read_count
            for _ in range(1000):
                registry.get_status("step1")
                with lock:
                    read_count += 1

        def write_status() -> None:
            nonlocal write_count
            statuses = [StepStatus.RUNNING, StepStatus.PASSED, StepStatus.FAILED]
            for i in range(1000):
                registry.update_status("step1", statuses[i % 3])
                with lock:
                    write_count += 1

        threads = [
            threading.Thread(target=read_status),
            threading.Thread(target=write_status),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All operations should complete
        assert read_count == 1000
        assert write_count == 1000


class TestStepRegistryEventPublishing:
    """Tests for event publishing on status changes."""

    @pytest.mark.asyncio
    async def test_publishes_event_on_status_change(self) -> None:
        """Should publish STEP_STATUS_CHANGED event."""
        bus = EventBus()
        registry = StepRegistry(event_bus=bus)

        received: list[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        bus.subscribe(EventType.STEP_STATUS_CHANGED, handler)
        await bus.start()

        registry.register("step1")
        registry.update_status("step1", StepStatus.RUNNING)

        # Give time for event to be processed
        await asyncio.sleep(0.1)
        await bus.stop()

        # Event should have been published
        assert len(received) >= 1
        event = received[0]
        assert event.data["step_id"] == "step1"
        assert event.data["old_status"] == "PENDING"
        assert event.data["new_status"] == "RUNNING"

    @pytest.mark.asyncio
    async def test_no_event_on_same_status(self) -> None:
        """Should not publish event when status unchanged."""
        bus = EventBus()
        registry = StepRegistry(event_bus=bus)

        received: list[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        bus.subscribe(EventType.STEP_STATUS_CHANGED, handler)
        await bus.start()

        registry.register("step1")
        registry.update_status("step1", StepStatus.PENDING)  # Same as initial

        await asyncio.sleep(0.05)
        await bus.stop()

        # No event should have been published
        assert len(received) == 0


class TestStepRegistryOtherMethods:
    """Tests for other StepRegistry methods."""

    def test_unregister_existing(self) -> None:
        """Should unregister an existing step."""
        registry = StepRegistry()

        registry.register("step1")
        result = registry.unregister("step1")

        assert result is True
        assert not registry.has_step("step1")

    def test_unregister_non_existing(self) -> None:
        """Should return False for non-existing step."""
        registry = StepRegistry()

        result = registry.unregister("unknown")

        assert result is False

    def test_has_step(self) -> None:
        """Should correctly check if step exists."""
        registry = StepRegistry()

        registry.register("step1")

        assert registry.has_step("step1") is True
        assert registry.has_step("unknown") is False

    def test_get_all_steps(self) -> None:
        """Should return copy of all steps."""
        registry = StepRegistry()

        registry.register("step1")
        registry.register("step2")

        steps = registry.get_all_steps()

        assert steps == {"step1": StepStatus.PENDING, "step2": StepStatus.PENDING}

        # Should be a copy, not the original
        steps["step3"] = StepStatus.PENDING
        assert not registry.has_step("step3")

    def test_get_condition(self) -> None:
        """Should return condition for step with condition."""
        registry = StepRegistry()

        condition = Condition(step="step0", status="PASSED")
        registry.register("step1", condition)

        result = registry.get_condition("step1")

        assert result == condition

    def test_get_condition_no_condition(self) -> None:
        """Should return None for step without condition."""
        registry = StepRegistry()

        registry.register("step1")

        result = registry.get_condition("step1")

        assert result is None

    def test_get_condition_unregistered_raises(self) -> None:
        """Should raise KeyError for unregistered step."""
        registry = StepRegistry()

        with pytest.raises(KeyError, match="not registered"):
            registry.get_condition("unknown")

    def test_clear(self) -> None:
        """Should clear all registered steps."""
        registry = StepRegistry()

        registry.register("step1")
        registry.register("step2")

        registry.clear()

        assert registry.get_all_steps() == {}
        assert registry.get_ready_steps() == []


class TestStepRegistryAllStatuses:
    """Tests for all possible StepStatus values."""

    def test_all_status_transitions(self) -> None:
        """Should handle all status values correctly."""
        registry = StepRegistry()

        registry.register("step1")

        statuses = [
            StepStatus.RUNNING,
            StepStatus.PASSED,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
            StepStatus.ERROR,
        ]

        for status in statuses:
            registry.update_status("step1", status)
            assert registry.get_status("step1") == status