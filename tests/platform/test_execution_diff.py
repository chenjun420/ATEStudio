"""ExecutionDiff.compare 结构化摘要测试（T12，§7.9.3 差异对比）。

覆盖：同录制零差异、步骤增删/状态迁移、测量值容差（默认相对 1e-9、
自定义容差）、时序 delta（逐步 + 总时长）、资源调用计数差异、变量变更、
JSON 可序列化、design-doc 风格 dict 载荷（type/resource_id/duration_ms 别名）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ate_platform.simulation.diff import ExecutionDiff
from ate_platform.simulation.recording import RecordingInterceptor


class _TickClock:
    """确定性时钟：每次读取前进一个 tick；advance() 只推时间不产生事件。"""

    def __init__(self, tick: float = 0.1) -> None:
        self._t = 0.0
        self._tick = tick

    def __call__(self) -> float:
        t = self._t
        self._t += self._tick
        return t

    def advance(self, ticks: int = 1) -> None:
        self._t += self._tick * ticks


def _record(path: Path, execution_id: str, plan: Any) -> list[dict[str, Any]]:
    """按 plan(rec, clock) 录制一段会话并 load 回事件列表（确定性时间线）。"""
    clock = _TickClock()
    rec = RecordingInterceptor(path, execution_id=execution_id, clock=clock)
    plan(rec, clock)
    rec.finalize()
    return RecordingInterceptor.load(path)


# ---------------------------------------------------------------------------
# 同录制 → 全空差异 + match:true
# ---------------------------------------------------------------------------


def _identical_plan(rec: Any, clock: Any) -> None:
    rec.record_step_started("s1")
    clock.advance(4)
    rec.record_step_completed("s1")
    rec.record_instrument_call("DMM_1", "measure_voltage", result=3.3, elapsed_ms=2.0)
    rec.record_variable_change("global", "vset", 5.0)


def test_identical_recordings_all_sections_empty_and_match(tmp_path):
    a = _record(tmp_path / "a.jsonl", "exec-a", _identical_plan)
    b = _record(tmp_path / "b.jsonl", "exec-b", _identical_plan)

    summary = ExecutionDiff.compare(a, b)

    assert summary["match"] is True
    assert summary["steps"] == {"added": [], "removed": [], "status_changed": []}
    assert summary["measurements"] == []
    assert summary["timing"] == {"total": None, "steps": []}
    assert summary["resources"] == []
    assert summary["variables"]["changed"] == []


def test_empty_streams_match(tmp_path):
    a = _record(tmp_path / "a.jsonl", "exec-a", lambda rec, clock: None)
    b = _record(tmp_path / "b.jsonl", "exec-b", lambda rec, clock: None)
    summary = ExecutionDiff.compare(a, b)
    assert summary["match"] is True
    assert summary["timing"]["total"] is None


# ---------------------------------------------------------------------------
# 步骤：新增 / 删除 / 状态迁移
# ---------------------------------------------------------------------------


def test_step_added(tmp_path):
    a = _record(tmp_path / "a.jsonl", "a", lambda rec, clock: rec.record_step_completed("s1"))
    b = _record(
        tmp_path / "b.jsonl",
        "b",
        lambda rec, clock: (rec.record_step_completed("s1"), rec.record_step_completed("s2")),
    )
    summary = ExecutionDiff.compare(a, b)
    assert summary["steps"]["added"] == ["s2"]
    assert summary["steps"]["removed"] == []
    assert summary["match"] is False


def test_step_removed(tmp_path):
    a = _record(
        tmp_path / "a.jsonl",
        "a",
        lambda rec, clock: (rec.record_step_completed("s1"), rec.record_step_failed("s2", error="x")),
    )
    b = _record(tmp_path / "b.jsonl", "b", lambda rec, clock: rec.record_step_completed("s1"))
    summary = ExecutionDiff.compare(a, b)
    assert summary["steps"]["removed"] == ["s2"]
    assert summary["steps"]["added"] == []


def test_step_status_changed(tmp_path):
    def plan_a(rec: Any, clock: Any) -> None:
        rec.record_step_started("s1")
        rec.record_step_completed("s1")

    def plan_b(rec: Any, clock: Any) -> None:
        rec.record_step_started("s1")
        rec.record_step_failed("s1", error="limit exceeded")

    summary = ExecutionDiff.compare(
        _record(tmp_path / "a.jsonl", "a", plan_a), _record(tmp_path / "b.jsonl", "b", plan_b)
    )
    assert summary["steps"]["status_changed"] == [
        {"step_id": "s1", "a": "passed", "b": "failed"}
    ]
    assert summary["match"] is False


# ---------------------------------------------------------------------------
# 测量值：容差语义（默认相对 1e-9；自定义容差生效）
# ---------------------------------------------------------------------------


def _measurement_plan(value: float) -> Any:
    def plan(rec: Any, clock: Any) -> None:
        rec.record("measurement", step_id="s1", name="vout", value=value)

    return plan


def test_measurement_within_default_tolerance_not_reported(tmp_path):
    a = _record(tmp_path / "a.jsonl", "a", _measurement_plan(3.3))
    # 相对偏差 1e-12 << 默认 1e-9：不报告
    b = _record(tmp_path / "b.jsonl", "b", _measurement_plan(3.3 * (1 + 1e-12)))
    summary = ExecutionDiff.compare(a, b)
    assert summary["measurements"] == []
    assert summary["match"] is True


def test_measurement_outside_tolerance_flagged_with_delta(tmp_path):
    a = _record(tmp_path / "a.jsonl", "a", _measurement_plan(3.3))
    b = _record(tmp_path / "b.jsonl", "b", _measurement_plan(3.4))
    summary = ExecutionDiff.compare(a, b)
    (entry,) = summary["measurements"]
    assert entry["key"] == "s1:vout"
    assert entry["a"] == pytest.approx(3.3)
    assert entry["b"] == pytest.approx(3.4)
    assert entry["delta"] == pytest.approx(0.10000000000000053)
    assert summary["match"] is False


def test_custom_tolerance_honored_both_directions(tmp_path):
    a = _record(tmp_path / "a.jsonl", "a", _measurement_plan(100.0))
    b = _record(tmp_path / "b.jsonl", "b", _measurement_plan(100.001))  # 相对偏差 1e-5

    tight = ExecutionDiff.compare(a, b, tolerance=1e-9)
    loose = ExecutionDiff.compare(a, b, tolerance=1e-3)

    assert len(tight["measurements"]) == 1  # 紧容差：标记
    assert loose["measurements"] == []  # 松容差：通过
    assert loose["match"] is True


def test_relative_tolerance_is_scale_invariant(tmp_path):
    # 同一相对偏差在不同量纲下结论一致（纯相对语义）
    for base in (1.0, 1000.0):
        a = _record(tmp_path / f"a{base}.jsonl", "a", _measurement_plan(base))
        b = _record(tmp_path / f"b{base}.jsonl", "b", _measurement_plan(base * (1 + 1e-6)))
        assert len(ExecutionDiff.compare(a, b)["measurements"]) == 1
        assert ExecutionDiff.compare(a, b, tolerance=1e-3)["measurements"] == []


def test_measurement_from_numeric_instrument_call_results(tmp_path):
    def plan(rec: Any, clock: Any) -> None:
        rec.record_instrument_call("DMM_1", "measure_voltage", result=3.3)
        rec.record_instrument_call("DMM_1", "measure_voltage", result=3.31)

    def plan_b(rec: Any, clock: Any) -> None:
        rec.record_instrument_call("DMM_1", "measure_voltage", result=9.9)
        rec.record_instrument_call("DMM_1", "measure_voltage", result=3.31)

    summary = ExecutionDiff.compare(
        _record(tmp_path / "a.jsonl", "a", plan), _record(tmp_path / "b.jsonl", "b", plan_b)
    )
    (entry,) = summary["measurements"]
    assert entry["key"] == "DMM_1.measure_voltage#0"  # 按出现序号配对
    assert entry["delta"] == pytest.approx(6.6)


# ---------------------------------------------------------------------------
# 时序：逐步时长 delta + 总时长
# ---------------------------------------------------------------------------


def test_timing_delta_per_step_and_total(tmp_path):
    def plan_a(rec: Any, clock: Any) -> None:
        rec.record_step_started("s1")
        clock.advance(2)  # 完成时读到 0.3s → 300ms
        rec.record_step_completed("s1")

    def plan_b(rec: Any, clock: Any) -> None:
        rec.record_step_started("s1")
        clock.advance(7)  # 完成时读到 0.8s → 800ms
        rec.record_step_completed("s1")

    summary = ExecutionDiff.compare(
        _record(tmp_path / "a.jsonl", "a", plan_a), _record(tmp_path / "b.jsonl", "b", plan_b)
    )
    assert summary["timing"]["steps"] == [
        {"step_id": "s1", "a_ms": pytest.approx(300.0), "b_ms": pytest.approx(800.0),
         "delta_ms": pytest.approx(500.0)}
    ]
    total = summary["timing"]["total"]
    assert total is not None
    assert total["delta_ms"] == pytest.approx(500.0)
    assert summary["match"] is False


# ---------------------------------------------------------------------------
# 资源：调用计数差异
# ---------------------------------------------------------------------------


def test_resource_call_count_diff(tmp_path):
    def plan_a(rec: Any, clock: Any) -> None:
        rec.record_instrument_call("PSU_MAIN", "set_voltage", result=None)
        rec.record_instrument_call("DMM_1", "measure_voltage", result=3.3)

    def plan_b(rec: Any, clock: Any) -> None:
        rec.record_instrument_call("PSU_MAIN", "set_voltage", result=None)
        rec.record_instrument_call("PSU_MAIN", "set_voltage", result=None)
        rec.record_instrument_call("DMM_1", "measure_current", result=0.5)

    summary = ExecutionDiff.compare(
        _record(tmp_path / "a.jsonl", "a", plan_a), _record(tmp_path / "b.jsonl", "b", plan_b)
    )
    assert summary["resources"] == [
        {"resource": "DMM_1", "method": "measure_current", "a_count": 0, "b_count": 1},
        {"resource": "DMM_1", "method": "measure_voltage", "a_count": 1, "b_count": 0},
        {"resource": "PSU_MAIN", "method": "set_voltage", "a_count": 1, "b_count": 2},
    ]


# ---------------------------------------------------------------------------
# 变量：变更键 old/new（含新增/删除键）
# ---------------------------------------------------------------------------


def test_variable_change_old_new_reported(tmp_path):
    def plan_a(rec: Any, clock: Any) -> None:
        rec.record_variable_change("global", "mode", "fast")
        rec.record_variable_change("global", "retry", 3)

    def plan_b(rec: Any, clock: Any) -> None:
        rec.record_variable_change("global", "mode", "slow")  # 值变更
        rec.record_variable_change("global", "extra", True)  # B 新增键
        # A 的 retry 键在 B 缺失 → 删除键

    summary = ExecutionDiff.compare(
        _record(tmp_path / "a.jsonl", "a", plan_a), _record(tmp_path / "b.jsonl", "b", plan_b)
    )
    assert summary["variables"]["changed"] == [
        {"scope": "global", "key": "extra", "old": None, "new": True},
        {"scope": "global", "key": "mode", "old": "fast", "new": "slow"},
        {"scope": "global", "key": "retry", "old": 3, "new": None},
    ]
    assert summary["match"] is False


# ---------------------------------------------------------------------------
# 输入形态兼容 + JSON 安全
# ---------------------------------------------------------------------------


def test_accepts_design_doc_dict_payloads_with_type_key():
    """无 RecordingInterceptor 的裸 dict 流（type/resource_id/duration_ms/name+new）。"""
    exec_a = [
        {"type": "step_started", "t": 0.0, "step_id": "s1"},
        {"type": "instrument_call", "t": 0.1, "resource_id": "DMM", "method": "read", "result": 1.0,
         "duration_ms": 5.0},
        {"type": "step_completed", "t": 0.2, "step_id": "s1"},
        {"type": "variable_change", "t": 0.3, "name": "k", "old": 1, "new": 2},
    ]
    exec_b = [
        {"type": "step_started", "t": 0.0, "step_id": "s1"},
        {"type": "instrument_call", "t": 0.1, "resource_id": "DMM", "method": "read", "result": 2.0,
         "duration_ms": 8.0},
        {"type": "step_completed", "t": 0.5, "step_id": "s1"},
        {"type": "variable_change", "t": 0.6, "name": "k", "old": 1, "new": 9},
    ]

    summary = ExecutionDiff.compare(exec_a, exec_b)

    # 两侧调用计数相同 → resources 段为空（只报差异）
    assert summary["resources"] == []
    assert summary["measurements"][0]["key"] == "DMM.read#0"
    assert summary["timing"]["steps"][0]["delta_ms"] == pytest.approx(300.0)
    assert summary["variables"]["changed"] == [{"scope": "", "key": "k", "old": 2, "new": 9}]
    assert summary["match"] is False


def test_summary_is_json_serializable(tmp_path):
    a = _record(tmp_path / "a.jsonl", "a", _identical_plan)
    b = _record(
        tmp_path / "b.jsonl",
        "b",
        lambda rec, clock: (
            rec.record_step_started("s1"),
            rec.record_step_failed("s1", error="boom"),
            rec.record_instrument_call("DMM_1", "measure_voltage", result=9.9),
            rec.record_variable_change("global", "vset", 12.0),
        ),
    )
    summary = ExecutionDiff.compare(a, b)
    round_trip = json.loads(json.dumps(summary))
    assert round_trip == summary
    assert round_trip["match"] is False


def test_invalid_inputs_rejected():
    with pytest.raises(TypeError):
        ExecutionDiff.compare(["not-a-dict"], [])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        ExecutionDiff.compare([], [], tolerance=-1.0)
