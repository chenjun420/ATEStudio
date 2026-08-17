"""FaultLocalizer 故障定位器测试（设计文档 §8.3.7，任务 #9）。

覆盖：
- 测量值分析：0→开路、低于下限→under、高于上限→over、范围内→无故障
- 仪器通信故障检测
- 继电器未闭合故障
- 夹具传感器检查：气缸未到位、温度超量程
- 无具体定位时的兜底建议
- as_dict 输出形状
"""

from __future__ import annotations

from shared.fixture_topology import FixtureTopology, LinkStatus
from ate_platform.runtime import FaultLocalizer


def _topology_dict(**overrides: object) -> dict[str, object]:
    """合法拓扑：PSU--夹具--DUT，DUT TP1 有期望范围。"""
    topo: dict[str, object] = {
        "name": "故障定位拓扑",
        "instruments": [
            {
                "id": "PSU_MAIN",
                "name": "电源",
                "type": "psu",
                "communication": {"type": "gpib", "address": "5"},
                "channels": [
                    {"id": "CH1", "type": "voltage", "direction": "output",
                     "specs": {"max_current": 5.0}},
                ],
            },
        ],
        "fixtures": [
            {
                "id": "FIX1",
                "name": "产测夹具",
                "terminals": [
                    {"id": "T1", "type": "voltage", "direction": "bidirectional"},
                ],
                "relays": [
                    {"id": "R1", "type": "spdt", "control_signal": "GPIO1"},
                ],
                "actuators": [
                    {"id": "A1", "type": "cylinder"},
                ],
                "sensors": [
                    {"id": "clamp_position", "type": "position", "unit": ""},
                    {"id": "temp", "type": "temperature", "unit": "C",
                     "range": {"min": 0, "max": 60}},
                ],
                "dut_slot_count": 1,
            },
        ],
        "duts": [
            {
                "id": "DUT1",
                "product_model": "comm_module_v2",
                "test_points": [
                    {"id": "TP1", "net": "VOUT", "type": "voltage",
                     "expected_range": {"min": 4.5, "max": 5.5}},
                ],
            },
        ],
        "links": [
            {
                "id": "L1",
                "from": {"entity_type": "instrument_channel",
                         "entity_id": "PSU_MAIN", "port_id": "CH1"},
                "to": {"entity_type": "fixture_terminal",
                       "entity_id": "FIX1", "port_id": "T1"},
                "signal_type": "power",
            },
            {
                "id": "L2",
                "from": {"entity_type": "fixture_terminal",
                         "entity_id": "FIX1", "port_id": "T1"},
                "to": {"entity_type": "dut_testpoint",
                       "entity_id": "DUT1", "port_id": "TP1"},
                "signal_type": "power",
            },
        ],
        "routes": [
            {"id": "RT1", "name": "VOUT 测量路径",
             "links": ["L1", "L2"], "relays": ["R1"],
             "associated_step": "step_measure"},
        ],
    }
    topo.update(overrides)
    return topo


def _localizer(**overrides: object) -> FaultLocalizer:
    return FaultLocalizer(FixtureTopology.model_validate(_topology_dict(**overrides)))


def _step_result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {"step_id": "step_measure"}
    result.update(overrides)
    return result


class TestMeasurementAnalysis:
    def test_zero_value_detects_open_circuit(self) -> None:
        result = _step_result(measurement={
            "dut_id": "DUT1", "testpoint_id": "TP1", "value": 0,
        })
        locs = _localizer().localize(result)
        assert any(l.type == "open_circuit" for l in locs)
        open_loc = next(l for l in locs if l.type == "open_circuit")
        assert open_loc.suspect_links == ["L1", "L2"]
        assert open_loc.suspect_relays == ["R1"]
        assert open_loc.testpoint_id == "TP1"
        assert open_loc.suggestion

    def test_below_min_detects_under_range(self) -> None:
        result = _step_result(measurement={
            "dut_id": "DUT1", "testpoint_id": "TP1", "value": 3.2,
        })
        locs = _localizer().localize(result)
        under = [l for l in locs if l.type == "measurement_out_of_range"]
        assert under, f"expected under-range fault, got {[l.type for l in locs]}"
        assert "低于下限" in under[0].message

    def test_above_max_detects_over_range(self) -> None:
        result = _step_result(measurement={
            "dut_id": "DUT1", "testpoint_id": "TP1", "value": 6.1,
        })
        locs = _localizer().localize(result)
        over = [l for l in locs if l.type == "measurement_out_of_range"]
        assert over
        assert "高于上限" in over[0].message

    def test_in_range_no_measurement_fault(self) -> None:
        result = _step_result(measurement={
            "dut_id": "DUT1", "testpoint_id": "TP1", "value": 5.0,
        })
        locs = _localizer().localize(result)
        assert not any(
            l.type in ("open_circuit", "measurement_out_of_range")
            for l in locs
        )

    def test_no_measurement_no_fault(self) -> None:
        locs = _localizer().localize(_step_result())
        # 无测量信息时给出兜底建议
        assert locs


class TestInstrumentStatus:
    def test_communication_fault_detected(self) -> None:
        topo = _topology_dict()
        # 在 L1 上挂通信故障
        topo["links"][0]["status"] = LinkStatus.FAULT.value
        topo["links"][0]["fault_info"] = {
            "type": "communication",
            "severity": "error",
            "message": "VISA timeout",
            "suggestion": "检查 GPIB 线缆",
        }
        result = _step_result(measurement={
            "dut_id": "DUT1", "testpoint_id": "TP1", "value": 0,
        })
        locs = FaultLocalizer(
            FixtureTopology.model_validate(topo)
        ).localize(result)
        comm = [l for l in locs if l.type == "communication"]
        assert comm
        assert comm[0].instrument_id == "PSU_MAIN"
        assert "VISA timeout" in comm[0].message


class TestRelayStates:
    def test_relay_not_closed_detected(self) -> None:
        result = _step_result(
            measurement={"dut_id": "DUT1", "testpoint_id": "TP1", "value": 4.8},
            relay_states={"R1": "open"},
        )
        locs = _localizer().localize(result)
        relay_fault = [l for l in locs if l.type == "relay_fault"]
        assert relay_fault
        assert relay_fault[0].suspect_relays == ["R1"]

    def test_relay_closed_ok(self) -> None:
        result = _step_result(
            measurement={"dut_id": "DUT1", "testpoint_id": "TP1", "value": 4.8},
            relay_states={"R1": "closed"},
        )
        locs = _localizer().localize(result)
        assert not any(l.type == "relay_fault" for l in locs)

    def test_relay_state_dict_form(self) -> None:
        result = _step_result(
            relay_states={"R1": {"state": "open"}},
        )
        locs = _localizer().localize(result)
        assert any(l.type == "relay_fault" for l in locs)


class TestFixtureSensors:
    def test_cylinder_not_positioned(self) -> None:
        result = _step_result(
            fixture_sensors={"FIX1": {"clamp_position": {"value": 0, "expected": 1}}},
        )
        locs = _localizer().localize(result)
        assert any(
            l.type == "relay_fault" and l.fixture_id == "FIX1"
            for l in locs
        )

    def test_temperature_out_of_range(self) -> None:
        result = _step_result(
            fixture_sensors={"FIX1": {"temp": {"value": 80, "expected": None}}},
        )
        locs = _localizer().localize(result)
        temp_fault = [l for l in locs if l.type == "measurement_out_of_range"]
        assert temp_fault
        assert temp_fault[0].severity == "warning"

    def test_temperature_in_range_ok(self) -> None:
        result = _step_result(
            fixture_sensors={"FIX1": {"temp": {"value": 35, "expected": None}}},
        )
        locs = _localizer().localize(result)
        assert not any(
            l.type == "measurement_out_of_range" and "温度" in l.message
            for l in locs
        )


class TestFallback:
    def test_no_specific_fault_gives_fallback(self) -> None:
        """步骤失败但无测量/继电器/传感器信息 -> 兜底建议。"""
        locs = _localizer().localize(_step_result())
        assert locs
        assert locs[0].route_id == "RT1"
        assert locs[0].suspect_links == ["L1", "L2"]


class TestOutputShape:
    def test_as_dict_shape(self) -> None:
        result = _step_result(measurement={
            "dut_id": "DUT1", "testpoint_id": "TP1", "value": 0,
        })
        loc = _localizer().localize(result)[0]
        d = loc.as_dict()
        assert set(d) == {
            "type", "message", "suggestion", "route_id", "testpoint_id",
            "instrument_id", "fixture_id", "suspect_links", "suspect_relays",
            "severity",
        }
        assert d["type"] == "open_circuit"

    def test_severity_sorting_critical_first(self) -> None:
        """critical 故障排在 error 之前。"""
        topo = _topology_dict()
        topo["links"][0]["status"] = LinkStatus.FAULT.value
        topo["links"][0]["fault_info"] = {
            "type": "communication", "severity": "critical",
            "message": "通信中断", "suggestion": "重启",
        }
        result = _step_result(measurement={
            "dut_id": "DUT1", "testpoint_id": "TP1", "value": 0,
        })
        locs = FaultLocalizer(FixtureTopology.model_validate(topo)).localize(result)
        assert locs[0].type == "communication"
