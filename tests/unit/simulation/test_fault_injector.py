"""四层故障注入引擎测试（设计文档 §7.7，任务 #6）。

覆盖：
- 五种触发方式：count / probability / time / condition / state
- ≥8 种故障类型：timeout / instrument_error / value_override / delay /
  bus_error / connection_drop / truncated_data / checksum_error /
  scpi_error / scheduler_error
- target/method 过滤（* 通配）
- once 一次性规则
- YAML DSL load 解析（§7.7.2）
- InstrumentSimulator 端到端挂接（网络/协议/仪器层）
- 调度层 check_scheduler
"""

from __future__ import annotations

import time

import pytest

from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.simulation.fault_injector import (
    FaultAction,
    FaultInjectionError,
    FaultInjector,
    FaultRule,
    InstrumentFaultError,
    NetworkFaultError,
    ProtocolFaultError,
    SchedulerFaultError,
)
from ate_platform.simulation.instrument_simulator import InstrumentSimulator


class _SimDriver(BaseDriver):
    """最小 SIM 驱动（V3.2 双基类签名）。"""

    def query(self, command: str, delay: float | None = None) -> str:  # noqa: PLW0221
        return "1.500000E+00"

    def write(self, command: str) -> None:
        pass

    def read(self) -> str:
        return "1.5"

    def connect(self, address: str) -> None:
        pass

    def disconnect(self) -> None:
        pass

    @property
    def is_connected(self) -> bool:
        return True


def _make_sim(injector: FaultInjector | None = None) -> InstrumentSimulator:
    return InstrumentSimulator(_SimDriver(), "DMM", injector=injector)


# ---------------------------------------------------------------------------
# FaultRule / 触发方式
# ---------------------------------------------------------------------------


def test_trigger_count_reaches_threshold() -> None:
    rule = FaultRule(
        fault_id="r", layer="instrument", target="DMM",
        trigger={"type": "count", "value": 3},
        action={"type": "instrument_error"},
    )
    assert not rule.matches({"call_count": 2})
    # 第 3 次起命中
    assert rule.matches({"call_count": 3})
    assert rule.matches({"call_count": 5})
    assert rule.triggered_count == 2


def test_trigger_probability() -> None:
    # 概率 1.0 必命中
    rule = FaultRule(
        fault_id="r", layer="network", target="*",
        trigger={"type": "probability", "value": 1.0},
        action={"type": "packet_loss"},
    )
    assert rule.matches({})
    # 概率 0.0 必不命中
    rule0 = FaultRule(
        fault_id="r0", layer="network", target="*",
        trigger={"type": "probability", "value": 0.0},
        action={"type": "packet_loss"},
    )
    assert not rule0.matches({})


def test_trigger_time() -> None:
    rule = FaultRule(
        fault_id="r", layer="instrument", target="DMM",
        trigger={"type": "time", "after_s": 0.1},
        action={"type": "instrument_error"},
    )
    assert not rule.matches({"elapsed_s": 0.0})
    assert rule.matches({"elapsed_s": 0.2})


def test_trigger_condition_expression() -> None:
    rule = FaultRule(
        fault_id="r", layer="scheduler", target="step_5",
        trigger={"type": "condition", "expression": "step_5_retries > 2"},
        action={"type": "scheduler_error"},
    )
    assert not rule.matches({"step_5_retries": 2})
    assert rule.matches({"step_5_retries": 3})


def test_trigger_state() -> None:
    rule = FaultRule(
        fault_id="r", layer="instrument", target="PSU",
        trigger={"type": "state", "expression": "active_devices > 2"},
        action={"type": "bus_error"},
    )
    assert not rule.matches({"active_devices": 2})
    assert rule.matches({"active_devices": 3})


def test_trigger_condition_invalid_expression_is_safe() -> None:
    """非法表达式不抛异常，视为未命中（不会把 eval 泄漏到运行时）。"""
    rule = FaultRule(
        fault_id="r", layer="instrument", target="DMM",
        trigger={"type": "condition", "expression": "__import__('os').system('x')"},
        action={"type": "instrument_error"},
    )
    assert not rule.matches({})


def test_once_rule_fires_single_time() -> None:
    rule = FaultRule(
        fault_id="once", layer="instrument", target="DMM",
        trigger={"type": "count", "value": 1},
        action={"type": "instrument_error"},
        once=True,
    )
    assert rule.matches({"call_count": 1})
    assert not rule.matches({"call_count": 2}), "once 规则命中后失效"
    assert rule.triggered_count == 1


def test_invalid_layer_rejected() -> None:
    with pytest.raises(ValueError):
        FaultRule("r", layer="bad", target="*", action={"type": "timeout"})
    with pytest.raises(ValueError):
        FaultRule("r", layer="instrument", target="*", action={})
    with pytest.raises(ValueError):
        FaultRule(
            "r", layer="instrument", target="*",
            trigger={"type": "never"}, action={"type": "timeout"},
        )


# ---------------------------------------------------------------------------
# FaultInjector 匹配 / 过滤 / DSL 加载
# ---------------------------------------------------------------------------


def test_injector_target_and_method_filtering() -> None:
    inj = FaultInjector()
    inj.load([
        {"id": "a", "target": "DMM", "trigger": {"type": "count", "value": 1},
         "action": {"type": "timeout"}},
        {"id": "b", "target": "*", "method": "*",
         "trigger": {"type": "count", "value": 1},
         "action": {"type": "bus_error"}},
    ])
    # DMM 命中 a（timeout）
    act = inj.check_instrument("DMM", "query", {"call_count": 1})
    assert act is not None and act.fault_type == "timeout"
    # 其他仪器命中 b（通配）
    act2 = inj.check_instrument("PSU", "query", {"call_count": 1})
    assert act2 is not None and act2.fault_type == "bus_error"


def test_injector_layer_routing() -> None:
    """同 target 不同 layer 独立命中。"""
    inj = FaultInjector()
    inj.load([
        {"id": "net", "layer": "network", "target": "DMM",
         "trigger": {"type": "count", "value": 1},
         "action": {"type": "connection_drop"}},
        {"id": "proto", "layer": "protocol", "target": "DMM",
         "trigger": {"type": "count", "value": 1},
         "action": {"type": "scpi_error", "code": -113}},
    ])
    assert inj.check_network("DMM", "query", {"call_count": 1}).fault_type == "connection_drop"
    assert inj.check_protocol("DMM", "query", {"call_count": 1}).fault_type == "scpi_error"
    # 非目标层不命中
    assert inj.check_scheduler("DMM", "query", {"call_count": 1}) is None


def test_injector_elapsed_context_auto() -> None:
    """elapsed_s 由注入器自动提供。"""
    inj = FaultInjector()
    inj.load([
        {"id": "t", "target": "DMM", "trigger": {"type": "time", "after_s": 0.0},
         "action": {"type": "timeout"}},
    ])
    act = inj.check_instrument("DMM", "query", {})
    assert act is not None, "after_s=0 应立刻命中（elapsed_s 自动填充）"


def test_dsl_load_matches_doc_example() -> None:
    """§7.7.2 文档示例加载后可命中。"""
    inj = FaultInjector(seed=42)
    inj.load([
        {"id": "dmm_timeout_once", "target": "DMM_CH1",
         "method": "measure_voltage", "once": True,
         "trigger": {"type": "count", "value": 3},
         "fault": {"type": "timeout", "timeout_ms": 5000}},
        {"id": "eload_random_error", "target": "ELOAD_MAIN", "method": "*",
         "trigger": {"type": "probability", "value": 0.05},
         "fault": {"type": "instrument_error", "code": -113,
                   "message": "Undefined header"}},
    ])
    assert len(inj.rules) == 2
    # 注意：文档示例用 fault: 键而非 action: —— load 解析兼容两种键
    act = inj.check_instrument("DMM_CH1", "measure_voltage", {"call_count": 3})
    assert act is not None and act.fault_type == "timeout"


def test_load_accepts_fault_key_alias() -> None:
    """DSL 的 action/fault 键名均支持。"""
    inj = FaultInjector()
    inj.load([
        {"id": "x", "target": "*", "fault": {"type": "value_override", "value": 9.9}},
    ])
    act = inj.check_instrument("ANY", "query", {"call_count": 1})
    assert act is not None and act.value == 9.9


def test_clear_rules() -> None:
    inj = FaultInjector()
    inj.load([{"id": "a", "target": "*", "action": {"type": "timeout"}}])
    assert inj.rules
    inj.clear()
    assert inj.rules == []


# ---------------------------------------------------------------------------
# 异常映射（≥8 种故障类型）
# ---------------------------------------------------------------------------


def test_raise_for_maps_fault_types() -> None:
    cases = [
        ("connection_drop", NetworkFaultError),
        ("checksum_error", NetworkFaultError),
        ("scpi_error", ProtocolFaultError),
        ("truncated_data", ProtocolFaultError),
        ("instrument_error", InstrumentFaultError),
        ("out_of_range", InstrumentFaultError),
        ("scheduler_error", SchedulerFaultError),
        ("timeout", FaultInjectionError),  # 兜底统一基类
        ("bus_error", FaultInjectionError),
    ]
    for fault_type, exc_cls in cases:
        inj = FaultInjector()
        inj.load([{"id": "r", "target": "*",
                   "action": {"type": fault_type, "code": -113}}])
        act = inj.check_instrument("X", "query", {"call_count": 1})
        assert act is not None, fault_type
        with pytest.raises(exc_cls):
            inj.raise_for(act)


def test_raise_for_skips_non_exception_actions() -> None:
    """value_override/delay 等非异常动作不会被 raise_for 抛出。"""
    inj = FaultInjector()
    inj.load([
        {"id": "ov", "layer": "instrument", "target": "*",
         "action": {"type": "value_override", "value": 1.0}},
        {"id": "dl", "layer": "network", "target": "*",
         "action": {"type": "delay", "delay_ms": 10}},
    ])
    act_ov = inj.check_instrument("X", "query", {"call_count": 1})
    act_dl = inj.check_network("X", "query", {"call_count": 1})
    # 不抛异常
    inj.raise_for(act_ov)
    inj.raise_for(act_dl)


def test_fault_action_structure() -> None:
    act = FaultAction(fault_id="f", layer="network", fault_type="delay",
                      params={"delay_ms": 100})
    assert not act.is_exception
    act2 = FaultAction(fault_id="f", layer="protocol", fault_type="scpi_error",
                       params={"code": -113})
    assert act2.is_exception


# ---------------------------------------------------------------------------
# 调度层
# ---------------------------------------------------------------------------


def test_scheduler_layer_injection() -> None:
    inj = FaultInjector()
    inj.load([
        {"id": "deadlock", "layer": "scheduler", "target": "step_9",
         "trigger": {"type": "count", "value": 2},
         "action": {"type": "scheduler_error"}},
    ])
    assert inj.check_scheduler("step_9", "execute", {"call_count": 1}) is None
    act = inj.check_scheduler("step_9", "execute", {"call_count": 2})
    assert act is not None and act.fault_type == "scheduler_error"
    with pytest.raises(SchedulerFaultError):
        inj.raise_for(act)


# ---------------------------------------------------------------------------
# InstrumentSimulator 端到端挂接
# ---------------------------------------------------------------------------


def test_simulator_unchanged_without_injector() -> None:
    sim = _make_sim(None)
    val = float(sim.query("MEAS:VOLT:DC?"))
    # 原始 SIM 值为 1.5，无注入时只叠加噪声，值应接近 1.5
    assert 1.0 < val < 2.0


def test_simulator_network_packet_loss_returns_empty() -> None:
    inj = FaultInjector()
    inj.load([{"id": "loss", "layer": "network", "target": "DMM",
               "trigger": {"type": "count", "value": 1},
               "action": {"type": "packet_loss"}}])
    sim = _make_sim(inj)
    assert sim.query("MEAS:VOLT:DC?") == ""


def test_simulator_network_connection_drop_raises() -> None:
    inj = FaultInjector()
    inj.load([{"id": "drop", "layer": "network", "target": "DMM",
               "trigger": {"type": "count", "value": 1},
               "action": {"type": "connection_drop"}}])
    sim = _make_sim(inj)
    with pytest.raises(NetworkFaultError):
        sim.query("MEAS:VOLT:DC?")


def test_simulator_network_delay_sleeps() -> None:
    inj = FaultInjector()
    inj.load([{"id": "slow", "layer": "network", "target": "DMM",
               "trigger": {"type": "count", "value": 1},
               "action": {"type": "delay", "delay_ms": 100}}])
    sim = _make_sim(inj)
    start = time.monotonic()
    sim.query("MEAS:VOLT:DC?")
    assert time.monotonic() - start >= 0.09


def test_simulator_protocol_scpi_error_raises() -> None:
    inj = FaultInjector()
    inj.load([{"id": "scpi", "layer": "protocol", "target": "DMM",
               "trigger": {"type": "count", "value": 1},
               "action": {"type": "scpi_error", "code": -113}}])
    sim = _make_sim(inj)
    with pytest.raises(ProtocolFaultError) as ei:
        sim.query("MEAS:VOLT:DC?")
    assert ei.value.code == -113


def test_simulator_protocol_truncated_data() -> None:
    inj = FaultInjector()
    inj.load([{"id": "trunc", "layer": "protocol", "target": "DMM",
               "trigger": {"type": "count", "value": 1},
               "action": {"type": "truncated_data", "bytes": 3}}])
    sim = _make_sim(inj)
    resp = sim.query("MEAS:VOLT:DC?")
    assert len(resp) <= 3


def test_simulator_instrument_value_override() -> None:
    inj = FaultInjector()
    inj.load([{"id": "ov", "layer": "instrument", "target": "DMM",
               "trigger": {"type": "count", "value": 2},
               "action": {"type": "value_override", "value": 10.5}}])
    sim = _make_sim(inj)
    assert float(sim.query("MEAS?")) != 10.5
    assert float(sim.query("MEAS?")) == 10.5


def test_simulator_instrument_out_of_range_raises() -> None:
    inj = FaultInjector()
    inj.load([{"id": "oor", "layer": "instrument", "target": "DMM",
               "trigger": {"type": "count", "value": 1},
               "action": {"type": "out_of_range"}}])
    sim = _make_sim(inj)
    with pytest.raises(InstrumentFaultError):
        sim.query("MEAS?")


def test_simulator_once_rule_fires_then_passes() -> None:
    """once 规则：第 1 次丢包，后续恢复正常（容错重试验证场景）。"""
    inj = FaultInjector()
    inj.load([{"id": "once_loss", "layer": "network", "target": "DMM",
               "trigger": {"type": "count", "value": 1}, "once": True,
               "action": {"type": "packet_loss"}}])
    sim = _make_sim(inj)
    assert sim.query("MEAS?") == ""
    assert float(sim.query("MEAS?")) > 0  # 恢复
