"""StateSnapshot / 崩溃恢复测试（设计文档 §6.6，任务 #5）。

覆盖：
- StateSnapshot 原子写/加载/可恢复判定/清理/损坏容错
- VariableSpace snapshot/restore round-trip
- ScannerScheduler 崩溃恢复流程：模拟崩溃后重启，恢复步骤状态、
  变量，并触发仪器重置回调（*RST）
- 正常完成时快照被清理；中途停止保留快照
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from shared.types import StepStatus

from ate_platform.scheduler import StateSnapshot, VariableSpace
from ate_platform.scheduler.scanner_scheduler import ScannerScheduler
from ate_platform.scheduler.step_registry import StepRegistry


# ---------------------------------------------------------------------------
# StateSnapshot 组件
# ---------------------------------------------------------------------------


def test_snapshot_save_load_roundtrip(tmp_path: Path) -> None:
    snap = StateSnapshot(str(tmp_path))
    state = {"step_states": {"s1": "PASSED"}, "variables": {"scope": {"v": 1}}}
    snap.save(state)
    assert snap.snapshot_path.exists()
    assert snap.load() == state


def test_snapshot_can_resume_requires_step_states(tmp_path: Path) -> None:
    snap = StateSnapshot(str(tmp_path))
    # 空文件/无 step_states → 不可恢复
    snap.save({"foo": 1})
    assert not snap.can_resume()
    # 有非空 step_states → 可恢复
    snap.save({"step_states": {"s1": "PASSED"}})
    assert snap.can_resume()


def test_snapshot_load_missing_returns_none(tmp_path: Path) -> None:
    snap = StateSnapshot(str(tmp_path))
    assert snap.load() is None
    assert not snap.can_resume()


def test_snapshot_corrupt_file_returns_none(tmp_path: Path) -> None:
    snap = StateSnapshot(str(tmp_path))
    snap.snapshot_path.write_text("{not valid json", encoding="utf-8")
    assert snap.load() is None
    assert not snap.can_resume()


def test_snapshot_cleanup_removes_file(tmp_path: Path) -> None:
    snap = StateSnapshot(str(tmp_path))
    snap.save({"step_states": {"s1": "PASSED"}})
    assert snap.snapshot_path.exists()
    snap.cleanup()
    assert not snap.snapshot_path.exists()
    # 幂等
    snap.cleanup()


# ---------------------------------------------------------------------------
# VariableSpace snapshot / restore
# ---------------------------------------------------------------------------


def test_variable_space_snapshot_restore_roundtrip() -> None:
    vs = VariableSpace()
    vs.set("scope.voltage", 3.3)
    vs.set("scope.current", 1.5)
    vs.set("steps.m1.measure", 42)
    vs.set("loop.li.0.i", 7)

    data = vs.snapshot()
    assert data["scope"] == {"voltage": 3.3, "current": 1.5}
    assert data["steps"] == {"m1": {"measure": 42}}
    assert data["loop"] == {"li": {"0.i": 7}}

    # 新实例恢复后完全一致
    vs2 = VariableSpace()
    vs2.restore(data)
    assert vs2.get("scope.voltage") == 3.3
    assert vs2.get("steps.m1.measure") == 42
    assert vs2.get("loop.li.0.i") == 7


def test_variable_space_restore_overwrites() -> None:
    vs = VariableSpace()
    vs.set("scope.voltage", 1.0)
    vs.restore({"scope": {"voltage": 5.0}})
    assert vs.get("scope.voltage") == 5.0


def test_variable_space_restore_tolerates_bad_structure() -> None:
    vs = VariableSpace()
    vs.set("scope.v", 1)
    vs.restore({"scope": "not-a-dict"})  # 非法结构被忽略，保留现有
    assert vs.get("scope.v") == 1


# ---------------------------------------------------------------------------
# ScannerScheduler 崩溃恢复集成
# ---------------------------------------------------------------------------

# 与既有 scheduler 测试一致的最小依赖装配


def _make_scheduler(tmp_path: Path, reset_calls: list) -> tuple[ScannerScheduler, Any]:
    from ate_platform.scheduler.event_bus import EventBus
    from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
    from ate_platform.scheduler.resource_manager import ResourceManager
    from ate_platform.types import Condition

    event_bus = EventBus()
    registry = StepRegistry(event_bus=event_bus)
    variable_space = VariableSpace(event_bus=event_bus)
    resource_manager = ResourceManager(event_bus=event_bus)
    evaluator = ConditionEvaluator(
        {},
        resource_manager=resource_manager,
        variable_space=variable_space,
    )

    async def reset_all() -> None:
        reset_calls.append("RST")

    scheduler = ScannerScheduler(
        event_bus=event_bus,
        registry=registry,
        evaluator=evaluator,
        variable_space=variable_space,
        resource_manager=resource_manager,
        scan_interval=0.05,
        snapshot_dir=str(tmp_path),
        instrument_reset_callback=reset_all,
    )

    # 注册两个串行步骤：s1 → s2（s2 依赖 s1 PASSED）
    registry.register("s1", None)
    registry.register("s2", Condition(step="s1", status="PASSED"))
    scheduler.compile_plan([("s1", None), ("s2", Condition(step="s1", status="PASSED"))])
    return scheduler, event_bus


@pytest.mark.asyncio
async def test_resume_restores_step_states_and_resets_instruments(
    tmp_path: Path,
) -> None:
    """模拟崩溃：快照中 s1=PASSED、s2=PENDING → 重启后 s1 不重跑，
    且仪器重置回调被调用（*RST）。"""
    # 先手工构造崩溃前快照
    snap = StateSnapshot(str(tmp_path))
    snap.save(
        {
            "step_states": {"s1": "PASSED", "s2": "PENDING"},
            "variables": {"scope": {"voltage": 3.3}},
        }
    )

    reset_calls: list = []
    scheduler, _event_bus = _make_scheduler(tmp_path, reset_calls)

    # registry 恢复后 s1 应为 PASSED（不重跑）
    # 注意：scheduler 构造时注册为 PENDING，start() 会从快照恢复
    assert scheduler.get_status()["snapshot_resumable"] is True
    await scheduler.start()
    await scheduler.stop()

    assert reset_calls == ["RST"], "恢复时必须重置仪器"
    assert scheduler._registry.get_status("s1") == StepStatus.PASSED  # type: ignore[attr-defined]
    assert scheduler._registry.get_status("s2") == StepStatus.PENDING  # type: ignore[attr-defined]
    assert scheduler.get_status()["resumed_from_snapshot"] is True


@pytest.mark.asyncio
async def test_resume_restores_variables(tmp_path: Path) -> None:
    """恢复后变量空间与崩溃前一致。"""
    snap = StateSnapshot(str(tmp_path))
    snap.save(
        {
            "step_states": {"s1": "PASSED", "s2": "PENDING"},
            "variables": {"scope": {"voltage": 5.0, "current": 2.0}},
        }
    )
    scheduler, _event_bus = _make_scheduler(tmp_path, [])
    await scheduler.start()
    assert scheduler._variable_space.get("scope.voltage") == 5.0  # type: ignore[attr-defined]
    assert scheduler._variable_space.get("scope.current") == 2.0  # type: ignore[attr-defined]
    await scheduler.stop()


@pytest.mark.asyncio
async def test_status_change_writes_snapshot(tmp_path: Path) -> None:
    """步骤状态变更后快照自动落盘。"""
    import asyncio

    scheduler, _event_bus = _make_scheduler(tmp_path, [])
    await _event_bus.start()  # 启动投递循环（生产路径 JetStreamWorker 先做）
    await scheduler.start()
    scheduler._registry.update_status("s1", StepStatus.PASSED)
    # publish_sync → queue → _process_events 异步投递，轮询等待快照落盘
    data = None
    for _ in range(20):
        await asyncio.sleep(0.05)
        assert scheduler._snapshot is not None  # type: ignore[union-attr]
        data = scheduler._snapshot.load()  # type: ignore[union-attr]
        if data and data.get("step_states", {}).get("s1") == "PASSED":
            break
    assert data is not None, "handler 应消费事件并写入快照"
    assert data["step_states"]["s1"] == "PASSED"
    await scheduler.stop()
    await _event_bus.stop()


@pytest.mark.asyncio
async def test_no_snapshot_when_disabled(tmp_path: Path) -> None:
    """未启用 snapshot_dir 时完全无快照行为。"""
    from ate_platform.scheduler.event_bus import EventBus
    from ate_platform.scheduler.condition_evaluator import ConditionEvaluator
    from ate_platform.scheduler.resource_manager import ResourceManager

    event_bus = EventBus()
    registry = StepRegistry(event_bus=event_bus)
    scheduler = ScannerScheduler(
        event_bus=event_bus,
        registry=registry,
        evaluator=ConditionEvaluator(
            {},
            resource_manager=ResourceManager(event_bus=event_bus),
            variable_space=VariableSpace(event_bus=event_bus),
        ),
        variable_space=VariableSpace(event_bus=event_bus),
        resource_manager=ResourceManager(event_bus=event_bus),
    )
    assert scheduler.get_status()["snapshot_enabled"] is False


@pytest.mark.asyncio
async def test_graceful_completion_cleans_snapshot(tmp_path: Path) -> None:
    """全部步骤到终态后 stop() 清理快照；否则保留（断点续跑）。"""
    scheduler, _event_bus = _make_scheduler(tmp_path, [])
    await _event_bus.start()
    await scheduler.start()

    # 全部步骤终态：s1、s2 都 PASSED → stop 时清理快照
    scheduler._registry.update_status("s1", StepStatus.PASSED)
    scheduler._registry.update_status("s2", StepStatus.PASSED)
    for _ in range(20):
        await asyncio.sleep(0.05)
        if scheduler._snapshot is not None and scheduler._snapshot.can_resume():  # type: ignore[union-attr]
            break
    await scheduler.stop()
    await _event_bus.stop()
    assert scheduler._snapshot is not None  # type: ignore[union-attr]
    assert not scheduler._snapshot.snapshot_path.exists(), "完成应清理快照"  # type: ignore[union-attr]

    # 中途停止（s2 仍 PENDING）→ 保留快照
    scheduler2, _event_bus2 = _make_scheduler(tmp_path, [])
    await _event_bus2.start()
    await scheduler2.start()
    scheduler2._registry.update_status("s1", StepStatus.PASSED)
    await asyncio.sleep(0.1)  # 等 handler 落盘
    await scheduler2.stop()
    await _event_bus2.stop()
    assert scheduler2._snapshot is not None  # type: ignore[union-attr]
    assert scheduler2._snapshot.snapshot_path.exists(), "未完成应保留快照"  # type: ignore[union-attr]


def test_running_step_falls_back_to_pending(tmp_path: Path) -> None:
    """快照中 RUNNING 步骤视为未完成 → 恢复为 PENDING 重跑。"""
    snap = StateSnapshot(str(tmp_path))
    snap.save({"step_states": {"s1": "RUNNING", "s2": "PASSED"}})
    scheduler, _event_bus = _make_scheduler(tmp_path, [])
    # 手动触发恢复（无需完整 start/stop）
    import asyncio

    asyncio.run(scheduler._maybe_resume())
    assert scheduler._registry.get_status("s1") == StepStatus.PENDING  # type: ignore[attr-defined]
    assert scheduler._registry.get_status("s2") == StepStatus.PASSED  # type: ignore[attr-defined]
