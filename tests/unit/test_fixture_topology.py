"""共享工装拓扑数据模型 + 8 类接线校验引擎测试（§8.3.2 / §8.3.5，任务 #7）。

覆盖：
- 模型 strict 校验（extra=forbid，未知键拒绝）
- YAML round-trip 序列化/反序列化（含 from 别名）
- 8 类校验：端口类型匹配 / 信号方向 / 短路冲突 / 接地完整性 /
  通道占用 / 矩阵路由可达 / 夹具控制完整性 / DUT 测试点覆盖
- 辅助检查：电源容量 / GPIB 地址冲突
- ValidationResult valid/summary/as_dict
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from shared.fixture_topology import (
    DUT,
    Actuator,
    ActuatorType,
    Channel,
    ChannelDirection,
    ChannelType,
    CommunicationConfig,
    CommunicationType,
    Fixture,
    FixtureTopology,
    Instrument,
    InstrumentType,
    Link,
    LinkEndpoint,
    LinkEndpointType,
    LinkSignalType,
    Relay,
    RelayType,
    Route,
    Sensor,
    SensorType,
    Terminal,
    TestPoint,
    TopologyValidator,
    parse_fixture_topology,
    serialize_fixture_topology,
)


def _psu(id_: str = "PSU_MAIN", gpib: str | None = "5") -> Instrument:
    return Instrument(
        id=id_,
        name="电源",
        type=InstrumentType.PSU,
        communication=CommunicationConfig(
            type=CommunicationType.GPIB, address=gpib,
        ),
        channels=[
            Channel(
                id="CH1",
                type=ChannelType.VOLTAGE,
                direction=ChannelDirection.OUTPUT,
                specs={"max_current": 5.0, "rated_current": 10.0},
            ),
        ],
    )


def _dmm() -> Instrument:
    return Instrument(
        id="DMM_MAIN",
        name="万用表",
        type=InstrumentType.DMM,
        communication=CommunicationConfig(
            type=CommunicationType.GPIB, address="6",
        ),
        channels=[
            Channel(
                id="CH1",
                type=ChannelType.VOLTAGE,
                direction=ChannelDirection.INPUT,
            ),
        ],
    )


def _fixture() -> Fixture:
    return Fixture(
        id="FIX1",
        name="产测夹具",
        terminals=[
            Terminal(
                id="T1",
                type=ChannelType.VOLTAGE,
                direction=ChannelDirection.BIDIRECTIONAL,
            ),
            Terminal(
                id="TGND",
                type=ChannelType.VOLTAGE,
                direction=ChannelDirection.BIDIRECTIONAL,
            ),
        ],
        relays=[Relay(id="R1", type=RelayType.SPDT, control_signal="GPIO1")],
        actuators=[Actuator(id="A1", type=ActuatorType.CYLINDER)],
        sensors=[Sensor(id="S1", type=SensorType.POSITION)],
    )


def _dut() -> DUT:
    return DUT(
        id="DUT1",
        product_model="comm_module_v2",
        test_points=[
            TestPoint(
                id="TP1",
                net="VOUT",
                type=ChannelType.VOLTAGE,
                expected_range={"min": 4.5, "max": 5.5},
            ),
        ],
    )


def _base_topology() -> FixtureTopology:
    """合法拓扑：PSU--夹具--DUT，接地完整。"""
    psu = _psu()
    dmm = _dmm()
    fix = _fixture()
    dut = _dut()
    return FixtureTopology(
        name="产测工装",
        product_model="comm_module_v2",
        instruments=[psu, dmm],
        fixtures=[fix],
        duts=[dut],
        links=[
            Link(
                id="L1",
                from_endpoint=LinkEndpoint(
                    entity_type=LinkEndpointType.INSTRUMENT_CHANNEL,
                    entity_id="PSU_MAIN",
                    port_id="CH1",
                ),
                to=LinkEndpoint(
                    entity_type=LinkEndpointType.FIXTURE_TERMINAL,
                    entity_id="FIX1",
                    port_id="T1",
                ),
                signal_type=LinkSignalType.POWER,
                max_current=2.0,
            ),
            Link(
                id="L2",
                from_endpoint=LinkEndpoint(
                    entity_type=LinkEndpointType.FIXTURE_TERMINAL,
                    entity_id="FIX1",
                    port_id="T1",
                ),
                to=LinkEndpoint(
                    entity_type=LinkEndpointType.DUT_TESTPOINT,
                    entity_id="DUT1",
                    port_id="TP1",
                ),
                signal_type=LinkSignalType.POWER,
            ),
            Link(
                id="LGND",
                from_endpoint=LinkEndpoint(
                    entity_type=LinkEndpointType.INSTRUMENT_CHANNEL,
                    entity_id="PSU_MAIN",
                    port_id="CH1",
                ),
                to=LinkEndpoint(
                    entity_type=LinkEndpointType.FIXTURE_TERMINAL,
                    entity_id="FIX1",
                    port_id="TGND",
                ),
                signal_type=LinkSignalType.GROUND,
            ),
        ],
        routes=[Route(id="RT1", name="电源路径", links=["L1", "L2"], relays=["R1"])],
    )


# ---------------------------------------------------------------------------
# 模型 strict 校验
# ---------------------------------------------------------------------------


class TestModelStrict:
    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FixtureTopology.model_validate({"name": "x", "bogus_field": 1})

    def test_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            FixtureTopology.model_validate({})

    def test_link_from_alias_accepted(self) -> None:
        """'from' 别名与 'from_endpoint' 均可用。"""
        data = {
            "id": "L1",
            "from": {"entity_type": "instrument_channel", "entity_id": "PSU_MAIN", "port_id": "CH1"},
            "to": {"entity_type": "dut_testpoint", "entity_id": "DUT1", "port_id": "TP1"},
            "signal_type": "power",
        }
        link = Link.model_validate(data)
        assert link.from_endpoint.entity_id == "PSU_MAIN"
        assert link.to.entity_id == "DUT1"


# ---------------------------------------------------------------------------
# YAML round-trip
# ---------------------------------------------------------------------------


class TestYamlRoundTrip:
    def test_round_trip(self) -> None:
        topo = _base_topology()
        s = serialize_fixture_topology(topo)
        back = parse_fixture_topology(s)
        assert back.name == topo.name
        assert back.links[0].id == "L1"
        assert back.links[0].from_endpoint.entity_id == "PSU_MAIN"
        assert back.instruments[0].channels[0].direction == ChannelDirection.OUTPUT

    def test_malformed_yaml_raises(self) -> None:
        with pytest.raises(yaml.YAMLError):
            parse_fixture_topology(": : : not : yaml")


# ---------------------------------------------------------------------------
# 校验引擎
# ---------------------------------------------------------------------------


class TestValidator:
    def test_valid_topology_passes(self) -> None:
        result = _base_topology().validate_topology()
        assert result.valid, result.summary

    def test_validation_result_dict(self) -> None:
        topo = _base_topology()
        # 制造一个错误
        topo.links[0].max_current = 99.0  # 超容量 -> warning
        d = topo.validate_topology().as_dict()
        assert "valid" in d and "errors" in d and "warnings" in d and "summary" in d

    # 检查 1：端口类型匹配
    def test_port_type_mismatch(self) -> None:
        topo = _base_topology()
        # L1 是电压端口连线，但声明为 RF 信号类型 -> 不兼容
        topo.links[0].signal_type = LinkSignalType.RF
        result = topo.validate_topology()
        assert not result.valid
        assert any(i.code == TopologyValidator.CHECK_PORT_TYPE for i in result.errors)

    # 检查 2：信号方向
    def test_direction_mismatch_source_not_output(self) -> None:
        topo = _base_topology()
        # 把 PSU 通道改为输入方向
        topo.instruments[0].channels[0].direction = ChannelDirection.INPUT
        result = topo.validate_topology()
        assert any(i.code == TopologyValidator.CHECK_DIRECTION for i in result.errors)

    def test_direction_mismatch_unresolvable_endpoint(self) -> None:
        topo = _base_topology()
        topo.links[0].from_endpoint = LinkEndpoint(
            entity_type=LinkEndpointType.INSTRUMENT_CHANNEL,
            entity_id="NOPE",  # 不存在的仪器
            port_id="CH1",
        )
        result = topo.validate_topology()
        assert any(i.code == TopologyValidator.CHECK_DIRECTION for i in result.errors)

    # 检查 3：短路/通道冲突
    def test_short_circuit_conflict(self) -> None:
        topo = _base_topology()
        # 两条线都指向 DUT1.TP1
        topo.links.append(
            Link(
                id="L3",
                from_endpoint=LinkEndpoint(
                    entity_type=LinkEndpointType.FIXTURE_TERMINAL,
                    entity_id="FIX1",
                    port_id="TGND",
                ),
                to=LinkEndpoint(
                    entity_type=LinkEndpointType.DUT_TESTPOINT,
                    entity_id="DUT1",
                    port_id="TP1",
                ),
                signal_type=LinkSignalType.SIGNAL,
            )
        )
        result = topo.validate_topology()
        assert any(i.code == TopologyValidator.CHECK_CONFLICT for i in result.errors)

    # 检查 4：接地完整性
    def test_ground_integrity_warning(self) -> None:
        topo = _base_topology()
        # 移除地线
        topo.links = [link for link in topo.links if link.signal_type != LinkSignalType.GROUND]
        result = topo.validate_topology()
        assert result.valid  # warning 不阻断
        assert any(i.code == TopologyValidator.CHECK_GROUND for i in result.warnings)

    # 检查 5：仪器通道占用
    def test_channel_occupancy_without_matrix(self) -> None:
        topo = _base_topology()
        # 同一 DMM 通道接两个 DUT 测试点（无 routeId -> 无矩阵隔离）
        topo.duts[0].test_points.append(
            TestPoint(id="TP2", net="VBAT", type=ChannelType.VOLTAGE)
        )
        for tp_id in ("TP1", "TP2"):
            topo.links.append(
                Link(
                    id=f"L4_{tp_id}",
                    from_endpoint=LinkEndpoint(
                        entity_type=LinkEndpointType.INSTRUMENT_CHANNEL,
                        entity_id="DMM_MAIN",
                        port_id="CH1",
                    ),
                    to=LinkEndpoint(
                        entity_type=LinkEndpointType.DUT_TESTPOINT,
                        entity_id="DUT1",
                        port_id=tp_id,
                    ),
                    signal_type=LinkSignalType.SIGNAL,
                )
            )
        result = topo.validate_topology()
        assert any(i.code == TopologyValidator.CHECK_OCCUPANCY for i in result.errors)

    def test_channel_occupancy_with_matrix_allowed(self) -> None:
        topo = _base_topology()
        topo.duts[0].test_points.append(
            TestPoint(id="TP2", net="VBAT", type=ChannelType.VOLTAGE)
        )
        # 同通道接两个测试点，但经矩阵开关（带 routeId）隔离
        for tp_id in ("TP1", "TP2"):
            topo.links.append(
                Link(
                    id=f"L4_{tp_id}",
                    from_endpoint=LinkEndpoint(
                        entity_type=LinkEndpointType.INSTRUMENT_CHANNEL,
                        entity_id="DMM_MAIN",
                        port_id="CH1",
                    ),
                    to=LinkEndpoint(
                        entity_type=LinkEndpointType.DUT_TESTPOINT,
                        entity_id="DUT1",
                        port_id=tp_id,
                    ),
                    signal_type=LinkSignalType.SIGNAL,
                    routeId="RT1",
                )
            )
        result = topo.validate_topology()
        assert not any(
            i.code == TopologyValidator.CHECK_OCCUPANCY for i in result.errors
        ), "经矩阵开关隔离不应报通道占用"

    # 检查 6：矩阵开关路由可达
    def test_route_referenced_but_missing(self) -> None:
        topo = _base_topology()
        topo.links[0].routeId = "NOPE_ROUTE"
        result = topo.validate_topology()
        assert any(i.code == TopologyValidator.CHECK_ROUTE for i in result.errors)

    def test_route_missing_relay(self) -> None:
        topo = _base_topology()
        topo.routes[0].relays = ["R1", "R_NOT_EXIST"]
        result = topo.validate_topology()
        assert any(i.code == TopologyValidator.CHECK_ROUTE for i in result.errors)

    # 检查 7：夹具控制完整性
    def test_fixture_control_missing(self) -> None:
        topo = _base_topology()
        topo.fixtures[0].relays[0].control_signal = ""
        result = topo.validate_topology()
        assert any(i.code == TopologyValidator.CHECK_FIXTURE_CONTROL for i in result.errors)

    # 检查 8：DUT 测试点覆盖
    def test_dut_coverage_missing(self) -> None:
        topo = _base_topology()
        # 新增一个带期望范围的测试点但未接线
        topo.duts[0].test_points.append(
            TestPoint(
                id="TPX",
                net="VTEMP",
                type=ChannelType.VOLTAGE,
                expected_range={"min": 0.0, "max": 3.3},
            )
        )
        result = topo.validate_topology()
        assert result.valid  # warning 不阻断
        assert any(i.code == TopologyValidator.CHECK_DUT_COVERAGE for i in result.warnings)

    # 辅助检查：电源容量
    def test_power_capacity_warning(self) -> None:
        topo = _base_topology()
        topo.links[0].max_current = 99.0  # 超过额定 10A
        result = topo.validate_topology()
        assert any(i.code == TopologyValidator.CHECK_POWER for i in result.warnings)

    # 辅助检查：GPIB 地址冲突
    def test_gpib_address_conflict(self) -> None:
        topo = _base_topology()
        topo.instruments.append(
            Instrument(
                id="PSU_BACKUP",
                name="备份电源",
                type=InstrumentType.PSU,
                communication=CommunicationConfig(
                    type=CommunicationType.GPIB, address="5",  # 与 PSU_MAIN 冲突
                ),
            )
        )
        result = topo.validate_topology()
        assert any(i.code == TopologyValidator.CHECK_GPIB for i in result.errors)

    # 严格度配置
    def test_strictness_warning_downgrades(self) -> None:
        """strictness=warning 时冲突类检查降级为 warning。"""
        topo = _base_topology()
        topo.links.append(
            Link(
                id="L3",
                from_endpoint=LinkEndpoint(
                    entity_type=LinkEndpointType.FIXTURE_TERMINAL,
                    entity_id="FIX1",
                    port_id="TGND",
                ),
                to=LinkEndpoint(
                    entity_type=LinkEndpointType.DUT_TESTPOINT,
                    entity_id="DUT1",
                    port_id="TP1",
                ),
                signal_type=LinkSignalType.SIGNAL,
            )
        )
        v = TopologyValidator(strictness="warning")
        result = v.validate(topo)
        assert result.valid, "严格度 warning 下不应阻断"
        assert any(i.code == TopologyValidator.CHECK_CONFLICT for i in result.warnings)
