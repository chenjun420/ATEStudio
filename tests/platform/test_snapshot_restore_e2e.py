"""Snapshot restore end-to-end（§6.6 崩溃恢复，T16 / AC-11）。

驱动 ScannerScheduler 走完一次真实的崩溃-恢复生命周期：

    运行 → 执行中途落快照 → 弃用实例（模拟崩溃）→ 全新调度器实例
    恢复步骤状态 + 变量 → instrument_reset_callback 发 *RST →
    仅剩余步骤执行且各恰好一次，已完成步骤绝不重跑。

与 tests/unit/scheduler/test_state_snapshot.py 互补：那边覆盖文件层
（原子写/损坏容错/清理）与手工构造的快照；这里全部快照都由真实步骤
执行经 EventBus 状态事件链路产生，验证生产接线。

崩溃建模：实例 1 在 s5 完成后停止（s6..s8 仍 PENDING，非全终态 →
stop() 保留快照）。worker 侧用 permitted 集合做确定性闸门 —— 未许可
的 STEP_STARTED 一律不执行，与事件投递竞态无关。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ate_platform.proxy.proxy_manager import ProxyManager
from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
from ate_platform.scheduler.event_bus import EventBus
from ate_platform.scheduler.jetstream_worker import (
    JetStreamWorker,
    PlanBootstrapper,
    make_instrument_reset_callback,
)
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
from ate_platform.scheduler.state_snapshot import StateSnapshot
from ate_platform.scheduler.step_registry import StepRegistry
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import Condition
from shared.dsl import YamlPlan, YamlStep
from shared.events import EventType
from shared.types import StepStatus

PLAN_STEPS = [f"s{i}" for i in range(1, 9)]  # s1..s8 串行链
INSTRUMENTS = ["dmm1", "psu1", "eload1"]


# ---------------------------------------------------------------------------
# 测试装置
# ---------------------------------------------------------------------------


class InstrumentRack:
    """模拟仪器机架：instrument_reset_callback 的 *RST 落点。"""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.commands: list[str] = []
        self.reset_count = 0

    async def reset_all(self) -> None:
        """崩溃恢复回调：对每台仪器发 *RST。"""
        self.reset_count += 1
        for name in self.names:
            self.commands.append(f"{name}: *RST")


class ExecutionWorker:
    """JetStreamWorker 的测试替身：消费 STEP_STARTED → 执行 → 上报。

    permitted 是确定性闸门：不在集合内的 step_id 收到派发也不执行
    （保持 PENDING），用于精确控制"崩溃时刻"哪些步骤已执行。
    stall_running 中的步骤只置 RUNNING 不置终态（模拟执行中崩溃）。
    """

    def __init__(
        self,
        event_bus: EventBus,
        registry: StepRegistry,
        variable_space: VariableSpace,
        permitted: set[str],
        run_counts: dict[str, int] | None = None,
        stall_running: set[str] | None = None,
    ) -> None:
        self.registry = registry
        self.variable_space = variable_space
        self.permitted = set(permitted)
        self.run_counts: dict[str, int] = run_counts if run_counts is not None else {}
        self.stall_running = set(stall_running or ())
        event_bus.subscribe(EventType.STEP_STARTED, self._on_step_started)

    def _on_step_started(self, event: Any) -> None:
        step_id = event.data.get("step_id")
        if step_id not in self.permitted:
            return  # 崩溃闸门：不执行，状态保持 PENDING/RUNNING
        self.run_counts[step_id] = self.run_counts.get(step_id, 0) + 1
        self.registry.update_status(step_id, StepStatus.RUNNING)
        if step_id in self.stall_running:
            return  # 执行中崩溃：永不完成
        # 步骤副作用：写变量（真实 save 链路据此落快照）
        self.variable_space.set(f"steps.{step_id}.executed", True)
        self.variable_space.set("scope.last_executed", step_id)
        self.registry.update_status(step_id, StepStatus.PASSED)


class World:
    """一套完整的调度器装配（对应崩溃后"新进程"的全部内存态）。"""

    def __init__(
        self,
        snapshot_dir: str,
        permitted: set[str],
        rack: InstrumentRack,
        run_counts: dict[str, int],
        stall_running: set[str] | None = None,
    ) -> None:
        self.event_bus = EventBus()
        self.registry = StepRegistry(event_bus=self.event_bus)
        self.variable_space = VariableSpace(event_bus=self.event_bus)
        resource_manager = ResourceManager(event_bus=self.event_bus)
        evaluator = ConditionEvaluator(
            {},
            resource_manager=resource_manager,
            variable_space=self.variable_space,
        )
        self.scheduler = ScannerScheduler(
            event_bus=self.event_bus,
            registry=self.registry,
            evaluator=evaluator,
            variable_space=self.variable_space,
            resource_manager=resource_manager,
            scan_interval=0.05,
            snapshot_dir=snapshot_dir,
            instrument_reset_callback=rack.reset_all,
        )
        pairs: list[tuple[str, Condition | None]] = [("s1", None)]
        for prev, cur in zip(PLAN_STEPS, PLAN_STEPS[1:], strict=False):
            pairs.append((cur, Condition(step=prev, status="PASSED")))
        for step_id, condition in pairs:
            self.registry.register(step_id, condition)
        self.scheduler.compile_plan(pairs)
        self.worker = ExecutionWorker(
            self.event_bus,
            self.registry,
            self.variable_space,
            permitted,
            run_counts=run_counts,
            stall_running=stall_running,
        )

    async def start(self) -> None:
        await self.event_bus.start()
        await self.scheduler.start()

    async def stop(self) -> None:
        await self.scheduler.stop()
        await self.event_bus.stop()


async def _wait_until(pred: Any, description: str, timeout: float = 5.0) -> None:
    """轮询等待事件链路收敛；超时抛 AssertionError。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timeout waiting for: {description}")


async def _crash_mid_run(
    tmp_path: Path,
    rack: InstrumentRack,
    run_counts: dict[str, int],
) -> World:
    """阶段一：跑完 s1..s5 后"崩溃"（弃用实例，快照保留）。"""
    world = World(
        str(tmp_path),
        permitted={"s1", "s2", "s3", "s4", "s5"},
        rack=rack,
        run_counts=run_counts,
    )
    await world.start()
    await _wait_until(
        lambda: world.registry.get_status("s5") == StepStatus.PASSED,
        "s5 PASSED (crash point)",
    )
    # 此刻 s6 可能已被派发（STEP_STARTED 已发），但 worker 闸门保证其
    # 未被执行 —— 状态仍 PENDING，与真实"执行到第 5 步崩溃"等价。
    await world.stop()  # 非全终态 → 快照保留（断点续跑语义）
    return world


# ---------------------------------------------------------------------------
# E2E 用例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_mid_run_preserves_resumable_snapshot(tmp_path: Path) -> None:
    """崩溃点快照内容正确：s1..s5 PASSED、s6..s8 PENDING、可恢复。"""
    rack = InstrumentRack(INSTRUMENTS)
    world = await _crash_mid_run(tmp_path, rack, {})

    snap = StateSnapshot(str(tmp_path))
    assert snap.snapshot_path.exists(), "非全终态 stop 必须保留快照"
    assert snap.can_resume()

    data = snap.load()
    assert data is not None
    states = data["step_states"]
    assert {k: v for k, v in states.items() if k in PLAN_STEPS[:5]} == {
        "s1": "PASSED",
        "s2": "PASSED",
        "s3": "PASSED",
        "s4": "PASSED",
        "s5": "PASSED",
    }
    assert all(states[sid] == "PENDING" for sid in PLAN_STEPS[5:])
    # 新鲜运行不得触发仪器重置（*RST 只属于崩溃恢复）
    assert rack.reset_count == 0
    assert world.scheduler.get_status()["resumed_from_snapshot"] is False


@pytest.mark.asyncio
async def test_restore_executes_only_remaining_steps(tmp_path: Path) -> None:
    """核心 AC：恢复后仅 s6..s8 各执行一次；s1..s5 绝不重跑。"""
    rack = InstrumentRack(INSTRUMENTS)
    run_counts: dict[str, int] = {}
    await _crash_mid_run(tmp_path, rack, run_counts)
    pre_crash_counts = dict(run_counts)
    assert pre_crash_counts == {f"s{i}": 1 for i in range(1, 6)}

    # 全新实例（新 EventBus/registry/VariableSpace —— 模拟新进程）
    world2 = World(
        str(tmp_path),
        permitted={"s6", "s7", "s8"},
        rack=rack,
        run_counts=run_counts,
    )
    await world2.start()
    assert world2.scheduler.get_status()["resumed_from_snapshot"] is True
    await _wait_until(
        lambda: all(
            world2.registry.get_status(sid) == StepStatus.PASSED
            for sid in PLAN_STEPS
        ),
        "all 8 steps PASSED after resume",
    )
    await world2.stop()

    # 每步全程恰好执行一次：完成的没重跑，剩余的补齐
    assert run_counts == {f"s{i}": 1 for i in range(1, 9)}
    # 全终态 stop → 快照清理
    snap = StateSnapshot(str(tmp_path))
    assert not snap.snapshot_path.exists(), "完成后应清理快照"
    assert not snap.can_resume()


@pytest.mark.asyncio
async def test_restore_sends_rst_to_all_instruments(tmp_path: Path) -> None:
    """*RST 回调只在恢复时触发一次，且逐台仪器下发。"""
    rack = InstrumentRack(INSTRUMENTS)
    run_counts: dict[str, int] = {}
    crashed = await _crash_mid_run(tmp_path, rack, run_counts)
    assert rack.reset_count == 0, "运行中不重置"

    world2 = World(
        str(tmp_path),
        permitted={"s6", "s7", "s8"},
        rack=rack,
        run_counts=run_counts,
    )
    await world2.start()  # start() 内部 _maybe_resume → reset callback
    assert rack.reset_count == 1, "恢复时必须且只重置一次"
    assert rack.commands == [f"{name}: *RST" for name in INSTRUMENTS]
    await _wait_until(
        lambda: crashed.scheduler is not None
        and all(
            world2.registry.get_status(sid) == StepStatus.PASSED
            for sid in PLAN_STEPS
        ),
        "resume completes",
    )
    await world2.stop()
    assert rack.reset_count == 1, "后续正常执行不再触发重置"


@pytest.mark.asyncio
async def test_restore_recovers_variables(tmp_path: Path) -> None:
    """崩溃前写入的变量经真实 save→load 链路在新实例中可见。"""
    rack = InstrumentRack(INSTRUMENTS)
    run_counts: dict[str, int] = {}
    await _crash_mid_run(tmp_path, rack, run_counts)

    world2 = World(
        str(tmp_path),
        permitted={"s6", "s7", "s8"},
        rack=rack,
        run_counts=run_counts,
    )
    await world2.start()
    # start() 同步完成恢复 —— 变量立即可读
    assert world2.variable_space.get("scope.last_executed") == "s5"
    assert world2.variable_space.get("steps.s3.executed") is True
    await _wait_until(
        lambda: all(
            world2.registry.get_status(sid) == StepStatus.PASSED
            for sid in PLAN_STEPS
        ),
        "resume completes",
    )
    # 恢复的变量继续参与后续执行（s8 执行后覆盖为最新值）
    assert world2.variable_space.get("scope.last_executed") == "s8"
    await world2.stop()


@pytest.mark.asyncio
async def test_corrupted_snapshot_tolerated_with_fresh_start(tmp_path: Path) -> None:
    """QA-failure 场景：快照损坏 → warning 容忍 → 从零全新开始。"""
    snap = StateSnapshot(str(tmp_path))
    snap.snapshot_path.write_text("{corrupted json!", encoding="utf-8")
    assert not snap.can_resume()

    rack = InstrumentRack(INSTRUMENTS)
    run_counts: dict[str, int] = {}
    world = World(
        str(tmp_path),
        permitted=set(PLAN_STEPS),
        rack=rack,
        run_counts=run_counts,
    )
    await world.start()
    assert world.scheduler.get_status()["resumed_from_snapshot"] is False
    assert rack.reset_count == 0, "全新开始不算崩溃恢复"
    await _wait_until(
        lambda: all(
            world.registry.get_status(sid) == StepStatus.PASSED
            for sid in PLAN_STEPS
        ),
        "fresh full run completes",
    )
    await world.stop()
    assert run_counts == {f"s{i}": 1 for i in range(1, 9)}, "8 步全部真实执行"


@pytest.mark.asyncio
async def test_uninterrupted_run_cleans_snapshot_on_completion(tmp_path: Path) -> None:
    """无崩溃对照：一口气跑完全部步骤 → stop 清理快照、不可恢复。"""
    rack = InstrumentRack(INSTRUMENTS)
    run_counts: dict[str, int] = {}
    world = World(
        str(tmp_path),
        permitted=set(PLAN_STEPS),
        rack=rack,
        run_counts=run_counts,
    )
    await world.start()
    await _wait_until(
        lambda: all(
            world.registry.get_status(sid) == StepStatus.PASSED
            for sid in PLAN_STEPS
        ),
        "full run completes",
    )
    await world.stop()

    snap = StateSnapshot(str(tmp_path))
    assert not snap.snapshot_path.exists()
    assert not snap.can_resume()
    assert run_counts == {f"s{i}": 1 for i in range(1, 9)}
    assert rack.reset_count == 0


@pytest.mark.asyncio
async def test_running_step_reexecutes_exactly_once_after_crash(tmp_path: Path) -> None:
    """§6.6 RUNNING 回退语义：执行中的 s6 崩溃 → 恢复为 PENDING 重跑，
    且只补跑一次；s7/s8 不受影响。"""
    rack = InstrumentRack(INSTRUMENTS)
    run_counts: dict[str, int] = {}

    # s6 允许启动但永远不完成（执行中崩溃）
    world1 = World(
        str(tmp_path),
        permitted={"s1", "s2", "s3", "s4", "s5", "s6"},
        rack=rack,
        run_counts=run_counts,
        stall_running={"s6"},
    )
    await world1.start()
    await _wait_until(
        lambda: world1.registry.get_status("s6") == StepStatus.RUNNING,
        "s6 stuck RUNNING (mid-execution crash)",
    )
    await world1.stop()

    data = StateSnapshot(str(tmp_path)).load()
    assert data is not None
    assert data["step_states"]["s6"] == "RUNNING"

    world2 = World(
        str(tmp_path),
        permitted={"s6", "s7", "s8"},
        rack=rack,
        run_counts=run_counts,
    )
    await world2.start()
    await _wait_until(
        lambda: all(
            world2.registry.get_status(sid) == StepStatus.PASSED
            for sid in PLAN_STEPS
        ),
        "all steps PASSED after resume",
    )
    await world2.stop()

    # s1..s5 恰好一次；s6 被重新执行（RUNNING 视为未完成）；s7/s8 补齐
    assert run_counts == {
        "s1": 1,
        "s2": 1,
        "s3": 1,
        "s4": 1,
        "s5": 1,
        "s6": 2,  # 崩溃时执行中断 → 恢复后重跑一次
        "s7": 1,
        "s8": 1,
    }
    assert world2.variable_space.get("scope.last_executed") == "s8"


# ---------------------------------------------------------------------------
# Worker 级（RH-4）：PlanBootstrapper / JetStreamWorker 恢复链回调接线
#
# T16 缺口：ScannerScheduler 支持 instrument_reset_callback，但 worker 侧
# PlanBootstrapper 从未传入 —— 崩溃恢复时 *RST 永远不会到达仪器。
# 这里沿生产装配链（PlanBootstrapper → ScannerScheduler._maybe_resume）
# 验证回调接线；不启动调度器扫描循环（无闸门计划经真实执行器会挂起），
# 直接调用 §6.6 恢复入口 _maybe_resume（start() 内部同款调用点）。
# ---------------------------------------------------------------------------


class RecordingProxyManager:
    """ProxyManager 测试替身：记录每台仪器的 reset() 调用。

    fail_on 中的 resource_id 在 reset() 时抛 RuntimeError（模拟代理停止/
    仪器不可达），用于验证容错语义。
    """

    def __init__(
        self,
        resource_ids: list[str],
        fail_on: set[str] | None = None,
    ) -> None:
        self.config: dict[str, Any] = {
            "instruments": {rid: {"type": "MOCK"} for rid in resource_ids}
        }
        self.resets: list[str] = []
        self.fail_on = set(fail_on or ())

    def client(self, resource_id: str, timeout: float = 30.0) -> Any:
        manager = self

        class _Client:
            def reset(self) -> None:
                if resource_id in manager.fail_on:
                    raise RuntimeError(f"Instrument proxy stopped [{resource_id}]")
                manager.resets.append(resource_id)

        return _Client()


def _resumable_snapshot(snapshot_dir: str) -> None:
    """预置一份可恢复快照（模拟上次运行崩溃遗留）。"""
    StateSnapshot(snapshot_dir).save(
        {
            "step_states": {"s1": "PASSED", "s2": "PENDING"},
            "variables": {"scope": {}, "steps": {}, "loop": {}},
            "timestamp": 0.0,
        }
    )


def _two_step_plan() -> YamlPlan:
    return YamlPlan(
        name="rh4-restore",
        version="1.0",
        steps=[
            YamlStep(id="s1", script="noop.py"),
            YamlStep(id="s2", script="noop.py", preconditions=["s1"]),
        ],
    )


@pytest.mark.asyncio
async def test_bootstrapper_wires_reset_callback_through_restore_chain(
    tmp_path: Path,
) -> None:
    """生产装配链：PlanBootstrapper 传入的回调在恢复时对每台仪器发 *RST。"""
    _resumable_snapshot(str(tmp_path))
    rack = InstrumentRack(INSTRUMENTS)

    bootstrapper = PlanBootstrapper(
        _two_step_plan(),
        snapshot_dir=str(tmp_path),
        instrument_reset_callback=rack.reset_all,
    )
    bootstrapper.bootstrap()  # 注册步骤 + 编译计划（不启动扫描循环）
    assert rack.reset_count == 0, "构造期不得触发 *RST"

    await bootstrapper.event_bus.start()
    try:
        await bootstrapper.scheduler._maybe_resume()  # noqa: SLF001 — §6.6 恢复入口
        assert rack.reset_count == 1, "恢复时必须且只触发一次"
        assert rack.commands == [f"{name}: *RST" for name in INSTRUMENTS]
        assert bootstrapper.scheduler.get_status()["resumed_from_snapshot"] is True
        assert (
            bootstrapper.step_registry.get_status("s1") == StepStatus.PASSED
        ), "步骤状态随恢复链还原"
    finally:
        await bootstrapper.event_bus.stop()


@pytest.mark.asyncio
async def test_bootstrapper_without_callback_keeps_legacy_restore(
    tmp_path: Path,
) -> None:
    """不传回调参数 = 既有行为：恢复照常完成，调度器回调为 None。"""
    _resumable_snapshot(str(tmp_path))

    bootstrapper = PlanBootstrapper(_two_step_plan(), snapshot_dir=str(tmp_path))
    bootstrapper.bootstrap()  # 注册步骤 + 编译计划（不启动扫描循环）
    assert bootstrapper.scheduler._instrument_reset_callback is None  # noqa: SLF001

    await bootstrapper.event_bus.start()
    try:
        await bootstrapper.scheduler._maybe_resume()  # noqa: SLF001
        assert bootstrapper.scheduler.get_status()["resumed_from_snapshot"] is True
        assert bootstrapper.step_registry.get_status("s1") == StepStatus.PASSED
    finally:
        await bootstrapper.event_bus.stop()


@pytest.mark.asyncio
async def test_worker_reset_callback_resets_each_managed_instrument_once() -> None:
    """worker 构建的回调：对代理配置中的每台仪器恰好发一次 reset。"""
    stub = RecordingProxyManager(list(INSTRUMENTS))
    callback = make_instrument_reset_callback(stub)  # type: ignore[arg-type]

    await callback()

    assert stub.resets == sorted(INSTRUMENTS), "每台受管仪器各复位一次"


@pytest.mark.asyncio
async def test_worker_reset_callback_tolerates_unavailable_instruments() -> None:
    """单台仪器不可达 → warning 继续，其余仪器照常复位，绝不抛异常。"""
    stub = RecordingProxyManager(
        list(INSTRUMENTS),
        fail_on={"psu1", "eload1"},
    )
    callback = make_instrument_reset_callback(stub)  # type: ignore[arg-type]

    await callback()  # 不应抛出

    assert stub.resets == ["dmm1"], "可达仪器仍被复位，失败仪器被跳过"


@pytest.mark.asyncio
async def test_worker_reset_callback_tolerates_empty_proxy_config() -> None:
    """代理无任何受管仪器 → warning 后直接返回（恢复不被阻断）。"""
    stub = RecordingProxyManager([])
    callback = make_instrument_reset_callback(stub)  # type: ignore[arg-type]

    await callback()

    assert stub.resets == []


def test_worker_without_proxy_manager_passes_no_callback(tmp_path: Path) -> None:
    """未配置代理的 worker：构建结果为 None → 调度器保持既有无回调行为。"""
    worker = JetStreamWorker(snapshot_dir=str(tmp_path))
    assert worker._build_instrument_reset_callback() is None  # noqa: SLF001

    configured = JetStreamWorker(
        snapshot_dir=str(tmp_path),
        proxy_manager=RecordingProxyManager(list(INSTRUMENTS)),  # type: ignore[arg-type]
    )
    assert callable(configured._build_instrument_reset_callback())  # noqa: SLF001


@pytest.mark.asyncio
async def test_inline_proxy_rst_reaches_mock_instruments(tmp_path: Path) -> None:
    """集成冒烟：回调经真实 inline ProxyManager/InstrumentClient 到达 mock 驱动。

    *RST 效果在 mock 驱动上不可观测，故验证：回调全程无异常 + 复位后
    仪器仍可正常 query（IPC 链路未被破坏）。
    """
    manager = ProxyManager(
        {"instruments": {"DMM_CH1": {"type": "DMM"}}},
        simulation=True,
        log_dir=str(tmp_path),
    )
    manager.start()
    try:
        client = manager.client("DMM_CH1")
        client.connect("MOCK::DMM")

        callback = make_instrument_reset_callback(manager)
        await callback()  # 经 IPC 对 DMM_CH1 发送复位指令

        response = client.query("MEAS:VOLT:DC?")
        assert float(response) > 0, "复位后仪器仍可正常测量"
    finally:
        manager.stop()
