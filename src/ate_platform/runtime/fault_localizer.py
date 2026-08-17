"""FaultLocalizer — 故障定位器（设计文档 §8.3.7，任务 #9）。

测试步骤失败时，在工装拓扑中定位故障位置并给出修复建议。

定位策略（§8.3.7）：
1. 关联 Route 分析：高亮整条路径（suspect_links / suspect_relays）。
2. 测量值分析：0/超量程→开路短路；偏低→接触不良；波动→松动干扰。
3. 仪器状态检查：报错/超时→通信故障。
4. 继电器状态检查：未按预期闭合→继电器故障。
5. 夹具传感器检查：气缸未到位/温度异常。

输出 FaultLocation 列表，前端据此在拓扑上高亮并显示修复建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from shared.fixture_topology import (
    FaultType,
    FixtureTopology,
    LinkEndpointType,
    Route,
)

logger = structlog.get_logger(__name__)


@dataclass
class FaultLocation:
    """单条故障定位结果。

    Attributes:
        type: 故障类型（FaultType 值）。
        message: 故障描述。
        suggestion: 修复建议。
        route_id: 关联信号路径 ID。
        testpoint_id: 涉及测试点 ID。
        instrument_id: 涉及仪器 ID。
        fixture_id: 涉及夹具 ID。
        suspect_links: 疑似故障链路 ID 列表。
        suspect_relays: 疑似故障继电器 ID 列表。
        severity: 严重度（error/critical）。
    """

    type: str
    message: str
    suggestion: str | None = None
    route_id: str | None = None
    testpoint_id: str | None = None
    instrument_id: str | None = None
    fixture_id: str | None = None
    suspect_links: list[str] = field(default_factory=list)
    suspect_relays: list[str] = field(default_factory=list)
    severity: str = "error"

    def as_dict(self) -> dict[str, Any]:
        """转为 dict（SSE / API 传输用）。"""
        return {
            "type": self.type,
            "message": self.message,
            "suggestion": self.suggestion,
            "route_id": self.route_id,
            "testpoint_id": self.testpoint_id,
            "instrument_id": self.instrument_id,
            "fixture_id": self.fixture_id,
            "suspect_links": self.suspect_links,
            "suspect_relays": self.suspect_relays,
            "severity": self.severity,
        }


class FaultLocalizer:
    """根据测试失败信息在工装拓扑中定位故障位置（§8.3.7）。

    Args:
        topology: 工装拓扑（共享 FixtureTopology 模型）。
    """

    def __init__(self, topology: FixtureTopology) -> None:
        self.topology = topology
        self._route_index: dict[str, Route] = {
            r.id: r for r in topology.routes
        }
        self._link_index: dict[str, Any] = {l.id: l for l in topology.links}
        self._fixture_index: dict[str, Any] = {
            f.id: f for f in topology.fixtures
        }
        self._dut_index: dict[str, Any] = {d.id: d for d in topology.duts}

    def localize(self, step_result: dict[str, Any]) -> list[FaultLocation]:
        """定位步骤失败的故障位置。

        Args:
            step_result: 步骤结果（含 step_id / measurement / instrument_status
                / relay_states / fixture_sensors 等字段，见 §8.3.7）。

        Returns:
            FaultLocation 列表（按严重度降序）。
        """
        locations: list[FaultLocation] = []

        step_id = step_result.get("step_id", "")
        routes = self._find_routes_for_step(step_id)

        # 1. 测量值分析
        measurement = step_result.get("measurement")
        if measurement:
            for route in routes:
                loc = self._analyze_measurement(route, measurement)
                if loc:
                    locations.append(loc)

        # 2. 仪器状态检查
        for route in routes:
            loc = self._check_instrument_status(route)
            if loc:
                locations.append(loc)

        # 3. 继电器状态检查
        for route in routes:
            loc = self._check_relay_states(route, step_result)
            if loc:
                locations.append(loc)

        # 4. 夹具传感器检查（不依赖 route）
        sensor_loc = self._check_fixture_sensors(step_result)
        if sensor_loc:
            locations.extend(sensor_loc)

        # 5. 无具体定位时的兜底建议（仅在没有任何可分析信息时触发；
        #    测量正常说明故障另有原因，不在此猜测）
        has_clues = bool(
            measurement
            or (step_result.get("relay_states") is not None)
            or (step_result.get("fixture_sensors") is not None)
        )
        if not locations and not has_clues and routes:
            locations.append(
                FaultLocation(
                    type=FaultType.MEASUREMENT_OUT_OF_RANGE.value,
                    message=f"步骤 {step_id} 失败，路径 {routes[0].name or routes[0].id} 未检出具体故障",
                    suggestion="检查路径链路与继电器闭合状态，必要时重新校准仪器",
                    route_id=routes[0].id,
                    suspect_links=routes[0].links,
                    suspect_relays=routes[0].relays,
                )
            )

        locations.sort(key=lambda loc: loc.severity != "critical")
        logger.info(
            "fault_localized",
            step_id=step_id,
            count=len(locations),
            types=[loc.type for loc in locations],
        )
        return locations

    # ------------------------------------------------------------------
    # 定位步骤
    # ------------------------------------------------------------------

    def _find_routes_for_step(self, step_id: str) -> list[Route]:
        """找步骤关联的信号路径（Route.associated_step == step_id）。"""
        if not step_id:
            return list(self._route_index.values())
        routes = [
            r for r in self.topology.routes
            if r.associated_step == step_id
        ]
        return routes or list(self._route_index.values())

    def _analyze_measurement(
        self, route: Route, measurement: dict[str, Any],
    ) -> FaultLocation | None:
        """测量值分析：0/超量程→开路；偏低→接触不良；偏高→过压。"""
        tp_id = measurement.get("testpoint_id")
        expected = measurement.get("expected_range")
        actual = measurement.get("value")

        if actual is None:
            return None

        dut_id = measurement.get("dut_id")
        if tp_id and dut_id:
            tp = self._find_testpoint(dut_id, tp_id)
            if tp is not None and tp.expected_range is not None:
                expected = tp.expected_range
                # 0 / 超量程 → 开路短路（优先于越界判断）
                if actual == 0:
                    return self._open_circuit(route, tp_id)
                if expected.get("max") is not None and actual > expected["max"]:
                    return FaultLocation(
                        type=FaultType.MEASUREMENT_OUT_OF_RANGE.value,
                        message=f"测试点 {tp_id} 测量值 {actual} 高于上限 {expected['max']}",
                        suggestion="检查电源设定值、DUT 是否存在过压故障",
                        route_id=route.id,
                        testpoint_id=tp_id,
                        suspect_links=route.links,
                        suspect_relays=route.relays,
                    )
                if expected.get("min") is not None and actual < expected["min"]:
                    return FaultLocation(
                        type=FaultType.MEASUREMENT_OUT_OF_RANGE.value,
                        message=f"测试点 {tp_id} 测量值 {actual} 低于下限 {expected['min']}",
                        suggestion="检查 DUT 输出是否正常、接触电阻是否过大",
                        route_id=route.id,
                        testpoint_id=tp_id,
                        suspect_links=route.links,
                        suspect_relays=route.relays,
                    )
                return None

        # 无 expected_range 时按经验规则
        if actual == 0:
            return self._open_circuit(route, tp_id)
        return None

    @staticmethod
    def _open_circuit(route: Route, tp_id: str | None) -> FaultLocation:
        """构造开路故障定位。"""
        return FaultLocation(
            type=FaultType.OPEN_CIRCUIT.value,
            message=f"测试点 {tp_id or '?'} 测量值为零，疑似链路开路",
            suggestion="检查接线是否松动、继电器是否正常闭合、DUT 是否正确放入",
            route_id=route.id,
            testpoint_id=tp_id,
            suspect_links=route.links,
            suspect_relays=route.relays,
        )

    def _check_instrument_status(self, route: Route) -> FaultLocation | None:
        """检查路径涉及仪器的通信状态。"""
        for link_id in route.links:
            link = self._link_index.get(link_id)
            if link is None:
                continue
            inst_id = self._instrument_of_link(link)
            if inst_id is None:
                continue
            fault = link.fault_info
            if fault is not None and fault.type == FaultType.COMMUNICATION:
                severity = fault.severity.value if hasattr(fault.severity, "value") else str(fault.severity)
                return FaultLocation(
                    type=FaultType.COMMUNICATION.value,
                    message=f"仪器 {inst_id} 通信故障：{fault.message}",
                    suggestion=fault.suggestion or "检查仪器通信线缆与地址配置，必要时重启仪器",
                    route_id=route.id,
                    instrument_id=inst_id,
                    suspect_links=[link_id],
                    severity=severity,
                )
        return None

    def _check_relay_states(
        self, route: Route, step_result: dict[str, Any],
    ) -> FaultLocation | None:
        """检查路径继电器是否按预期闭合。"""
        relay_states = step_result.get("relay_states") or {}
        for relay_id in route.relays:
            expected = relay_states.get(relay_id)
            if expected is None:
                continue
            # expected: "closed" / {"state": "closed"} / True
            if isinstance(expected, dict):
                is_closed = expected.get("state") == "closed"
            elif isinstance(expected, bool):
                is_closed = expected
            else:
                is_closed = expected == "closed"
            if not is_closed:
                return FaultLocation(
                    type=FaultType.RELAY_FAULT.value,
                    message=f"继电器 {relay_id} 未按预期闭合（路径 {route.name or route.id}）",
                    suggestion="检查继电器控制信号（GPIO/Modbus）、驱动电路与触点",
                    route_id=route.id,
                    suspect_links=route.links,
                    suspect_relays=[relay_id],
                )
        return None

    def _check_fixture_sensors(
        self, step_result: dict[str, Any],
    ) -> list[FaultLocation]:
        """检查夹具传感器状态（气缸未到位/温度异常等）。"""
        locations: list[FaultLocation] = []
        sensors = step_result.get("fixture_sensors") or {}
        for fixture_id, readings in sensors.items():
            if not isinstance(readings, dict):
                continue
            fixture = self._fixture_index.get(fixture_id)
            for sensor_id, reading in readings.items():
                if isinstance(reading, dict):
                    value = reading.get("value")
                    expected = reading.get("expected")
                else:
                    value = reading
                    expected = None
                sensor = self._find_sensor(fixture, sensor_id)
                if sensor is None:
                    continue
                if sensor.type.value == "position" and value != expected:
                    locations.append(
                        FaultLocation(
                            type=FaultType.RELAY_FAULT.value,
                            message=(
                                f"夹具 {fixture_id} 传感器 {sensor_id} "
                                f"未到位（值={value}，期望={expected}）"
                            ),
                            suggestion="检查气缸是否卡滞、气源压力与电磁阀状态",
                            fixture_id=fixture_id,
                            severity="error",
                        )
                    )
                elif sensor.type.value == "temperature" and isinstance(value, (int, float)):
                    if self._out_of_range(value, sensor):
                        locations.append(
                            FaultLocation(
                                type=FaultType.MEASUREMENT_OUT_OF_RANGE.value,
                                message=(
                                    f"夹具 {fixture_id} 温度传感器 {sensor_id} "
                                    f"读数 {value} 超量程"
                                ),
                                suggestion="检查温控回路与散热状态",
                                fixture_id=fixture_id,
                                severity="warning",
                            )
                        )
        return locations

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _find_testpoint(self, dut_id: str, tp_id: str) -> Any | None:
        dut = self._dut_index.get(dut_id)
        if dut is None:
            return None
        for tp in dut.test_points:
            if tp.id == tp_id:
                return tp
        return None

    def _find_sensor(self, fixture: Any, sensor_id: str) -> Any | None:
        if fixture is None:
            return None
        for sensor in fixture.sensors:
            if sensor.id == sensor_id:
                return sensor
        return None

    @staticmethod
    def _out_of_range(value: float, sensor: Any) -> bool:
        if sensor.range is None:
            return False
        if sensor.range.get("max") is not None and value > sensor.range["max"]:
            return True
        if sensor.range.get("min") is not None and value < sensor.range["min"]:
            return True
        return False

    @staticmethod
    def _instrument_of_link(link: Any) -> str | None:
        """取链路起点仪器 ID（若起点为仪器通道）。"""
        src = link.from_endpoint
        if src.entity_type == LinkEndpointType.INSTRUMENT_CHANNEL:
            return src.entity_id
        return None
