"""UUT affinity scheduling tests（v41-gap-analysis 任务 2）。

覆盖（设计意图：specific 亲和的步骤只在所指 UUT 槽位空闲时才就绪；
'any' 语义与 T2 之前逐字节一致；亲和门在共享仪器资源授予之前生效，
绝不绕过 ResourceLock）：

- 'any' 亲和：UUT 池全忙时照常派发（默认行为不变）
- specific 亲和：所指 UUT 忙 → 等待；空闲 → 立即派发
- 混合计划：any 步骤先行，pinned 步骤等 UUT 释放后补位
- 同一 UUT 上两个 pinned 步骤串行化（占用声明 + 终态释放）
- 未知 UUT id 在注册期拒绝（ValueError，报错含池内 id）
- 未接 UUTManager 时 specific 亲和直通（向后兼容）
- 亲和门与 ResourceLock 组合：资源被占/槽位被占任一命中即不派发
- 响应式派发路径（_dispatch_step）同样过亲和门
"""

from __future__ import annotations

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
from ate_platform.scheduler.step_registry import StepRegistry
from ate_platform.scheduler.uut_sync import UUTManager
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import Condition, StepStatus


class _RecordingEventBus(EventBus):
    """同步记录发布的假总线（不排队、不分发，测试确定性）。

    - ``publish``（异步路径：STEP_STARTED 等）：只记录不分发。
    - ``publish_sync``（STEP_STATUS_CHANGED）：记录后**同步**回调订阅者，
      使 registry.update_status → 调度器 on_step_status_changed 的
      UUT 占用释放链路能在测试中确定性走通。
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


def _make_scheduler(
    bus: EventBus,
    uut_manager: UUTManager | None = None,
    resource_manager: ResourceManager | None = None,
) -> ScannerScheduler:
    rm = resource_manager or ResourceManager()
    scheduler = ScannerScheduler(
        event_bus=bus,
        registry=StepRegistry(event_bus=bus),
        # 生产接线：调度器级 evaluator 与 ResourceManager 共享，
        # _emergency_scan 的条件复核才能验证 resource_available
        evaluator=ConditionEvaluator({}, rm, None),
        variable_space=VariableSpace(),
        resource_manager=rm,
        uut_manager=uut_manager,
    )
    scheduler._setup_event_handlers()
    return scheduler


def _started(bus: _RecordingEventBus) -> list[str]:
    """按发布顺序返回 STEP_STARTED 的 step_id 列表。"""
    return [d["step_id"] for t, d in bus.events if t == EventType.STEP_STARTED]


# ---------------------------------------------------------------------------
# 'any' 亲和 —— 默认行为逐字节不变
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_any_affinity_schedules_immediately_with_saturated_pool() -> None:
    """UUT 池全忙时 'any' 亲和步骤照常派发（T2 前行为不变）。"""
    manager = UUTManager(uut_ids=["UUT_0", "UUT_1"])
    assert manager.allocate() is not None
    assert manager.allocate() is not None  # 两块 UUT 全部 TESTING

    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register("s_any")
    scheduler.register_uut_affinities({"s_any": "any"})

    await scheduler._emergency_scan()

    assert _started(bus) == ["s_any"]
    # 派发只发事件不改状态 —— 状态由执行器消费 STEP_STARTED 后更新
    assert scheduler._registry.get_status("s_any") == StepStatus.PENDING


@pytest.mark.asyncio
async def test_no_uut_manager_specific_affinity_is_passthrough() -> None:
    """未接 UUTManager 时 specific 亲和注册与派发均直通（向后兼容）。"""
    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus)  # 无 uut_manager
    scheduler._registry.register("s_pin")
    scheduler.register_uut_affinities({"s_pin": "UUT_9"})  # 无法校验，接受

    await scheduler._emergency_scan()

    assert _started(bus) == ["s_pin"]


# ---------------------------------------------------------------------------
# specific 亲和 —— 槽位门控
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_specific_affinity_waits_until_named_uut_free() -> None:
    """pinned 步骤在所指 UUT 忙时等待，释放后下一次扫描即派发。"""
    manager = UUTManager(uut_ids=["UUT_0", "UUT_1"])
    manager.get("UUT_1").start_test()  # 外部把 UUT_1 置忙

    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register("s_pin")
    scheduler.register_uut_affinities({"s_pin": "UUT_1"})

    await scheduler._emergency_scan()

    assert _started(bus) == [], "UUT_1 忙时 pinned 步骤不得派发"
    assert "s_pin" not in scheduler._notified_ready

    manager.release("UUT_1")  # finish(passed=True) → 不再 busy

    await scheduler._emergency_scan()

    assert _started(bus) == ["s_pin"]


@pytest.mark.asyncio
async def test_specific_affinity_dispatches_when_uut_idle() -> None:
    """所指 UUT 空闲时 pinned 步骤首个扫描周期即派发。"""
    manager = UUTManager(uut_ids=["UUT_0", "UUT_1"])

    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register("s_pin")
    scheduler.register_uut_affinities({"s_pin": "UUT_1"})

    await scheduler._emergency_scan()

    assert _started(bus) == ["s_pin"]


@pytest.mark.asyncio
async def test_mixed_plan_any_first_pinned_after_release() -> None:
    """混合计划：any 步骤先行，pinned 步骤等 UUT 释放后按序补位。"""
    manager = UUTManager(uut_ids=["UUT_0", "UUT_1"])
    manager.get("UUT_1").start_test()

    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register("s_any")
    scheduler._registry.register("s_pin")
    scheduler.compile_plan([("s_any", None), ("s_pin", None)])
    scheduler.register_uut_affinities({"s_any": "any", "s_pin": "UUT_1"})

    await scheduler._emergency_scan()
    assert _started(bus) == ["s_any"], "忙池中只有 any 步骤派发"

    await scheduler._emergency_scan()  # UUT_1 仍忙
    assert _started(bus) == ["s_any"], "pinned 步骤继续等待"

    manager.release("UUT_1")
    await scheduler._emergency_scan()
    assert _started(bus) == ["s_any", "s_pin"], "释放后 pinned 步骤补位"


@pytest.mark.asyncio
async def test_two_pinned_steps_serialize_on_same_uut() -> None:
    """同一 UUT 的两个 pinned 步骤串行化：占用直到终态才释放。"""
    manager = UUTManager(uut_ids=["UUT_0", "UUT_1"])

    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register("s1")
    scheduler._registry.register("s2")
    scheduler.compile_plan([("s1", None), ("s2", None)])
    scheduler.register_uut_affinities({"s1": "UUT_1", "s2": "UUT_1"})

    await scheduler._emergency_scan()
    assert _started(bus) == ["s1"]
    assert scheduler._affinity_claims == {"UUT_1": "s1"}

    await scheduler._emergency_scan()  # s1 未到终态，占用仍在
    assert _started(bus) == ["s1"], "s2 必须等 s1 让出 UUT_1"

    scheduler._registry.update_status("s1", StepStatus.PASSED)  # 同步释放占用
    assert scheduler._affinity_claims == {}

    await scheduler._emergency_scan()
    assert _started(bus) == ["s1", "s2"]


@pytest.mark.asyncio
async def test_claim_released_on_failed_status() -> None:
    """FAILED 终态同样释放占用，后续 pinned 步骤可派发。"""
    manager = UUTManager(uut_ids=["UUT_1"])

    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register("s1")
    scheduler._registry.register("s2")
    scheduler.compile_plan([("s1", None), ("s2", None)])
    scheduler.register_uut_affinities({"s1": "UUT_1", "s2": "UUT_1"})

    await scheduler._emergency_scan()
    assert scheduler._affinity_claims == {"UUT_1": "s1"}

    scheduler._registry.update_status("s1", StepStatus.FAILED)
    assert scheduler._affinity_claims == {}

    await scheduler._emergency_scan()
    assert _started(bus) == ["s1", "s2"]


# ---------------------------------------------------------------------------
# 注册期校验
# ---------------------------------------------------------------------------


def test_unknown_uut_id_rejected_at_registration() -> None:
    """未知 UUT id 在注册期拒绝，报错含步骤名与池内 id。"""
    manager = UUTManager(uut_ids=["UUT_0", "UUT_1"])
    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)

    with pytest.raises(ValueError, match="UUT_X") as excinfo:
        scheduler.register_uut_affinities({"s_bad": "UUT_X"})
    assert "s_bad" in str(excinfo.value)
    assert "UUT_0" in str(excinfo.value) and "UUT_1" in str(excinfo.value)

    # 'any' 永远合法；校验失败时不留下半套注册
    scheduler.register_uut_affinities({"s_ok": "any"})
    assert scheduler._uut_affinities == {"s_ok": "any"}


# ---------------------------------------------------------------------------
# 与 ResourceLock 的组合 —— 亲和门不得绕过资源锁
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resource_lock_not_bypassed_when_uut_free() -> None:
    """UUT 空闲但共享仪器资源被占 → 不派发（ResourceLock 仍然生效）。"""
    manager = UUTManager(uut_ids=["UUT_1"])  # UUT_1 空闲
    rm = ResourceManager()
    assert rm.acquire("DMM_CH1", "other_step")

    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager, rm)
    condition = Condition(resource_available=["DMM_CH1"])
    scheduler._registry.register("s_pin", condition)
    scheduler.compile_plan([("s_pin", condition)])
    scheduler.register_uut_affinities({"s_pin": "UUT_1"})

    await scheduler._emergency_scan()
    assert _started(bus) == [], "资源被占时即使 UUT 空闲也不得派发"

    rm.release("DMM_CH1", "other_step")
    await scheduler._emergency_scan()
    assert _started(bus) == ["s_pin"]


@pytest.mark.asyncio
async def test_busy_uut_blocks_even_with_free_resources() -> None:
    """资源空闲但所指 UUT 忙 → 不派发（两道门同时要求）。"""
    manager = UUTManager(uut_ids=["UUT_1"])
    manager.get("UUT_1").start_test()
    rm = ResourceManager()  # DMM_CH1 从未被占

    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager, rm)
    condition = Condition(resource_available=["DMM_CH1"])
    scheduler._registry.register("s_pin", condition)
    scheduler.compile_plan([("s_pin", condition)])
    scheduler.register_uut_affinities({"s_pin": "UUT_1"})

    await scheduler._emergency_scan()
    assert _started(bus) == [], "UUT 忙时即使资源空闲也不得派发"

    manager.release("UUT_1")
    await scheduler._emergency_scan()
    assert _started(bus) == ["s_pin"]


# ---------------------------------------------------------------------------
# 响应式派发路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reactive_dispatch_path_gates_affinity() -> None:
    """_dispatch_step（响应式路径）同样过亲和门。"""
    manager = UUTManager(uut_ids=["UUT_1"])
    manager.get("UUT_1").start_test()

    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register("s_pin")
    scheduler.register_uut_affinities({"s_pin": "UUT_1"})

    await scheduler._dispatch_step("s_pin")
    assert _started(bus) == [], "响应式路径不得绕过亲和门"
    assert "s_pin" not in scheduler._notified_ready

    manager.release("UUT_1")
    await scheduler._dispatch_step("s_pin")
    assert _started(bus) == ["s_pin"]
