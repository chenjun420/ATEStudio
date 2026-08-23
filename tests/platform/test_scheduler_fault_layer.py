"""调度层故障注入测试（设计文档 §7.7，v41-gap-analysis 任务 6）。

覆盖：
- FaultInjector.check_scheduler_raise：count 第 n 次派发命中 / probability
  统计带 / once 一次性消耗 / 异常携带 layer+step_id+uut_id 上下文 /
  其他层规则被忽略
- 零开销热路径：无调度层规则时为单次空检查（<2x 裸调用，宽松 CI 余量）
- ScannerScheduler 派发钩子：命中规则 → 步骤按失败处理并携带
  layer=scheduler 归因；不吞非 SchedulerFaultError 异常
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.scheduler.event_bus import EventBus, EventType
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
from ate_platform.scheduler.step_registry import StepRegistry
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.simulation.fault_injector import (
    FaultInjector,
    SchedulerFaultError,
)
from ate_platform.types import StepStatus

# ---------------------------------------------------------------------------
# 注入器层 —— check_scheduler_raise 语义
# ---------------------------------------------------------------------------


def _scheduler_rule(
    fault_id: str,
    target: str = "*",
    trigger: dict[str, Any] | None = None,
    once: bool = False,
) -> dict[str, Any]:
    return {
        "id": fault_id,
        "layer": "scheduler",
        "target": target,
        "trigger": trigger or {"type": "count", "value": 1},
        "once": once,
        "action": {"type": "scheduler_error"},
    }


def test_count_trigger_fires_on_nth_dispatch_only() -> None:
    """count 触发：恰好第 n 次派发命中，前 n-1 次放行。"""
    inj = FaultInjector()
    inj.load([_scheduler_rule("nth", trigger={"type": "count", "value": 3})])

    inj.check_scheduler_raise("step_a")  # 第 1 次：放行
    inj.check_scheduler_raise("step_a")  # 第 2 次：放行
    with pytest.raises(SchedulerFaultError):
        inj.check_scheduler_raise("step_a")  # 第 3 次：命中


def test_probability_trigger_statistical_band() -> None:
    """probability=0.5 在 1000 次派发中落入 [350, 650] 统计带。"""
    inj = FaultInjector(seed=42)
    inj.load([_scheduler_rule("p50", trigger={"type": "probability", "value": 0.5})])

    fired = 0
    for _ in range(1000):
        try:
            inj.check_scheduler_raise("step_a")
        except SchedulerFaultError:
            fired += 1
    assert 350 <= fired <= 650, f"expected ~500 fires, got {fired}"


def test_once_only_rule_consumes_after_first_fire() -> None:
    """once 规则首次命中后失效，后续派发放行。"""
    inj = FaultInjector()
    inj.load([_scheduler_rule("once1", once=True)])

    with pytest.raises(SchedulerFaultError):
        inj.check_scheduler_raise("step_a")
    # 已消耗：任意步骤后续派发均放行
    inj.check_scheduler_raise("step_a")
    inj.check_scheduler_raise("step_b")
    assert inj.rules[0].triggered_count == 1


def test_no_rules_hot_path_under_2x_bare_call() -> None:
    """无调度层规则时热路径为单次空检查：<2x 等价裸调用（宽松余量）。

    基线函数与被测快路径同形（一次属性读 + 整数比较 + 返回），
    且注入器上挂了一条 network 层规则以证明过滤发生在空检查之前。
    """
    inj = FaultInjector()
    inj.load([
        {
            "id": "net_only",
            "layer": "network",
            "target": "*",
            "trigger": {"type": "count", "value": 1},
            "action": {"type": "connection_drop"},
        },
    ])

    class _Gate:
        def __init__(self) -> None:
            self.count = 0

    gate = _Gate()

    def bare(g: _Gate) -> None:
        if g.count == 0:
            return

    n = 50_000
    start = time.perf_counter()
    for _ in range(n):
        bare(gate)
    bare_total = time.perf_counter() - start

    start = time.perf_counter()
    for _ in range(n):
        inj.check_scheduler_raise("step_a")
    hot_total = time.perf_counter() - start

    assert hot_total < bare_total * 2.0, (
        f"hot path {hot_total:.4f}s vs bare {bare_total:.4f}s"
    )


def test_fired_error_carries_layer_step_uut_context() -> None:
    """命中的异常携带 layer='scheduler' + step_id + uut_id 上下文。"""
    inj = FaultInjector()
    inj.load([_scheduler_rule("ctx9", target="step_9")])

    with pytest.raises(SchedulerFaultError) as ei:
        inj.check_scheduler_raise("step_9", "UUT-2")

    exc = ei.value
    assert exc.layer == "scheduler"
    assert exc.target == "step_9"
    assert exc.uut_id == "UUT-2"
    assert exc.fault_id == "ctx9"


def test_other_layer_rule_ignored_by_check_scheduler() -> None:
    """network/instrument 层规则不影响调度层检查（且不进入热路径）。"""
    inj = FaultInjector()
    inj.load([
        {
            "id": "net",
            "layer": "network",
            "target": "step_1",
            "trigger": {"type": "count", "value": 1},
            "action": {"type": "connection_drop"},
        },
        {
            "id": "inst",
            "layer": "instrument",
            "target": "step_1",
            "trigger": {"type": "count", "value": 1},
            "action": {"type": "instrument_error"},
        },
    ])
    inj.check_scheduler_raise("step_1")  # 不抛 —— 调度层无规则


# ---------------------------------------------------------------------------
# 调度器接线 —— 派发前钩子
# ---------------------------------------------------------------------------


class _RecordingEventBus(EventBus):
    """同步记录发布的假总线（不排队、不分发，测试确定性）。"""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[EventType, dict[str, Any]]] = []

    async def publish(self, event_type: EventType, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))


def _make_scheduler(
    bus: EventBus,
    injector: FaultInjector | None = None,
) -> ScannerScheduler:
    return ScannerScheduler(
        event_bus=bus,
        registry=StepRegistry(),
        evaluator=ConditionEvaluator({}, None, None),
        variable_space=VariableSpace(),
        resource_manager=ResourceManager(),
        fault_injector=injector,
    )


@pytest.mark.asyncio
async def test_dispatch_hook_fails_step_with_layer_attribution() -> None:
    """命中调度层规则的步骤在派发点被置 FAILED 并发布 STEP_FAILED。"""
    bus = _RecordingEventBus()
    inj = FaultInjector()
    inj.load([_scheduler_rule("sf1", target="step_a")])
    scheduler = _make_scheduler(bus, inj)
    scheduler._registry.register("step_a")

    await scheduler._dispatch_step("step_a")

    assert scheduler._registry.get_status("step_a") == StepStatus.FAILED
    started = [e for e in bus.events if e[0] == EventType.STEP_STARTED]
    failed = [e for e in bus.events if e[0] == EventType.STEP_FAILED]
    assert not started, "命中注入的步骤不得派发 STEP_STARTED"
    assert len(failed) == 1
    error_text = failed[0][1]["error"]
    assert "layer=scheduler" in error_text
    assert "step_a" in error_text
    assert "sf1" in error_text


@pytest.mark.asyncio
async def test_emergency_scan_hook_fails_firing_step() -> None:
    """看门狗全量扫描路径同样经过故障检查（首个扫描周期即派发的步骤）。"""
    bus = _RecordingEventBus()
    inj = FaultInjector()
    inj.load([_scheduler_rule("sf2", target="step_b")])
    scheduler = _make_scheduler(bus, inj)
    scheduler._registry.register("step_b")

    await scheduler._emergency_scan()

    assert scheduler._registry.get_status("step_b") == StepStatus.FAILED
    assert not [e for e in bus.events if e[0] == EventType.STEP_STARTED]
    assert [e for e in bus.events if e[0] == EventType.STEP_FAILED]


@pytest.mark.asyncio
async def test_dispatch_without_injector_dispatches_normally() -> None:
    """未接注入器的调度器行为不变：正常发布 STEP_STARTED。"""
    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus)
    scheduler._registry.register("step_ok")

    await scheduler._dispatch_step("step_ok")

    assert [e for e in bus.events if e[0] == EventType.STEP_STARTED]


class _ExplodingInjector(FaultInjector):
    """check_scheduler_raise 抛无关异常的注入器（验证不吞异常）。"""

    def check_scheduler_raise(
        self,
        step_id: str,
        uut_id: str | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        raise _FakeAbortedError(f"abort at {step_id}")


class _FakeAbortedError(RuntimeError):
    """ExecutionAborted 尚未引入（任务 4），用本地哨兵验证传播语义。"""


@pytest.mark.asyncio
async def test_hook_does_not_swallow_unrelated_exceptions() -> None:
    """钩子只捕获 SchedulerFaultError，无关异常原样向上传播。"""
    scheduler = _make_scheduler(_RecordingEventBus(), _ExplodingInjector())
    scheduler._registry.register("step_x")

    with pytest.raises(_FakeAbortedError):
        await scheduler._check_scheduler_fault("step_x")
