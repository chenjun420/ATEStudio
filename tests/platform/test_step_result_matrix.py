"""超时/重试/on_failure 决策矩阵测试（v41-gap-analysis 任务 4）。

审计维度：{timeout hit, retry exhausted, on_failure abort|continue|skip}
× {passed, failed}。

背景事实（§6.4 决策矩阵）：
- 步骤超时由执行器映射为 StepStatus.ERROR（ThreadStepExecutor 捕获
  TimeoutError → ERROR），因此 "timeout hit" 与普通错误共用重试分支；
- 测量失败为 StepStatus.FAILED，走 repeat 分支；
- PASSED 清零 retry/repeat 计数；
- 终态失败（重试/重复耗尽或无策略）按 StepExecutionConfig.on_failure 处置：
  abort → 抛 ExecutionAborted；skip → 标记 SKIPPED 继续；
  continue（默认）→ 保持既有终态（向后兼容）。

全部确定性：无真实 sleep 依赖（retry_delay 用 monkeypatch 记录参数）、
屏障缺员用忙态 UUT 天然建模、事件经同步 Recording 总线内联路由。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ate_platform.exceptions import ExecutionAborted
from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.scheduler.event_bus import EventBus, EventType, get_event_category
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
from ate_platform.scheduler.step_registry import StepExecutionConfig, StepRegistry
from ate_platform.scheduler.uut_sync import UUTManager, UUTState
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import StepStatus


class _RecordingEventBus(EventBus):
    """同步记录发布的假总线（不排队；publish_sync 内联回调订阅者）。"""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[EventType, dict[str, Any]]] = []

    async def publish(self, event_type: EventType, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))

    def publish_sync(self, event_type: EventType, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))
        from ate_platform.scheduler.event_bus import Event

        event = Event(
            type=event_type, data=data, category=get_event_category(event_type)
        )
        for callback in list(self._subscribers.get(event_type, [])):
            callback(event)


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


def _register(
    scheduler: ScannerScheduler,
    step_id: str = "step1",
    **config_kwargs: Any,
) -> StepRegistry:
    scheduler._registry.register(step_id, config=StepExecutionConfig(**config_kwargs))
    return scheduler._registry


# ---------------------------------------------------------------------------
# 维度 A：timeout hit —— 执行器把超时映射为 ERROR，进入重试分支
# ---------------------------------------------------------------------------


class TestTimeoutHitDimension:
    """timeout hit × {retry available, passed, exhausted-default}。"""

    @pytest.mark.asyncio
    async def test_timeout_error_retries_when_budget_remains(self) -> None:
        """超时(ERROR)且 max_retries>0：置回 PENDING 重试，计数递增。"""
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        registry = _register(scheduler, max_retries=2, retry_delay_ms=0)

        result = await scheduler.handle_step_result("step1", StepStatus.ERROR)

        assert result is True
        assert registry.get_retry_count("step1") == 1
        assert registry.get_status("step1") == StepStatus.PENDING

    @pytest.mark.asyncio
    async def test_timeout_error_honors_retry_delay_ms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """retry_delay_ms>0：重试前按配置延迟（记录 sleep 参数，不真等）。"""
        delays: list[float] = []
        real_sleep = asyncio.sleep

        async def recording_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
            delays.append(delay)
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", recording_sleep)

        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        _register(scheduler, max_retries=1, retry_delay_ms=250)

        result = await scheduler.handle_step_result("step1", StepStatus.ERROR)

        assert result is True
        assert delays == [0.25], "必须按 retry_delay_ms/1000 延迟后再重试"

    @pytest.mark.asyncio
    async def test_timeout_then_passed_resets_counters(self) -> None:
        """timeout hit 后转 PASS：清零计数（不影响后续循环执行），返回 False。

        与生产调用方一致（如 _run_barrier_step）：调用方先写终态
        PASSED，再交 handle_step_result 做计数/处置决策。
        """
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        registry = _register(scheduler, max_retries=2, retry_delay_ms=0)

        assert await scheduler.handle_step_result("step1", StepStatus.ERROR) is True
        registry.update_status("step1", StepStatus.PASSED)
        result = await scheduler.handle_step_result("step1", StepStatus.PASSED)

        assert result is False
        assert registry.get_retry_count("step1") == 0
        assert registry.get_status("step1") == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_retry_exhausted_default_continues_in_error(self) -> None:
        """重试耗尽 + 默认 on_failure=continue：保持 ERROR、返回 False（既有行为）。"""
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        registry = _register(scheduler, max_retries=1, retry_delay_ms=0)

        assert await scheduler.handle_step_result("step1", StepStatus.ERROR) is True
        result = await scheduler.handle_step_result("step1", StepStatus.ERROR)

        assert result is False
        assert registry.get_status("step1") == StepStatus.ERROR


# ---------------------------------------------------------------------------
# 维度 B：on_failure 处置 —— 重试耗尽（ERROR / timeout 路径）
# ---------------------------------------------------------------------------


class TestOnFailureOnRetryExhausted:
    """on_failure ∈ {abort, skip, continue} × 重试耗尽。"""

    @pytest.mark.asyncio
    async def test_retry_exhausted_on_failure_abort_raises_execution_aborted(
        self,
    ) -> None:
        """QA 场景（failure）：重试耗尽后 on_failure=abort 抛 ExecutionAborted。"""
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        registry = _register(
            scheduler, max_retries=1, retry_delay_ms=0, on_failure="abort"
        )

        assert await scheduler.handle_step_result("step1", StepStatus.ERROR) is True

        with pytest.raises(ExecutionAborted, match="step1"):
            await scheduler.handle_step_result("step1", StepStatus.ERROR)
        # 状态在抛出前已落为终态 ERROR（崩溃恢复语义一致）
        assert registry.get_status("step1") == StepStatus.ERROR

    @pytest.mark.asyncio
    async def test_retry_exhausted_on_failure_skip_marks_skipped(self) -> None:
        """重试耗尽 + on_failure=skip：步骤标记 SKIPPED，不重派（False）。"""
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        registry = _register(
            scheduler, max_retries=1, retry_delay_ms=0, on_failure="skip"
        )

        assert await scheduler.handle_step_result("step1", StepStatus.ERROR) is True
        result = await scheduler.handle_step_result("step1", StepStatus.ERROR)

        assert result is False
        assert registry.get_status("step1") == StepStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_on_failure_continue_explicit_matches_legacy(self) -> None:
        """显式 on_failure='continue' 与默认行为逐字节一致（向后兼容锚点）。"""
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        registry = _register(
            scheduler, max_retries=1, retry_delay_ms=0, on_failure="continue"
        )

        assert await scheduler.handle_step_result("step1", StepStatus.ERROR) is True
        result = await scheduler.handle_step_result("step1", StepStatus.ERROR)

        assert result is False
        assert registry.get_status("step1") == StepStatus.ERROR
        assert registry.get_retry_count("step1") == 1  # 未再递增


# ---------------------------------------------------------------------------
# 维度 C：测量失败 —— repeat 策略（FAILED 路径）
# ---------------------------------------------------------------------------


class TestMeasurementFailDimension:
    """FAILED × {无策略, limit 内重复, force_repeat, passed}。"""

    @pytest.mark.asyncio
    async def test_failed_without_repeat_policy_returns_false(self) -> None:
        """未启用 repeat_on_measurement_fail：FAILED 即终态候选，返回 False。"""
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        registry = _register(scheduler)

        result = await scheduler.handle_step_result("step1", StepStatus.FAILED)

        assert result is False
        assert registry.get_status("step1") == StepStatus.FAILED

    @pytest.mark.asyncio
    async def test_failed_repeats_until_limit_then_exhausts(self) -> None:
        """repeat_limit=2：两次重复后第三次 FAILED 耗尽，保持 FAILED。"""
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        registry = _register(
            scheduler, repeat_on_measurement_fail=True, repeat_limit=2
        )

        r1 = await scheduler.handle_step_result("step1", StepStatus.FAILED)
        r2 = await scheduler.handle_step_result("step1", StepStatus.FAILED)
        r3 = await scheduler.handle_step_result("step1", StepStatus.FAILED)

        assert (r1, r2) == (True, True)
        assert registry.get_repeat_count("step1") == 2
        assert r3 is False
        assert registry.get_status("step1") == StepStatus.FAILED

    @pytest.mark.asyncio
    async def test_force_repeat_ignores_repeat_limit(self) -> None:
        """force_repeat=True 无视 repeat_limit 持续重复（人工介入后重测语义）。"""
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        registry = _register(
            scheduler,
            repeat_on_measurement_fail=True,
            repeat_limit=1,
            force_repeat=True,
        )

        results = [
            await scheduler.handle_step_result("step1", StepStatus.FAILED)
            for _ in range(3)
        ]

        assert results == [True, True, True]
        assert registry.get_repeat_count("step1") == 3
        assert registry.get_status("step1") == StepStatus.PENDING

    @pytest.mark.asyncio
    async def test_passed_after_repeat_resets_counters(self) -> None:
        """重复后转 PASS：repeat 计数清零。"""
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        registry = _register(
            scheduler, repeat_on_measurement_fail=True, repeat_limit=2
        )

        assert await scheduler.handle_step_result("step1", StepStatus.FAILED) is True
        registry.update_status("step1", StepStatus.PASSED)
        result = await scheduler.handle_step_result("step1", StepStatus.PASSED)

        assert result is False
        assert registry.get_repeat_count("step1") == 0


# ---------------------------------------------------------------------------
# 维度 D：on_failure 处置 —— 重复耗尽（FAILED 路径）
# ---------------------------------------------------------------------------


class TestOnFailureOnRepeatExhausted:
    """on_failure ∈ {abort, skip} × 重复耗尽。"""

    @pytest.mark.asyncio
    async def test_repeat_exhausted_on_failure_abort_raises_execution_aborted(
        self,
    ) -> None:
        """重复耗尽 + on_failure=abort：抛 ExecutionAborted。"""
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        registry = _register(
            scheduler,
            repeat_on_measurement_fail=True,
            repeat_limit=1,
            on_failure="abort",
        )

        assert await scheduler.handle_step_result("step1", StepStatus.FAILED) is True

        with pytest.raises(ExecutionAborted, match="step1"):
            await scheduler.handle_step_result("step1", StepStatus.FAILED)
        assert registry.get_status("step1") == StepStatus.FAILED

    @pytest.mark.asyncio
    async def test_repeat_exhausted_on_failure_skip_marks_skipped(self) -> None:
        """重复耗尽 + on_failure=skip：标记 SKIPPED 继续。"""
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        registry = _register(
            scheduler,
            repeat_on_measurement_fail=True,
            repeat_limit=1,
            on_failure="skip",
        )

        assert await scheduler.handle_step_result("step1", StepStatus.FAILED) is True
        result = await scheduler.handle_step_result("step1", StepStatus.FAILED)

        assert result is False
        assert registry.get_status("step1") == StepStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_failed_no_repeat_policy_with_abort_raises_immediately(
        self,
    ) -> None:
        """无 repeat 策略时 FAILED 即终态：on_failure=abort 立即生效。"""
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        _register(scheduler, on_failure="abort")

        with pytest.raises(ExecutionAborted, match="step1"):
            await scheduler.handle_step_result("step1", StepStatus.FAILED)


# ---------------------------------------------------------------------------
# 维度 E：守卫与配置校验
# ---------------------------------------------------------------------------


class TestGuardsAndValidation:
    """未注册步骤 / 非终态输入 / 非法 on_failure 值。"""

    @pytest.mark.asyncio
    async def test_unregistered_step_returns_false(self) -> None:
        """未注册步骤：KeyError 守卫 → False，不抛出。"""
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)

        result = await scheduler.handle_step_result("ghost", StepStatus.FAILED)

        assert result is False

    @pytest.mark.asyncio
    async def test_non_terminal_status_returns_false(self) -> None:
        """非终态输入（如 PENDING）：不属于任何决策分支 → False 且状态不变。"""
        bus = _RecordingEventBus()
        scheduler = _make_scheduler(bus)
        registry = _register(scheduler, max_retries=1)

        result = await scheduler.handle_step_result("step1", StepStatus.PENDING)

        assert result is False
        assert registry.get_retry_count("step1") == 0

    def test_invalid_on_failure_value_rejected_at_config(self) -> None:
        """非法 on_failure 值在构造 StepExecutionConfig 时即拒绝（fail-fast）。"""
        with pytest.raises(ValueError, match="on_failure"):
            StepExecutionConfig(on_failure="explode")


# ---------------------------------------------------------------------------
# 端到端：屏障超时 × on_failure=abort 经调度路径传播（不被吞掉）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_barrier_timeout_abort_propagates_through_emergency_scan() -> None:
    """T3 屏障流保持可用：全忙缺员快速失败 → 决策矩阵 → abort 向上传播。

    全池皆忙 → wait_barrier 免等待直接缺员失败（确定性，无 sleep）→
    STEP_FAILED 经 handle_step_result 路由 → 无 repeat 策略即终态 →
    on_failure=abort 抛 ExecutionAborted 穿透 _emergency_scan。
    """
    manager = UUTManager(uut_ids=["UUT_0", "UUT_1"])
    manager.get("UUT_0").start_test()
    manager.get("UUT_1").start_test()
    bus = _RecordingEventBus()
    scheduler = _make_scheduler(bus, manager)
    scheduler._registry.register(
        "b_abort", config=StepExecutionConfig(on_failure="abort")
    )
    scheduler.register_barrier_steps({"b_abort": "sync1"}, default_timeout=30.0)

    with pytest.raises(ExecutionAborted, match="b_abort"):
        await scheduler._emergency_scan()

    # 缺员 UUT 在抛出前已被标记 FAILED（屏障语义不受 abort 影响）
    assert all(u.state == UUTState.FAILED for u in manager.uuts.values())
