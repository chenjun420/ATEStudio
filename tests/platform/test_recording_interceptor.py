"""RecordingInterceptor JSONL 事件录制测试（T10，§7.9 record/replay 基础）。

覆盖：事件种类覆盖、相对时间戳单调、seq 严格递增、flush 节奏、
原子 finalize、崩溃安全（部分文件可读）、密钥脱敏、EventBus 订阅。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from ate_platform.simulation.recording import RecordingInterceptor


class _FakeBus:
    """确定性假 EventBus：记录订阅并允许同步派发（无 asyncio 循环）。"""

    def __init__(self) -> None:
        self.subs: dict[object, list[object]] = {}

    def subscribe(self, event_type: object, callback: object) -> None:
        self.subs.setdefault(event_type, []).append(callback)

    def dispatch(self, type_value: str, data: dict[str, object]) -> None:
        event = SimpleNamespace(type=SimpleNamespace(value=type_value), data=data)
        for cb in self.subs.get(None, []):
            cb(event)


# ---------------------------------------------------------------------------
# 事件种类覆盖
# ---------------------------------------------------------------------------


def test_instrument_call_event_recorded(tmp_path):
    path = tmp_path / "rec.jsonl"
    rec = RecordingInterceptor(path, execution_id="exec-1")
    rec.record_instrument_call(
        resource="PSU_MAIN",
        method="query",
        args=["*IDN?"],
        kwargs={"timeout": 2.0},
        result="CHROMA,62012P,v1",
        elapsed_ms=1.5,
    )
    rec.finalize()

    events = RecordingInterceptor.load(path)
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "instrument_call"
    assert ev["resource"] == "PSU_MAIN"
    assert ev["method"] == "query"
    assert ev["args"] == ["*IDN?"]
    assert ev["kwargs"] == {"timeout": 2.0}
    assert ev["result"] == "CHROMA,62012P,v1"
    assert ev["error"] is None


def test_instrument_call_error_recorded(tmp_path):
    path = tmp_path / "rec.jsonl"
    rec = RecordingInterceptor(path)
    rec.record_instrument_call(
        resource="DMM_1", method="measure_voltage", args=[], kwargs={}, error="timeout"
    )
    rec.finalize()

    (ev,) = RecordingInterceptor.load(path)
    assert ev["kind"] == "instrument_call"
    assert ev["error"] == "timeout"
    assert ev["result"] is None


def test_step_lifecycle_events_recorded(tmp_path):
    path = tmp_path / "rec.jsonl"
    rec = RecordingInterceptor(path)
    rec.record_step_started("step_a")
    rec.record_step_completed("step_a")
    rec.record_step_failed("step_b", error="assert 3.3 < 3.0")
    rec.finalize()

    events = RecordingInterceptor.load(path)
    assert [e["kind"] for e in events] == [
        "step_started",
        "step_completed",
        "step_failed",
    ]
    assert [e["step_id"] for e in events] == ["step_a", "step_a", "step_b"]
    assert events[2]["error"] == "assert 3.3 < 3.0"


def test_variable_change_event_recorded(tmp_path):
    path = tmp_path / "rec.jsonl"
    rec = RecordingInterceptor(path)
    rec.record_variable_change(scope="global", key="dut_serial", value="SN-001")
    rec.finalize()

    (ev,) = RecordingInterceptor.load(path)
    assert ev["kind"] == "variable_change"
    assert ev["scope"] == "global"
    assert ev["key"] == "dut_serial"
    assert ev["value"] == "SN-001"


# ---------------------------------------------------------------------------
# 时间戳与序号
# ---------------------------------------------------------------------------


def test_relative_timestamp_monotonic(tmp_path):
    ticks = iter([100.0, 100.5, 101.0, 101.0, 102.25])
    path = tmp_path / "rec.jsonl"
    rec = RecordingInterceptor(path, clock=lambda: next(ticks))
    for i in range(5):
        rec.record_step_started(f"s{i}")
    rec.finalize()

    events = RecordingInterceptor.load(path)
    ts = [e["t"] for e in events]
    assert all(isinstance(t, float) for t in ts)
    assert ts == sorted(ts), f"timestamps not monotonic: {ts}"
    assert ts[0] == 0.0  # 相对会话起点
    assert ts[-1] == 2.25


def test_seq_strictly_increasing(tmp_path):
    path = tmp_path / "rec.jsonl"
    rec = RecordingInterceptor(path)
    for i in range(10):
        rec.record_step_started(f"s{i}")
    rec.finalize()

    seqs = [e["seq"] for e in RecordingInterceptor.load(path)]
    assert seqs == list(range(len(seqs)))
    assert all(b > a for a, b in zip(seqs, seqs[1:], strict=False))


# ---------------------------------------------------------------------------
# flush 节奏与原子 finalize
# ---------------------------------------------------------------------------


def test_flush_cadence_writes_every_n_events(tmp_path):
    path = tmp_path / "rec.jsonl"
    rec = RecordingInterceptor(path, flush_every=3)
    for i in range(4):
        rec.record_step_started(f"s{i}")

    # 未 finalize：tmp 文件应已落盘 header + 前 3 条（第 4 条仍在缓冲）。
    tmp = path.with_name(path.name + ".tmp")
    assert tmp.exists()
    lines = [ln for ln in tmp.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 4  # header + 3 flushed
    assert len(rec._buffer) == 1  # 无界缓冲守卫：剩余 1 条待刷

    rec.finalize()


def test_finalize_atomic_rename(tmp_path):
    path = tmp_path / "rec.jsonl"
    rec = RecordingInterceptor(path)
    rec.record_step_started("s1")
    final = rec.finalize()

    assert final == path
    assert path.exists()
    assert not path.with_name(path.name + ".tmp").exists()
    lines = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    assert lines[0]["kind"] == "recording_header"
    assert lines[0]["execution_id"] == rec.execution_id
    assert sum(1 for r in lines if r["kind"] != "recording_header") == 1


def test_crash_safe_partial_file_readable(tmp_path):
    """模拟中途被杀：只写 tmp、从不 finalize —— 已落盘前缀必须可解析。"""
    path = tmp_path / "rec.jsonl"
    rec = RecordingInterceptor(path, flush_every=2)
    for i in range(5):
        rec.record_instrument_call(resource="DMM", method="query", args=[i], kwargs={})
    # 模拟撕裂的最后一行（进程在写入中途被杀）。
    with path.with_name(path.name + ".tmp").open("a", encoding="utf-8") as f:
        f.write('{"kind": "step_star')

    events = RecordingInterceptor.load(path.with_name(path.name + ".tmp"))
    assert len(events) == 4  # header 之后已刷盘的 4 条完整事件
    assert all(e["kind"] == "instrument_call" for e in events)


def test_load_skips_malformed_lines(tmp_path):
    path = tmp_path / "rec.jsonl"
    good = json.dumps({"kind": "step_started", "seq": 0, "t": 0.0, "step_id": "a"})
    path.write_text(good + "\nnot-json{{{\n" + good + "\n", encoding="utf-8")
    assert len(RecordingInterceptor.load(path)) == 2


# ---------------------------------------------------------------------------
# 头部与 200 事件回放场景
# ---------------------------------------------------------------------------


def test_header_replayable(tmp_path):
    path = tmp_path / "rec.jsonl"
    rec = RecordingInterceptor(path, execution_id="run-42")
    rec.record_step_started("s")
    rec.finalize()

    header = RecordingInterceptor.read_header(path)
    assert header["kind"] == "recording_header"
    assert header["version"] == 1
    assert header["execution_id"] == "run-42"
    assert "started_at" in header


def test_200_event_run_produces_valid_replayable_jsonl(tmp_path):
    path = tmp_path / "rec.jsonl"
    rec = RecordingInterceptor(path, execution_id="big-run", flush_every=100)
    for i in range(200):
        rec.record_instrument_call(
            resource=f"RES{i % 3}",
            method="query",
            args=[f"MEAS?{i}"],
            kwargs={},
            result=i,
            elapsed_ms=0.1,
        )
    rec.finalize()

    events = RecordingInterceptor.load(path)
    assert len(events) == 200
    assert all({"seq", "t", "kind", "resource", "method"} <= set(e) for e in events)
    # T11 将按 (resource, method) 索引调用——此处验证可分组性。
    by_pair: dict[tuple[str, str], int] = {}
    for e in events:
        by_pair[(e["resource"], e["method"])] = (
            by_pair.get((e["resource"], e["method"]), 0) + 1
        )
    assert sum(by_pair.values()) == 200


# ---------------------------------------------------------------------------
# 密钥脱敏
# ---------------------------------------------------------------------------


def test_secrets_redacted_in_kwargs_and_values(tmp_path):
    path = tmp_path / "rec.jsonl"
    rec = RecordingInterceptor(path)
    rec.record_instrument_call(
        resource="GW",
        method="write",
        args=[],
        kwargs={"password": "hunter2", "api_key": "abc", "level": 5},
    )
    rec.record_variable_change(
        scope="global",
        key="creds",
        value={"token": "t0psecret", "user": "op"},
    )
    rec.finalize()

    call_ev, var_ev = RecordingInterceptor.load(path)
    assert call_ev["kwargs"]["password"] == "[REDACTED]"
    assert call_ev["kwargs"]["api_key"] == "[REDACTED]"
    assert call_ev["kwargs"]["level"] == 5
    assert var_ev["value"]["token"] == "[REDACTED]"
    assert var_ev["value"]["user"] == "op"


# ---------------------------------------------------------------------------
# EventBus 订阅（同步假总线，无循环依赖）
# ---------------------------------------------------------------------------


def test_eventbus_subscription_records_mapped_kinds(tmp_path):
    path = tmp_path / "rec.jsonl"
    bus = _FakeBus()
    rec = RecordingInterceptor(path, execution_id="bus-run")
    rec.subscribe(bus)

    bus.dispatch("STEP_STARTED", {"step_id": "s1"})
    bus.dispatch("STEP_COMPLETED", {"step_id": "s1"})
    bus.dispatch("STEP_FAILED", {"step_id": "s2", "error": "boom"})
    bus.dispatch("UNRELATED_EVENT", {"noise": True})  # 应被忽略
    rec.finalize()

    events = RecordingInterceptor.load(path)
    assert [e["kind"] for e in events] == [
        "step_started",
        "step_completed",
        "step_failed",
    ]
    assert all(e["execution_id"] == "bus-run" for e in events)
