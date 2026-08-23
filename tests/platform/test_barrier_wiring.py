"""DSL BARRIER 步骤接线测试（v41-gap-analysis 任务 3）。

覆盖（设计意图：调度器派发 BARRIER 步骤时复用 UUTManager.wait_barrier/
SyncBarrier 驱动全池同步，绝不重新实现屏障逻辑）：

- 全员到达 → 步骤 PASSED（参与者为全池 UUT）
- 缺员超时 → 已到达者放行、缺员 UUT 标记 FAILED、步骤 FAILED
- 同名屏障跨多个同步点可复用（满员消费后自动重建）
- 单 UUT 计划的屏障是无死锁直通
- 响应式派发路径（_dispatch_step）同样驱动屏障
- 注册期校验：空 barrier_name 拒绝
- 未接 UUTManager：直通通过（向后兼容）
- 超时失败经 handle_step_result 决策矩阵路由（repeat 策略生效）
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.scheduler.event_bus import (
    Event,
    EventBus,
    EventType,
    get_event_category,
)
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
from ate_platform.scheduler.step_registry import StepExecutionConfig, StepRegistry
from ate_platform.scheduler.uut_sync import UUTManager, UUTState
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import StepStatus


class _RecordingEventBus(EventBus):
    """同步记录发布的假总线（不排队；publish_sync 内联回调订阅者）。

    与 test_uut_affinity_scheduling.py 相同的模式：registry.update_status
    触发的 STEP_STATUS_CHANGED 在测试中确定性走通订阅链。
    """

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[EventType, dict[str, Any]]] = []

    async def publish(self, event_type: EventType, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))

    def publish_sync(self, event_type: EventType, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))
        event = Event(
            type=event_type, data=data, category=get_event_category(event_type)
        )
        for callback in list(self._subscribers.get(event_type, [])):
            callback(event)


class _RecordingUUTManager(UUTManager):
    """记录 wait_barrier 调用参数的管理器（其余行为逐字节继承）。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls: list[tuple[str, str, float]] = []

    def wait_barrier(  # type: ignore[override]
        self, barrier_name: str, uut_id: str, timeout: float = 60.0
    ) -> Any:
        self.calls.append((barrier_name, uut_id, timeout))
        return super().wait_barrier(barrier_name, uut_id, timeout=timeout)


def _make_scheduler(
    bus: EventBus,
    uut_manager: UUTManager | None = None,
) -> ScannerScheduler:
    scheduler = ScannerScheduler(
        event_bus=bus,
        registry=StepRegistry(event_bus=bus),
        evaluator=ConditionEvaluator({}, ResourceManager(), None),
        variable_space=VariableSpace(),
        resource_manager=ResourceManager(),
        uut_manager=uut_manager,
    )
    scheduler._setup_event_handlers()
    return scheduler


def _events(bus: _RecordingEventBus, etype: EventType) -> list[dict[str, Any]]:
    return [d for t, d in bus.events if t == etype]


# ---------------------------------------------------------------------------
# 全员到达 —— 步骤通过
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_barrier_all_arrive_step_passes() -> None:
    """3-UUT 池全员空闲：派发即并发到达，步骤 PASSED，无缺员标记。"""
    manager = _RecordingUUTManager(uut_ids=["UUT_0", "UUT_1", "UUT_2"])
    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register("b_sync")
    scheduler.register_barrier_steps({"b_sync": "sync1"}, default_timeout=5.0)

    started = time.monotonic()
    await scheduler._emergency_scan()
    elapsed = time.monotonic() - started

    assert [d["step_id"] for d in _events(bus, EventType.STEP_STARTED)] == ["b_sync"]
    assert scheduler._registry.get_status("b_sync") == StepStatus.PASSED
    # 参与者正确性：全池 3 个 UUT 各到达一次，携带注册的超时
    assert sorted(manager.calls) == [
        ("sync1", "UUT_0", 5.0),
        ("sync1", "UUT_1", 5.0),
        ("sync1", "UUT_2", 5.0),
    ]
    # 无 UUT 被标记失败
    assert all(u.state != UUTState.FAILED for u in manager.uuts.values())
    # 满员即放行 —— 不应等待整个超时窗口
    assert elapsed < 4.0


@pytest.mark.asyncio
async def test_reactive_dispatch_path_runs_barrier() -> None:
    """响应式派发路径（_dispatch_step）同样驱动屏障收敛。"""
    manager = UUTManager(uut_ids=["UUT_0", "UUT_1"])
    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register("b_sync")
    scheduler.register_barrier_steps({"b_sync": "sync1"}, default_timeout=5.0)

    await scheduler._dispatch_step("b_sync")

    assert scheduler._registry.get_status("b_sync") == StepStatus.PASSED


# ---------------------------------------------------------------------------
# 缺员超时 —— 到达者放行、缺员 FAILED、步骤 FAILED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_barrier_timeout_marks_laggard_failed_and_releases_others() -> None:
    """忙态 UUT 无法到达（天然缺员）：短超时后到达者放行、缺员标 FAILED。"""
    manager = UUTManager(uut_ids=["UUT_0", "UUT_1", "UUT_2"])
    manager.get("UUT_2").start_test()  # UUT_2 忙于别处 —— 屏障缺员
    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register("b_sync")
    scheduler.register_barrier_steps({"b_sync": "sync1"}, default_timeout=0.2)

    await scheduler._emergency_scan()

    # 步骤按失败处理 + STEP_FAILED 事件携带屏障上下文
    assert scheduler._registry.get_status("b_sync") == StepStatus.FAILED
    failed_events = _events(bus, EventType.STEP_FAILED)
    assert len(failed_events) == 1
    assert "sync1" in failed_events[0]["error"]
    assert "UUT_2" in failed_events[0]["error"]

    # 缺员 UUT 标记 FAILED 并带原因
    laggard = manager.get("UUT_2")
    assert laggard.state == UUTState.FAILED
    assert laggard.last_error is not None and "sync1" in laggard.last_error
    # 到达者未被误标
    assert manager.get("UUT_0").state != UUTState.FAILED
    assert manager.get("UUT_1").state != UUTState.FAILED

    # 屏障已解除并清理 —— 后续同名同步点可复用
    assert "sync1" not in manager._barriers


@pytest.mark.asyncio
async def test_all_busy_barrier_fails_fast_marking_all_missing() -> None:
    """全池皆忙：无人能到达，立即按缺员失败处理（不空等超时窗口）。"""
    manager = UUTManager(uut_ids=["UUT_0", "UUT_1"])
    manager.get("UUT_0").start_test()
    manager.get("UUT_1").start_test()
    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register("b_sync")
    scheduler.register_barrier_steps({"b_sync": "sync1"}, default_timeout=30.0)

    started = time.monotonic()
    await scheduler._emergency_scan()
    elapsed = time.monotonic() - started

    assert scheduler._registry.get_status("b_sync") == StepStatus.FAILED
    assert elapsed < 5.0, "全忙缺员必须快速失败而非等待完整超时"
    assert all(u.state == UUTState.FAILED for u in manager.uuts.values())


# ---------------------------------------------------------------------------
# 复用与直通
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_barrier_reusable_across_multiple_sync_points() -> None:
    """同一屏障名的两个连续同步点：满员消费后自动重建，双双通过。"""
    manager = UUTManager(uut_ids=["UUT_0", "UUT_1"])
    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register("b_first")
    scheduler._registry.register("b_second")
    scheduler.register_barrier_steps(
        {"b_first": "sync1", "b_second": "sync1"}, default_timeout=5.0
    )

    await scheduler._emergency_scan()  # b_first 派发并收敛
    await scheduler._emergency_scan()  # b_second 复用同名屏障

    assert scheduler._registry.get_status("b_first") == StepStatus.PASSED
    assert scheduler._registry.get_status("b_second") == StepStatus.PASSED


@pytest.mark.asyncio
async def test_single_uut_barrier_is_noop_passthrough() -> None:
    """单 UUT 计划：唯一参与者到达即满员 —— 立即通过，无死锁。"""
    manager = UUTManager(count=1)
    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register("b_solo")
    scheduler.register_barrier_steps({"b_solo": "solo_sync"}, default_timeout=30.0)

    started = time.monotonic()
    await scheduler._emergency_scan()
    elapsed = time.monotonic() - started

    assert scheduler._registry.get_status("b_solo") == StepStatus.PASSED
    assert elapsed < 5.0, "单参与者屏障不得等待超时窗口"


@pytest.mark.asyncio
async def test_barrier_without_uut_manager_passthrough() -> None:
    """未接 UUTManager：无同步对象，直通通过（向后兼容）。"""
    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus)  # 无 uut_manager
    scheduler._registry.register("b_orphan")
    scheduler.register_barrier_steps({"b_orphan": "sync1"})

    await scheduler._emergency_scan()

    assert scheduler._registry.get_status("b_orphan") == StepStatus.PASSED


def test_empty_barrier_name_rejected_at_registration() -> None:
    """注册期校验：空 barrier_name 拒绝（fail-fast，与 T2 风格一致）。"""
    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus)

    with pytest.raises(ValueError, match="b_bad"):
        scheduler.register_barrier_steps({"b_bad": "   "})
    # 校验失败不留下半套注册
    scheduler.register_barrier_steps({"b_ok": "sync1"})
    assert scheduler._barrier_steps == {"b_ok": "sync1"}


# ---------------------------------------------------------------------------
# 失败路由 —— 经 handle_step_result 决策矩阵
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_failure_routes_through_handle_step_result() -> None:
    """超时失败走 handle_step_result：repeat_on_measurement_fail 时置回 PENDING。"""
    manager = UUTManager(uut_ids=["UUT_0", "UUT_1"])
    manager.get("UUT_1").start_test()  # 缺员
    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register(
        "b_retry",
        config=StepExecutionConfig(repeat_on_measurement_fail=True, repeat_limit=1),
    )
    scheduler.register_barrier_steps({"b_retry": "sync1"}, default_timeout=0.15)

    await scheduler._emergency_scan()
    assert scheduler._registry.get_status("b_retry") == StepStatus.PENDING, (
        "repeat 策略应把失败的屏障步骤置回 PENDING"
    )

    # 缺员 UUT 仍滞留在长操作中（保持忙态）—— 第二次派发依旧无法到达
    manager.get("UUT_1").start_test()

    # 第二次派发仍缺员且 repeat 耗尽 → 终态 FAILED
    await scheduler._emergency_scan()
    assert scheduler._registry.get_status("b_retry") == StepStatus.FAILED
