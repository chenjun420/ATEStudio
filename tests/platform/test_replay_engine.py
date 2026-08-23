"""ReplayEngine 严格模式回放测试（T11，§7.9 record/replay，AC-7）。

覆盖：精确回放、实参不匹配（含索引/上下文）、顺序违反、索引耗尽、
kwargs 子集规则、浮点容差、多资源交错、非严格 warn-once、
录制失败忠实重放、录制文件不可变。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from ate_platform.simulation.recording import RecordingInterceptor
from ate_platform.simulation.replay import (
    ReplayEngine,
    ReplayMismatchError,
    ReplayRecordedError,
)

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _make_recording(tmp_path, name="rec.jsonl"):
    """三调用两资源录制：PSU_MAIN.set_voltage → DMM_1.query → PSU_MAIN.query。"""
    path = tmp_path / name
    rec = RecordingInterceptor(path, execution_id="exec-replay-1")
    rec.record_instrument_call(
        resource="PSU_MAIN",
        method="set_voltage",
        args=[1, 3.3],
        kwargs={"channel": 1},
        result=None,
        elapsed_ms=0.4,
    )
    rec.record_instrument_call(
        resource="DMM_1",
        method="query",
        args=["*IDN?"],
        kwargs={"timeout": 2.0},
        result="MOCK,DMM34401,v1",
        elapsed_ms=1.2,
    )
    rec.record_instrument_call(
        resource="PSU_MAIN",
        method="query",
        args=["VOLT?"],
        kwargs={},
        result="3.300000",
        elapsed_ms=0.9,
    )
    rec.finalize()
    return path


# ---------------------------------------------------------------------------
# 精确回放（happy path）
# ---------------------------------------------------------------------------


def test_happy_replay_returns_recorded_results(tmp_path):
    path = _make_recording(tmp_path)
    engine = ReplayEngine(recording_path=path)

    assert engine.serve("PSU_MAIN", "set_voltage", [1, 3.3], {"channel": 1}) is None
    assert engine.serve("DMM_1", "query", ["*IDN?"], {"timeout": 2.0}) == "MOCK,DMM34401,v1"
    assert engine.serve("PSU_MAIN", "query", ["VOLT?"], {}) == "3.300000"
    assert engine.all_consumed


def test_exact_match_passes_with_float_args(tmp_path):
    rec = RecordingInterceptor(tmp_path / "r.jsonl")
    rec.record_instrument_call(
        resource="DMM", method="measure", args=[3.3, 0.001], kwargs={}, result=42.0
    )
    rec.finalize()
    engine = ReplayEngine(recording_path=tmp_path / "r.jsonl")
    assert engine.serve("DMM", "measure", [3.3, 0.001], {}) == 42.0


def test_construct_from_events_list_directly():
    events = [
        {
            "kind": "instrument_call",
            "seq": 0,
            "t": 0.0,
            "resource": "R",
            "method": "m",
            "args": [1],
            "kwargs": {},
            "result": "ok",
            "error": None,
            "elapsed_ms": 0.5,
        }
    ]
    engine = ReplayEngine(events=events)
    assert engine.serve("R", "m", [1], {}) == "ok"


def test_constructor_rejects_ambiguous_input():
    with pytest.raises(ValueError, match="exactly one"):
        ReplayEngine()  # 既无路径也无事件
    with pytest.raises(ValueError, match="exactly one"):
        ReplayEngine(events=[], recording_path="x.jsonl")


# ---------------------------------------------------------------------------
# 实参校验与偏差上下文
# ---------------------------------------------------------------------------


def test_arg_mismatch_raises_with_index_and_context(tmp_path):
    path = _make_recording(tmp_path)
    engine = ReplayEngine(recording_path=path)

    with pytest.raises(ReplayMismatchError) as ei:
        engine.serve("DMM_1", "query", ["MEAS:VOLT?"], {"timeout": 2.0})

    err = ei.value
    assert err.reason == "args_mismatch"
    assert err.resource == "DMM_1"
    assert err.method == "query"
    assert err.call_index == 0  # 该 (resource, method) 对内的第 0 次出现
    assert err.seq == 1  # 全局录制序号
    assert "*IDN?" in str(err) and "MEAS:VOLT?" in str(err)


def test_arg_arity_mismatch_raises(tmp_path):
    path = _make_recording(tmp_path)
    engine = ReplayEngine(recording_path=path)
    with pytest.raises(ReplayMismatchError, match="arity"):
        engine.serve("PSU_MAIN", "set_voltage", [1], {"channel": 1})


def test_out_of_order_interleaving_raises(tmp_path):
    """录制全局序 A(0)-B(1)-A(2)；实况跳过 B 先消费 A(2)，再补 B 时其
    录制 seq=1 已落后于已消费的 max seq=2 ⇒ 顺序违反。"""
    path = _make_recording(tmp_path)
    engine = ReplayEngine(recording_path=path)

    engine.serve("PSU_MAIN", "set_voltage", [1, 3.3], {"channel": 1})  # seq=0
    engine.serve("PSU_MAIN", "query", ["VOLT?"], {})  # seq=2（跳过 seq=1）
    with pytest.raises(ReplayMismatchError) as ei:
        engine.serve("DMM_1", "query", ["*IDN?"], {"timeout": 2.0})  # seq=1 < 2
    assert ei.value.reason == "order_violation"
    assert ei.value.seq == 1


def test_exhausted_index_raises(tmp_path):
    path = _make_recording(tmp_path)
    engine = ReplayEngine(recording_path=path)
    engine.serve("DMM_1", "query", ["*IDN?"], {"timeout": 2.0})
    with pytest.raises(ReplayMismatchError) as ei:
        engine.serve("DMM_1", "query", ["*IDN?"], {"timeout": 2.0})
    assert ei.value.reason == "exhausted"
    assert ei.value.call_index == 1


def test_unknown_resource_method_raises_exhausted(tmp_path):
    path = _make_recording(tmp_path)
    engine = ReplayEngine(recording_path=path)
    with pytest.raises(ReplayMismatchError, match="never recorded|exhausted"):
        engine.serve("SCOPE_9", "autoset", [], {})


# ---------------------------------------------------------------------------
# kwargs 子集规则与脱敏感知
# ---------------------------------------------------------------------------


def test_kwargs_missing_recorded_key_raises(tmp_path):
    path = _make_recording(tmp_path)
    engine = ReplayEngine(recording_path=path)
    with pytest.raises(ReplayMismatchError, match="channel"):
        engine.serve("PSU_MAIN", "set_voltage", [1, 3.3], {})


def test_kwargs_extra_live_keys_tolerated(tmp_path):
    """子集规则：录制键必须全部出现且相等；实况多余键放行（前向兼容）。"""
    path = _make_recording(tmp_path)
    engine = ReplayEngine(recording_path=path)
    assert (
        engine.serve("PSU_MAIN", "set_voltage", [1, 3.3], {"channel": 1, "ovp": True})
        is None
    )


def test_kwargs_value_mismatch_raises(tmp_path):
    path = _make_recording(tmp_path)
    engine = ReplayEngine(recording_path=path)
    with pytest.raises(ReplayMismatchError) as ei:
        engine.serve("DMM_1", "query", ["*IDN?"], {"timeout": 99.0})
    assert ei.value.reason == "kwargs_mismatch"
    assert "timeout" in str(ei.value)


def test_redacted_kwarg_skips_comparison(tmp_path):
    """录制侧密钥已脱敏为 [REDACTED]——无法比对，回放时跳过该字段。"""
    rec = RecordingInterceptor(tmp_path / "sec.jsonl")
    rec.record_instrument_call(
        resource="GW", method="login", args=[], kwargs={"password": "hunter2"}, result=True
    )
    rec.finalize()

    engine = ReplayEngine(recording_path=tmp_path / "sec.jsonl")
    # 回放侧给真实口令——若比对 [REDACTED] 会误报。
    assert engine.serve("GW", "login", [], {"password": "real-secret"}) is True


# ---------------------------------------------------------------------------
# 浮点容差
# ---------------------------------------------------------------------------


def test_float_within_tolerance_passes_and_outside_fails(tmp_path):
    rec = RecordingInterceptor(tmp_path / "tol.jsonl")
    rec.record_instrument_call(resource="D", method="m", args=[3.3], kwargs={}, result=1)
    rec.finalize()
    events = RecordingInterceptor.load(rec.path)

    tol_engine = ReplayEngine(events=events, float_tolerance=1e-3)
    assert tol_engine.serve("D", "m", [3.3000001], {}) == 1

    strict_engine = ReplayEngine(events=events)
    with pytest.raises(ReplayMismatchError):
        strict_engine.serve("D", "m", [3.5], {})


# ---------------------------------------------------------------------------
# 多资源交错与非严格模式
# ---------------------------------------------------------------------------


def test_multi_resource_interleaving_served_independently(tmp_path):
    path = tmp_path / "inter.jsonl"
    rec = RecordingInterceptor(path)
    for i in range(4):
        res = "A" if i % 2 == 0 else "B"
        rec.record_instrument_call(
            resource=res, method="ping", args=[i], kwargs={}, result=f"pong-{i}"
        )
    rec.finalize()

    engine = ReplayEngine(recording_path=path)
    got = [
        engine.serve("A", "ping", [0], {}),
        engine.serve("B", "ping", [1], {}),
        engine.serve("A", "ping", [2], {}),
        engine.serve("B", "ping", [3], {}),
    ]
    assert got == ["pong-0", "pong-1", "pong-2", "pong-3"]
    assert engine.pending("A", "ping") == 0
    assert engine.pending("B", "ping") == 0


def test_non_strict_warns_once_and_continues(tmp_path):
    path = _make_recording(tmp_path)
    engine = ReplayEngine(recording_path=path, strict=False)

    with pytest.warns(UserWarning, match="ReplayEngine") as warned:
        first = engine.serve("DMM_1", "query", ["WRONG"], {"timeout": 2.0})  # 偏差1：warn
        second = engine.serve("PSU_MAIN", "set_voltage", [9, 9.9], {"channel": 1})  # 偏差2：静默

    assert len(warned) == 1, f"expected exactly one warning, got {len(warned)}"
    assert first == "MOCK,DMM34401,v1"  # 非严格仍返回录制结果
    assert second is None  # set_voltage 录制结果本就是 None


# ---------------------------------------------------------------------------
# 忠实重放录制失败 + 时间/耗时来自录制
# ---------------------------------------------------------------------------


def test_recorded_error_replayed_as_replay_recorded_error(tmp_path):
    rec = RecordingInterceptor(tmp_path / "err.jsonl")
    rec.record_instrument_call(
        resource="DMM", method="measure", args=[], kwargs={}, error="timeout after 2s"
    )
    rec.finalize()

    engine = ReplayEngine(recording_path=tmp_path / "err.jsonl")
    with pytest.raises(ReplayRecordedError, match="timeout after 2s"):
        engine.serve("DMM", "measure", [], {})


def test_elapsed_and_timestamp_replayed_from_recording(tmp_path):
    path = _make_recording(tmp_path)
    engine = ReplayEngine(recording_path=path)
    engine.serve("DMM_1", "query", ["*IDN?"], {"timeout": 2.0})

    served = engine.last_served
    assert served["elapsed_ms"] == 1.2  # 来自录制，无真实 I/O 计时
    assert served["execution_id"] == "exec-replay-1"


# ---------------------------------------------------------------------------
# 录制不可变约束
# ---------------------------------------------------------------------------


def test_replay_does_not_mutate_recording_file(tmp_path):
    path = _make_recording(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    engine = ReplayEngine(recording_path=path)
    engine.serve("PSU_MAIN", "set_voltage", [1, 3.3], {"channel": 1})
    engine.serve("DMM_1", "query", ["*IDN?"], {"timeout": 2.0})

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    # JSONL 结构完好可再解析。
    assert len(json.loads(path.read_text(encoding="utf-8").splitlines()[0])) > 0
