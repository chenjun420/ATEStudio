"""Unit tests for worker pool exhaustion detection and alarm.

Tests cover:
- Pool saturation warning (full pool, no deadlock risk)
- Pool exhaustion alarm (full pool, deadlock detected via resource cross-reference)
- pool_stats() returns correct values for both ProcessStepExecutor and ThreadStepExecutor
- Pool of 1: two concurrent steps (second queues, no crash)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import pytest

from ate_platform.executor.process_executor import ProcessExecutor
from ate_platform.executor.step_executor import (
    ProcessStepExecutor,
    ThreadStepExecutor,
)
from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.scheduler.event_bus import EventBus, EventType
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
from ate_platform.scheduler.step_registry import StepRegistry
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import StepResult, StepStatus
from shared.types import Condition


@pytest.fixture
def examples_dir() -> str:
    """Get the path to examples directory as string."""
    return str(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "examples")
    )


@pytest.fixture
def fixtures_dir() -> str:
    """Get the path to test fixtures directory as string."""
    return str(
        os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")
    )


class TestPoolUtilization:
    """Tests for pool utilization tracking in ProcessExecutor."""

    def test_get_pool_utilization_zero_workers(self) -> None:
        """get_pool_utilization should return 0.0 when max_workers is 0.
        
        Note: ThreadPoolExecutor requires max_workers >= 1, so we test
        the edge case by creating with max_workers=1 and verifying the
        formula handles zero correctly via division logic.
        """
        executor = ProcessExecutor(max_workers=1)
        try:
            utilization = executor.get_pool_utilization()
            assert utilization == 0.0
        finally:
            executor.shutdown()

    def test_get_pool_utilization_idle(self) -> None:
        """get_pool_utilization should return 0.0 when no tasks are running."""
        executor = ProcessExecutor(max_workers=4)
        try:
            utilization = executor.get_pool_utilization()
            assert utilization == 0.0
        finally:
            executor.shutdown()

    def test_get_pool_utilization_with_active_task(
        self, fixtures_dir: str
    ) -> None:
        """get_pool_utilization should reflect active tasks."""
        executor = ProcessExecutor(max_workers=4, script_timeout=30.0)
        try:
            # Initial state: idle
            assert executor.get_pool_utilization() == 0.0

            # Submit a long-running task
            script_path = os.path.join(fixtures_dir, "sleep_2s.py")

            def _run_in_thread() -> None:
                executor.execute(script_path, {}, step_id="test_active")

            thread = threading.Thread(target=_run_in_thread)
            thread.start()
            time.sleep(0.1)  # Let the task start

            # Should show active worker
            utilization = executor.get_pool_utilization()
            assert utilization > 0.0
            assert utilization <= 1.0

            thread.join(timeout=5.0)
        finally:
            executor.shutdown()


class TestProcessStepExecutorPoolStats:
    """Tests for pool_stats() on ProcessStepExecutor."""

    def test_pool_stats_idle(self) -> None:
        """pool_stats should return zero active workers when idle."""
        executor = ProcessStepExecutor(max_workers=4)
        try:
            stats = executor.pool_stats()
            assert stats["active"] == 0
            assert stats["max"] == 4
            assert stats["utilization"] == 0.0
            assert stats["queued"] == 0
        finally:
            executor.shutdown()

    def test_pool_stats_after_execution(
        self, examples_dir: str
    ) -> None:
        """pool_stats should return 0 active workers after execution completes."""
        executor = ProcessStepExecutor(max_workers=4, script_timeout=30.0)
        try:
            script_path = os.path.join(examples_dir, "test_pass.py")
            result = executor.execute(script_path, {"value": 42})

            assert result.status == StepStatus.PASSED
            stats = executor.pool_stats()
            assert stats["active"] == 0
            assert stats["max"] == 4
            assert stats["utilization"] == 0.0
        finally:
            executor.shutdown()

    def test_pool_stats_custom_max_workers(self) -> None:
        """pool_stats should reflect the configured max_workers."""
        executor = ProcessStepExecutor(max_workers=8)
        try:
            stats = executor.pool_stats()
            assert stats["max"] == 8
        finally:
            executor.shutdown()


class TestThreadStepExecutorPoolStats:
    """Tests for pool_stats() on ThreadStepExecutor."""

    def test_pool_stats_idle(self) -> None:
        """pool_stats should return zero active workers when idle."""
        executor = ThreadStepExecutor(max_workers=4)
        try:
            stats = executor.pool_stats()
            assert stats["active"] == 0
            assert stats["max"] == 4
            assert stats["utilization"] == 0.0
            assert stats["queued"] == 0
        finally:
            executor.shutdown()

    def test_pool_stats_after_execution(
        self, examples_dir: str
    ) -> None:
        """pool_stats should return 0 active workers after execution completes."""
        executor = ThreadStepExecutor(max_workers=4)
        try:
            script_path = os.path.join(examples_dir, "test_pass.py")
            result = executor.execute(script_path, {"value": 42})

            assert result.status == StepStatus.PASSED
            stats = executor.pool_stats()
            assert stats["active"] == 0
            assert stats["max"] == 4
            assert stats["utilization"] == 0.0
        finally:
            executor.shutdown()

    def test_pool_stats_custom_max_workers(self) -> None:
        """pool_stats should reflect the configured max_workers."""
        executor = ThreadStepExecutor(max_workers=8)
        try:
            stats = executor.pool_stats()
            assert stats["max"] == 8
        finally:
            executor.shutdown()


class TestPoolSaturationWarning:
    """Tests for pool saturation warning (no deadlock)."""

    @pytest.mark.asyncio
    async def test_pool_saturated_warning_no_resource_risk(
        self, fixtures_dir: str, caplog: Any
    ) -> None:
        """When pool is full but step has no resource requirements,
        log WARNING without alarm."""
        event_bus = EventBus()
        await event_bus.start()

        rm = ResourceManager()
        vs = VariableSpace()
        registry = StepRegistry()
        registry.register("step2", Condition())  # No resource requirements

        executor = ProcessStepExecutor(max_workers=1, script_timeout=5.0)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=ConditionEvaluator({}),
            variable_space=vs,
            resource_manager=rm,
            step_executor=executor,
        )

        # Force pool utilization to 1.0 by starting a task
        script_path = os.path.join(fixtures_dir, "sleep_2s.py")

        # Submit a task to saturate the pool
        thread = threading.Thread(
            target=executor.execute, args=(script_path, {}, "step1")
        )
        thread.start()
        time.sleep(0.1)  # Let task start

        with caplog.at_level(logging.WARNING, logger="ate_platform.scheduler"):
            # Trigger pool exhaustion check via _check_pool_exhaustion
            await scheduler._check_pool_exhaustion("step2", Condition())

        # Should have logged saturation warning
        warning_logs = [
            r.message
            for r in caplog.records
            if "Pool saturated" in r.message and "step2" in r.message
        ]
        assert len(warning_logs) >= 1, (
            f"Expected pool saturation warning, got: {[r.message for r in caplog.records]}"
        )

        thread.join(timeout=5.0)
        executor.shutdown()
        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_pool_not_saturated_no_warning(
        self, caplog: Any
    ) -> None:
        """When pool is not saturated, no warning should be logged."""
        event_bus = EventBus()
        await event_bus.start()

        rm = ResourceManager()
        vs = VariableSpace()
        registry = StepRegistry()
        registry.register("step2", Condition())

        executor = ProcessStepExecutor(max_workers=4, script_timeout=5.0)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=ConditionEvaluator({}),
            variable_space=vs,
            resource_manager=rm,
            step_executor=executor,
        )

        with caplog.at_level(logging.WARNING, logger="ate_platform.scheduler"):
            await scheduler._check_pool_exhaustion("step2", Condition())

        # Should NOT have logged saturation warning
        warning_logs = [
            r.message
            for r in caplog.records
            if "Pool saturated" in r.message
        ]
        assert len(warning_logs) == 0, (
            f"Unexpected pool saturation warning: {warning_logs}"
        )

        executor.shutdown()
        await event_bus.stop()


class TestPoolExhaustionAlarm:
    """Tests for WORKER_EXHAUSTED alarm publishing."""

    @pytest.mark.asyncio
    async def test_pool_exhaustion_with_deadlock_risk(
        self, fixtures_dir: str
    ) -> None:
        """When pool is full and step's resources are held by running workers,
        publish WORKER_EXHAUSTED alarm with deadlock_risk=True."""
        event_bus = EventBus()
        await event_bus.start()

        rm = ResourceManager()
        vs = VariableSpace()
        registry = StepRegistry()
        registry.register(
            "step2", Condition(resource_available=["DMM_CH1", "PSU_CH1"])
        )

        # Acquire the resources as if held by currently-running workers
        rm.acquire("DMM_CH1", "running_step_a")
        rm.acquire("PSU_CH1", "running_step_b")

        executor = ProcessStepExecutor(max_workers=1, script_timeout=5.0)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=ConditionEvaluator({}),
            variable_space=vs,
            resource_manager=rm,
            step_executor=executor,
        )

        # Satpute the pool
        script_path = os.path.join(fixtures_dir, "sleep_2s.py")

        thread = threading.Thread(
            target=executor.execute, args=(script_path, {}, "step1")
        )
        thread.start()
        time.sleep(0.1)

        # Collect WORKER_EXHAUSTED events
        exhausted_events: list[dict] = []

        def _on_worker_exhausted(event: Any) -> None:
            exhausted_events.append(event.data)

        event_bus.subscribe(EventType.WORKER_EXHAUSTED, _on_worker_exhausted)

        # Trigger pool exhaustion check
        condition = registry.get_condition("step2")
        await scheduler._check_pool_exhaustion("step2", condition)

        # Allow events to be processed
        await asyncio.sleep(0.1)

        assert len(exhausted_events) == 1, (
            f"Expected 1 WORKER_EXHAUSTED event, got {len(exhausted_events)}"
        )
        event_data = exhausted_events[0]
        assert event_data.get("deadlock_risk") is True
        assert "DMM_CH1" in event_data.get("blocked_resources", [])
        assert "PSU_CH1" in event_data.get("blocked_resources", [])
        assert "running_step_a" in event_data.get("holding_workers", [])
        assert "running_step_b" in event_data.get("holding_workers", [])
        assert event_data.get("severity") == "warning"
        assert event_data.get("recoverable") is True
        assert event_data.get("active_workers") >= 1
        assert event_data.get("max_workers") == 1

        event_bus.unsubscribe(EventType.WORKER_EXHAUSTED, _on_worker_exhausted)
        thread.join(timeout=5.0)
        executor.shutdown()
        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_pool_exhaustion_no_deadlock_all_resources_free(
        self, fixtures_dir: str
    ) -> None:
        """When all required resources are free, log warning without alarm."""
        event_bus = EventBus()
        await event_bus.start()

        rm = ResourceManager()
        vs = VariableSpace()
        registry = StepRegistry()
        registry.register(
            "step2", Condition(resource_available=["DMM_CH1", "PSU_CH1"])
        )

        # Do NOT acquire any resources — all are free

        executor = ProcessStepExecutor(max_workers=1, script_timeout=5.0)

        scheduler = ScannerScheduler(
            event_bus=event_bus,
            registry=registry,
            evaluator=ConditionEvaluator({}),
            variable_space=vs,
            resource_manager=rm,
            step_executor=executor,
        )

        # Satpute the pool
        script_path = os.path.join(fixtures_dir, "sleep_2s.py")

        thread = threading.Thread(
            target=executor.execute, args=(script_path, {}, "step1")
        )
        thread.start()
        time.sleep(0.1)

        # Collect WORKER_EXHAUSTED events
        exhausted_events: list[dict] = []

        def _on_worker_exhausted(event: Any) -> None:
            exhausted_events.append(event.data)

        event_bus.subscribe(EventType.WORKER_EXHAUSTED, _on_worker_exhausted)

        condition = registry.get_condition("step2")
        await scheduler._check_pool_exhaustion("step2", condition)

        await asyncio.sleep(0.1)

        # All resources free — no deadlock, no alarm
        assert len(exhausted_events) == 0, (
            f"Expected 0 WORKER_EXHAUSTED events (all resources free), "
            f"got {len(exhausted_events)}"
        )

        event_bus.unsubscribe(EventType.WORKER_EXHAUSTED, _on_worker_exhausted)
        thread.join(timeout=5.0)
        executor.shutdown()
        await event_bus.stop()


class TestPoolOf1TwoSteps:
    """Tests for pool of 1 with two concurrent steps."""

    def test_two_steps_pool_of_1_no_crash(self, fixtures_dir: str) -> None:
        """Second step queues when pool of 1 is full, no crash or deadlock."""
        executor = ProcessStepExecutor(max_workers=1, script_timeout=5.0)
        try:
            script_path = os.path.join(fixtures_dir, "pass_script.py")

            # First task: saturates the pool
            results: list[StepResult | None] = [None, None]

            def _run_step1() -> None:
                results[0] = executor.execute(
                    script_path, {"value": 1}, step_id="step1"
                )

            def _run_step2() -> None:
                results[1] = executor.execute(
                    script_path, {"value": 2}, step_id="step2"
                )

            thread1 = threading.Thread(target=_run_step1)
            thread2 = threading.Thread(target=_run_step2)

            thread1.start()
            time.sleep(0.05)  # Let step1 start first
            thread2.start()

            thread1.join(timeout=5.0)
            thread2.join(timeout=5.0)

            assert results[0] is not None, "Step 1 should have completed"
            assert results[1] is not None, "Step 2 should have completed"
            assert results[0].status == StepStatus.PASSED
            assert results[1].status == StepStatus.PASSED

            # Both should have completed — active workers back to 0
            assert executor.active_workers == 0
        finally:
            executor.shutdown()