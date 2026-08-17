"""Fixture topology schema and validation engine for ATE Studio.

设计文档 §8.3.2 工装拓扑数据模型（核心实体）与 §8.3.5 接线校验引擎。

This module defines Pydantic v2 models for fixture (test jig) topologies:
- Instrument / Channel: 仪器仪表及其通道
- Fixture / Terminal / Relay / Actuator / Sensor: 夹具及其内部元件
- DUT / TestPoint / PowerPin: 被测产品
- Link / LinkEndpoint / Route / FaultInfo: 接线与信号路径
- FixtureTopology: 顶层工装拓扑（instruments/fixtures/duts/links/routes）

All models use ``extra='forbid'`` for strict validation -- unknown keys are
rejected rather than silently ignored, preventing configuration drift.

The :class:`TopologyValidator` performs the 8 类接线校验 from §8.3.5
(port type match / signal direction / short-circuit & channel conflict /
ground integrity / channel occupancy / matrix route reachability /
fixture control integrity / DUT test-point coverage) plus auxiliary checks
(power capacity, GPIB address conflict).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Actuator",
    "ActuatorType",
    "Channel",
    "ChannelDirection",
    "ChannelType",
    "CommunicationConfig",
    "CommunicationType",
    "DeviceStatus",
    "DUT",
    "DUTStatus",
    "FaultInfo",
    "FaultType",
    "Fixture",
    "FixtureStatus",
    "FixtureTopology",
    "Instrument",
    "InstrumentType",
    "Link",
    "LinkEndpoint",
    "LinkEndpointType",
    "LinkSignalType",
    "LinkStatus",
    "PowerPin",
    "Relay",
    "RelayType",
    "Route",
    "Sensor",
    "SensorType",
    "Severity",
    "Terminal",
    "TestPoint",
    "TestPointStatus",
    "TopologyValidator",
    "ValidationIssue",
    "ValidationResult",
    "parse_fixture_topology",
    "serialize_fixture_topology",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class InstrumentType(str, Enum):
    """仪器类型（§8.3.2 Instrument.type）。"""

    PSU = "psu"
    DMM = "dmm"
    ELOAD = "eload"
    OSCILLOSCOPE = "oscilloscope"
    GPIB_GATEWAY = "gpib_gateway"
    TCP_DEVICE = "tcp_device"
    CUSTOM = "custom"


class CommunicationType(str, Enum):
    """通信类型（§8.3.2 Instrument.communication.type）。"""

    GPIB = "gpib"
    TCP = "tcp"
    SERIAL = "serial"
    USB = "usb"
    CUSTOM = "custom"


class ChannelType(str, Enum):
    """通道信号类型（§8.3.2 Channel.type）。"""

    VOLTAGE = "voltage"
    CURRENT = "current"
    RESISTANCE = "resistance"
    DIGITAL_IO = "digital_io"
    RF = "rf"
    THERMAL = "thermal"


class ChannelDirection(str, Enum):
    """通道方向（§8.3.2 Channel.direction）。"""

    INPUT = "input"
    OUTPUT = "output"
    BIDIRECTIONAL = "bidirectional"


class DeviceStatus(str, Enum):
    """仪器状态（§8.3.2 Instrument.status）。"""

    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    FAULT = "fault"
    SIMULATED = "simulated"


class RelayType(str, Enum):
    """继电器类型（§8.3.2 Relay.type）。"""

    SPST = "spst"
    SPDT = "spdt"
    DPDT = "dpdt"
    MATRIX = "matrix"


class ActuatorType(str, Enum):
    """执行器类型（§8.3.2 Actuator.type）。"""

    CYLINDER = "cylinder"
    MOTOR = "motor"
    VALVE = "valve"


class SensorType(str, Enum):
    """传感器类型（§8.3.2 Sensor.type）。"""

    POSITION = "position"
    TEMPERATURE = "temperature"
    PRESSURE = "pressure"
    PROXIMITY = "proximity"
    OPTICAL = "optical"


class FixtureStatus(str, Enum):
    """夹具状态。"""

    IDLE = "idle"
    CLAMPED = "clamped"
    BUSY = "busy"
    FAULT = "fault"


class DUTStatus(str, Enum):
    """DUT 状态（§8.3.2 DUT.status）。"""

    IDLE = "idle"
    TESTING = "testing"
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class TestPointStatus(str, Enum):
    """测试点状态（§8.3.2 TestPoint.status）。"""

    IDLE = "idle"
    MEASURING = "measuring"
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class LinkSignalType(str, Enum):
    """连线信号类型（§8.3.2 Link.signal_type）。"""

    POWER = "power"
    SIGNAL = "signal"
    GROUND = "ground"
    RF = "rf"
    THERMAL = "thermal"
    AIR = "air"


class LinkStatus(str, Enum):
    """链路状态（§8.3.2 Link.status）。"""

    IDLE = "idle"
    ACTIVE = "active"
    FAULT = "fault"
    WARNING = "warning"


class LinkEndpointType(str, Enum):
    """链路端点实体类型（§8.3.2 LinkEndpoint.entity_type）。"""

    INSTRUMENT_CHANNEL = "instrument_channel"
    FIXTURE_TERMINAL = "fixture_terminal"
    DUT_TESTPOINT = "dut_testpoint"
    RELAY_CONTACT = "relay_contact"


class FaultType(str, Enum):
    """故障类型（§8.3.2 FaultInfo.type）。"""

    OPEN_CIRCUIT = "open_circuit"
    SHORT_CIRCUIT = "short_circuit"
    OVER_VOLTAGE = "over_voltage"
    OVER_CURRENT = "over_current"
    COMMUNICATION = "communication"
    MEASUREMENT_OUT_OF_RANGE = "measurement_out_of_range"
    RELAY_FAULT = "relay_fault"


class Severity(str, Enum):
    """故障严重度（§8.3.2 FaultInfo.severity）。"""

    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# 仪器仪表
# ---------------------------------------------------------------------------


class CommunicationConfig(BaseModel):
    """通信配置（§8.3.2 Instrument.communication）。

    Attributes:
        type: 通信类型（gpib/tcp/serial/usb/custom）。
        address: 资源地址（GPIB 地址 / TCP 主机 / 串口路径）。
        port: TCP 端口。
        config: 附加配置（波特率等）。
    """

    model_config = ConfigDict(extra="forbid")

    type: CommunicationType = Field(..., description="通信类型")
    address: str | None = Field(default=None, description="资源地址（GPIB/TCP 主机/串口路径）")
    port: int | None = Field(default=None, description="TCP 端口")
    config: dict[str, Any] = Field(default_factory=dict, description="附加通信配置")


class Channel(BaseModel):
    """仪器通道（§8.3.2 Channel）。

    Attributes:
        id: 通道标识（如 "CH1"）。
        name: 通道名称。
        type: 信号类型（电压/电流/电阻/数字IO/RF/热）。
        direction: 方向（输入/输出/双向）。
        specs: 通道规格（额定电压/电流等）。
        status: 通道状态。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="通道标识")
    name: str = Field(default="", description="通道名称")
    type: ChannelType = Field(..., description="信号类型")
    direction: ChannelDirection = Field(..., description="方向")
    specs: dict[str, Any] = Field(default_factory=dict, description="通道规格")
    status: str = Field(default="idle", description="通道状态")


class Instrument(BaseModel):
    """仪器仪表（§8.3.2 Instrument）。

    Attributes:
        id: 仪器标识（如 "PSU_MAIN"）。
        name: 仪器名称。
        type: 仪器类型。
        model: 型号（如 "Chroma 62012P"）。
        manufacturer: 制造商。
        communication: 通信配置。
        channels: 通道列表。
        status: 仪器状态。
        position: 画布坐标。
        simulation_profile: 仿真配置文件名。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="仪器标识")
    name: str = Field(default="", description="仪器名称")
    type: InstrumentType = Field(..., description="仪器类型")
    model: str = Field(default="", description="型号")
    manufacturer: str = Field(default="", description="制造商")
    communication: CommunicationConfig = Field(
        default_factory=lambda: CommunicationConfig(type=CommunicationType.GPIB),
        description="通信配置",
    )
    channels: list[Channel] = Field(default_factory=list, description="通道列表")
    status: DeviceStatus = Field(default=DeviceStatus.OFFLINE, description="仪器状态")
    position: dict[str, float] = Field(default_factory=dict, description="画布坐标")
    simulation_profile: str | None = Field(None, description="仿真配置文件名")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class Relay(BaseModel):
    """继电器（§8.3.2 Relay）。

    Attributes:
        id: 继电器标识。
        type: 类型（spst/spdt/dpdt/matrix）。
        control_signal: 控制信号标识（GPIO/Modbus 寄存器等）。
        contacts: 触点（common/no/nc）。
        state: 状态（open/closed）。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="继电器标识")
    type: RelayType = Field(..., description="继电器类型")
    control_signal: str = Field(..., min_length=1, description="控制信号标识")
    contacts: dict[str, str | None] = Field(
        default_factory=lambda: {"common": "", "no": None, "nc": None},
        description="触点（common/no/nc）",
    )
    state: Literal["open", "closed"] = Field(default="open", description="触点状态")


class Actuator(BaseModel):
    """执行器（§8.3.2 Actuator）。

    Attributes:
        id: 执行器标识。
        type: 类型（cylinder/motor/valve）。
        controlMethod: 控制方式（gpio/modbus/tcp）。
        state: 状态（idle/moving/active）。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="执行器标识")
    type: ActuatorType = Field(..., description="执行器类型")
    controlMethod: Literal["gpio", "modbus", "tcp"] = Field(
        default="gpio", description="控制方式"
    )
    state: Literal["idle", "moving", "active"] = Field(default="idle", description="状态")


class Sensor(BaseModel):
    """传感器（§8.3.2 Sensor）。

    Attributes:
        id: 传感器标识。
        type: 类型（position/temperature/pressure/proximity/optical）。
        unit: 单位。
        value: 实时读数（运行时填充）。
        range: 量程（min/max）。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="传感器标识")
    type: SensorType = Field(..., description="传感器类型")
    unit: str = Field(default="", description="单位")
    value: float | None = Field(None, description="实时读数（运行时）")
    range: dict[str, float] | None = Field(None, description="量程（min/max）")


class Terminal(BaseModel):
    """夹具外部接线端子（§8.3.2 Fixture.terminals）。

    Attributes:
        id: 端子标识（如 "T1"）。
        name: 端子名称。
        type: 信号类型（与 ChannelType 一致）。
        direction: 方向。
        specs: 规格。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="端子标识")
    name: str = Field(default="", description="端子名称")
    type: ChannelType = Field(..., description="信号类型")
    direction: ChannelDirection = Field(..., description="方向")
    specs: dict[str, Any] = Field(default_factory=dict, description="规格")


class Fixture(BaseModel):
    """夹具（§8.3.2 Fixture）。

    夹具是具备主动控制能力的实体（气缸/继电器/传感器），而非被动连接体。

    Attributes:
        id: 夹具标识。
        name: 夹具名称。
        version: 夹具版本。
        terminals: 外部接线端子。
        relays: 内部继电器矩阵。
        sensors: 传感器（气缸位置/温度等）。
        actuators: 执行器（气缸/电机）。
        status: 夹具状态。
        dut_slot_count: DUT 槽位数。
        position: 画布坐标。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="夹具标识")
    name: str = Field(default="", description="夹具名称")
    version: str = Field(default="1.0", description="夹具版本")
    terminals: list[Terminal] = Field(default_factory=list, description="外部接线端子")
    relays: list[Relay] = Field(default_factory=list, description="内部继电器矩阵")
    sensors: list[Sensor] = Field(default_factory=list, description="传感器")
    actuators: list[Actuator] = Field(default_factory=list, description="执行器")
    status: FixtureStatus = Field(default=FixtureStatus.IDLE, description="夹具状态")
    dut_slot_count: int = Field(default=1, ge=1, description="DUT 槽位数")
    position: dict[str, float] = Field(default_factory=dict, description="画布坐标")


# ---------------------------------------------------------------------------
# 被测产品
# ---------------------------------------------------------------------------


class TestPoint(BaseModel):
    """DUT 测试点（§8.3.2 TestPoint）。

    注意：``__test__ = False`` 防止 pytest 将本类误收集为测试类。

    Attributes:
        id: 测试点标识。
        net: 网络名（电气节点）。
        type: 信号类型。
        expected_range: 期望范围（min/max）。
        measured_value: 测量值（运行时填充）。
        status: 状态。
    """

    __test__ = False  # 阻止 pytest 收集（类名以 Test 开头）

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="测试点标识")
    net: str = Field(..., min_length=1, description="网络名")
    type: ChannelType = Field(..., description="信号类型")
    expected_range: dict[str, float] | None = Field(None, description="期望范围（min/max）")
    measured_value: float | None = Field(None, description="测量值（运行时）")
    status: TestPointStatus = Field(default=TestPointStatus.IDLE, description="状态")


class PowerPin(BaseModel):
    """DUT 电源引脚（§8.3.2 DUT.power_pins）。

    Attributes:
        id: 引脚标识。
        net: 网络名。
        voltage: 额定电压（V）。
        max_current: 最大电流（A）。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="引脚标识")
    net: str = Field(..., min_length=1, description="网络名")
    voltage: float | None = Field(None, description="额定电压（V）")
    max_current: float | None = Field(None, description="最大电流（A）")


class DUT(BaseModel):
    """被测产品（§8.3.2 DUT）。

    Attributes:
        id: DUT 标识。
        product_model: 产品型号。
        serial_number: 序列号（运行时绑定）。
        test_points: 测试点列表。
        power_pins: 电源引脚列表。
        uutIndex: 多 UUT 时的索引。
        slot_index: 槽位索引。
        status: 状态。
        measurements: 测量值记录（运行时填充）。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="DUT 标识")
    product_model: str = Field(..., min_length=1, description="产品型号")
    serial_number: str | None = Field(None, description="序列号（运行时绑定）")
    test_points: list[TestPoint] = Field(default_factory=list, description="测试点列表")
    power_pins: list[PowerPin] = Field(default_factory=list, description="电源引脚列表")
    uutIndex: int = Field(default=0, description="多 UUT 时的索引")
    slot_index: int = Field(default=0, description="槽位索引")
    status: DUTStatus = Field(default=DUTStatus.IDLE, description="状态")
    measurements: dict[str, Any] = Field(default_factory=dict, description="测量值记录")


# ---------------------------------------------------------------------------
# 接线与信号路径
# ---------------------------------------------------------------------------


class LinkEndpoint(BaseModel):
    """链路端点（§8.3.2 LinkEndpoint）。

    Attributes:
        entity_type: 实体类型（仪器通道/夹具端子/DUT测试点/继电器触点）。
        entity_id: 实体标识。
        port_id: 端口/通道标识（如 "CH1"、"T2"）。
    """

    model_config = ConfigDict(extra="forbid")

    entity_type: LinkEndpointType = Field(..., description="实体类型")
    entity_id: str = Field(..., min_length=1, description="实体标识")
    port_id: str = Field(..., min_length=1, description="端口/通道标识")


class FaultInfo(BaseModel):
    """故障信息（§8.3.2 FaultInfo）。

    Attributes:
        type: 故障类型。
        severity: 严重度。
        message: 故障描述。
        detected_at: 检测时间戳。
        detected_by: 检测来源。
        suggestion: 修复建议。
    """

    model_config = ConfigDict(extra="forbid")

    type: FaultType = Field(..., description="故障类型")
    severity: Severity = Field(..., description="严重度")
    message: str = Field(..., description="故障描述")
    detected_at: float = Field(0.0, description="检测时间戳")
    detected_by: str = Field(default="", description="检测来源")
    suggestion: str | None = Field(None, description="修复建议")


class Link(BaseModel):
    """接线（§8.3.2 Link）。

    Attributes:
        id: 连线标识。
        from: 起点端点。
        to: 终点端点。
        signal_type: 信号类型（power/signal/ground/rf/thermal/air）。
        wire_gauge: 线规。
        max_current: 最大电流（A）。
        routeId: 关联的矩阵开关路由（有值说明经过矩阵开关）。
        status: 链路状态。
        fault_info: 故障信息（运行时填充）。
    """

    id: str = Field(..., min_length=1, description="连线标识")
    from_endpoint: LinkEndpoint = Field(..., alias="from", description="起点端点")
    to: LinkEndpoint = Field(..., description="终点端点")
    signal_type: LinkSignalType = Field(..., description="信号类型")
    wire_gauge: str | None = Field(None, description="线规")
    max_current: float | None = Field(None, description="最大电流（A）")
    routeId: str | None = Field(None, description="关联的矩阵开关路由")
    status: LinkStatus = Field(default=LinkStatus.IDLE, description="链路状态")
    fault_info: FaultInfo | None = Field(None, description="故障信息")

    # populate_by_name: 允许用 "from" 或 "from_endpoint" 构造/序列化
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Route(BaseModel):
    """信号路径（§8.3.2 Route）。

    Attributes:
        id: 路径标识。
        name: 路径名称（如 "VOUT 测量路径"）。
        links: 经过的连线 ID 列表。
        relays: 需闭合的继电器 ID 列表。
        active: 是否激活。
        associated_step: 关联测试步骤 ID。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="路径标识")
    name: str = Field(default="", description="路径名称")
    links: list[str] = Field(default_factory=list, description="经过的连线 ID 列表")
    relays: list[str] = Field(default_factory=list, description="需闭合的继电器 ID 列表")
    active: bool = Field(default=False, description="是否激活")
    associated_step: str | None = Field(None, description="关联测试步骤 ID")


# ---------------------------------------------------------------------------
# 顶层工装拓扑
# ---------------------------------------------------------------------------


class FixtureTopology(BaseModel):
    """工装拓扑（§8.3.2 FixtureTopology）。

    Attributes:
        id: 拓扑标识。
        name: 拓扑名称。
        version: 版本号。
        product_model: 适配的产品型号。
        instruments: 仪器列表。
        fixtures: 夹具列表。
        duts: DUT 列表。
        links: 接线列表。
        routes: 信号路径列表。
        created_at: 创建时间。
        updated_at: 更新时间。
        tags: 标签。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default="", description="拓扑标识")
    name: str = Field(..., min_length=1, description="拓扑名称")
    version: str = Field(default="1.0", description="版本号")
    product_model: str = Field(default="", description="适配的产品型号")
    instruments: list[Instrument] = Field(default_factory=list, description="仪器列表")
    fixtures: list[Fixture] = Field(default_factory=list, description="夹具列表")
    duts: list[DUT] = Field(default_factory=list, description="DUT 列表")
    links: list[Link] = Field(default_factory=list, description="接线列表")
    routes: list[Route] = Field(default_factory=list, description="信号路径列表")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), description="创建时间")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), description="更新时间")
    tags: list[str] = Field(default_factory=list, description="标签")

    def validate_topology(self) -> ValidationResult:
        """对本拓扑执行 8 类接线校验（§8.3.5）。

        Returns:
            ValidationResult 包含 errors/warnings/summary。
        """
        return TopologyValidator().validate(self)


# ---------------------------------------------------------------------------
# 校验引擎（§8.3.5 8 类检查）
# ---------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    """校验问题项。

    Attributes:
        level: 级别（error 阻断 / warning 提示）。
        code: 检查项代码。
        message: 问题描述。
        path: 关联的拓扑元素路径（如 "instrument:PSU_MAIN"）。
    """

    model_config = ConfigDict(extra="forbid")

    level: Literal["error", "warning"] = Field(..., description="级别")
    code: str = Field(..., description="检查项代码")
    message: str = Field(..., description="问题描述")
    path: str = Field(default="", description="关联的拓扑元素路径")


class ValidationResult(BaseModel):
    """校验结果。

    Attributes:
        valid: 是否通过（无 error 即通过）。
        errors: 错误列表（阻断执行）。
        warnings: 警告列表（仅提示）。
    """

    model_config = ConfigDict(extra="forbid")

    errors: list[ValidationIssue] = Field(default_factory=list, description="错误列表")
    warnings: list[ValidationIssue] = Field(default_factory=list, description="警告列表")

    @property
    def valid(self) -> bool:
        """是否通过（无 error 即通过）。"""
        return len(self.errors) == 0

    @property
    def summary(self) -> str:
        """人类可读摘要（如 "3 错误, 2 警告"）。"""
        return f"{len(self.errors)} 错误, {len(self.warnings)} 警告"

    def as_dict(self) -> dict[str, Any]:
        """转 dict 便于 API 返回。"""
        return {
            "valid": self.valid,
            "errors": [i.model_dump() for i in self.errors],
            "warnings": [i.model_dump() for i in self.warnings],
            "summary": self.summary,
        }


class TopologyValidator:
    """工装拓扑接线校验引擎（§8.3.5 8 类检查 + 辅助检查）。

    校验严格度：error 阻断执行，warning 仅提示（§6.7.2）。
    """

    #: 检查项代码前缀
    CHECK_PORT_TYPE = "port_type"
    CHECK_DIRECTION = "direction"
    CHECK_CONFLICT = "channel_conflict"
    CHECK_GROUND = "ground_integrity"
    CHECK_OCCUPANCY = "channel_occupancy"
    CHECK_ROUTE = "route_reachable"
    CHECK_FIXTURE_CONTROL = "fixture_control"
    CHECK_DUT_COVERAGE = "dut_coverage"
    CHECK_POWER = "power_capacity"
    CHECK_GPIB = "gpib_conflict"

    def __init__(self, strictness: Literal["error", "warning"] = "error") -> None:
        """初始化校验器。

        Args:
            strictness: 校验严格度。"error" 下通道冲突类检查为 error，
                "warning" 下降级为 warning（§6.7.2 可配置）。
        """
        self._strictness: Literal["error", "warning"] = strictness

    def validate(self, topology: FixtureTopology) -> ValidationResult:
        """执行全部校验并返回结果。

        Args:
            topology: 待校验的工装拓扑。

        Returns:
            ValidationResult 包含 errors/warnings。
        """
        result = ValidationResult()
        self._check_port_type_match(topology, result)
        self._check_signal_direction(topology, result)
        self._check_short_circuit_conflict(topology, result)
        self._check_ground_integrity(topology, result)
        self._check_channel_occupancy(topology, result)
        self._check_route_reachable(topology, result)
        self._check_fixture_control(topology, result)
        self._check_dut_coverage(topology, result)
        self._check_power_capacity(topology, result)
        self._check_gpib_conflict(topology, result)
        return result

    # ------------------------------------------------------------------
    # 检查项
    # ------------------------------------------------------------------

    #: Link.signal_type 与端点 ChannelType 的兼容映射
    _SIGNAL_TYPE_COMPAT: dict[LinkSignalType, set[ChannelType]] = {
        LinkSignalType.POWER: {ChannelType.VOLTAGE, ChannelType.CURRENT},
        LinkSignalType.SIGNAL: {
            ChannelType.VOLTAGE,
            ChannelType.CURRENT,
            ChannelType.RESISTANCE,
            ChannelType.DIGITAL_IO,
        },
        LinkSignalType.GROUND: {ChannelType.VOLTAGE},
        LinkSignalType.RF: {ChannelType.RF},
        LinkSignalType.THERMAL: {ChannelType.THERMAL},
        LinkSignalType.AIR: set(),
    }

    def _check_port_type_match(self, topo: FixtureTopology, result: ValidationResult) -> None:
        """检查 1 端口类型匹配（error）：连线声明的信号类型与端点类型兼容。"""
        for link in topo.links:
            compatible = self._SIGNAL_TYPE_COMPAT.get(link.signal_type)
            if compatible is None:
                continue
            src = self._resolve_endpoint(topo, link.from_endpoint)
            dst = self._resolve_endpoint(topo, link.to)
            if src is None or dst is None:
                # 端点无法解析由 direction 检查报告
                continue
            src_type = self._signal_type_of(src)
            dst_type = self._signal_type_of(dst)
            if src_type is not None and src_type not in compatible:
                self._add(
                    result,
                    self.CHECK_PORT_TYPE,
                    f"Link {link.id}: 起点 {link.from_endpoint.entity_id}."
                    f"{link.from_endpoint.port_id} 类型 {src_type} 与连线信号类型 "
                    f"{link.signal_type.value} 不匹配",
                    f"link:{link.id}",
                )
            if dst_type is not None and dst_type not in compatible:
                self._add(
                    result,
                    self.CHECK_PORT_TYPE,
                    f"Link {link.id}: 终点 {link.to.entity_id}.{link.to.port_id} 类型 "
                    f"{dst_type} 与连线信号类型 {link.signal_type.value} 不匹配",
                    f"link:{link.id}",
                )

    def _check_signal_direction(self, topo: FixtureTopology, result: ValidationResult) -> None:
        """检查 2 信号方向（error）：source 必须输出端口，target 必须输入端口。"""
        for link in topo.links:
            src = self._resolve_endpoint(topo, link.from_endpoint)
            dst = self._resolve_endpoint(topo, link.to)
            if src is None:
                self._add(
                    result,
                    self.CHECK_DIRECTION,
                    f"Link {link.id}: 起点端点无法解析 "
                    f"({link.from_endpoint.entity_type}:{link.from_endpoint.entity_id}."
                    f"{link.from_endpoint.port_id})",
                    f"link:{link.id}",
                )
                continue
            if dst is None:
                self._add(
                    result,
                    self.CHECK_DIRECTION,
                    f"Link {link.id}: 终点端点无法解析 "
                    f"({link.to.entity_type}:{link.to.entity_id}.{link.to.port_id})",
                    f"link:{link.id}",
                )
                continue
            if self._is_output_port(src) and self._is_input_port(dst):
                continue
            if not self._is_output_port(src):
                self._add(
                    result,
                    self.CHECK_DIRECTION,
                    f"Link {link.id}: 起点 {link.from_endpoint.entity_id}."
                    f"{link.from_endpoint.port_id} 不是输出端口",
                    f"link:{link.id}",
                )
            if not self._is_input_port(dst):
                self._add(
                    result,
                    self.CHECK_DIRECTION,
                    f"Link {link.id}: 终点 {link.to.entity_id}.{link.to.port_id} 不是输入端口",
                    f"link:{link.id}",
                )

    def _check_short_circuit_conflict(
        self, topo: FixtureTopology, result: ValidationResult
    ) -> None:
        """检查 3 短路/通道冲突（error）：同一输入端口不能被多条连线连接。"""
        target_counts: dict[tuple[str, str], list[str]] = {}
        for link in topo.links:
            key = (link.to.entity_id, link.to.port_id)
            target_counts.setdefault(key, []).append(link.id)
        for (entity_id, port_id), link_ids in target_counts.items():
            if len(link_ids) > 1:
                self._add(
                    result,
                    self.CHECK_CONFLICT,
                    f"端口 {entity_id}.{port_id} 被多条连线连接（短路风险）: "
                    f"{', '.join(link_ids)}",
                    f"port:{entity_id}.{port_id}",
                )

    def _check_ground_integrity(self, topo: FixtureTopology, result: ValidationResult) -> None:
        """检查 4 接地完整性（warning）：电源回路必须有地线连接。"""
        power_links = [l for l in topo.links if l.signal_type == LinkSignalType.POWER]
        if not power_links:
            return
        ground_count = sum(
            1 for l in topo.links if l.signal_type == LinkSignalType.GROUND
        )
        if ground_count == 0:
            self._add(
                result,
                self.CHECK_GROUND,
                "存在电源连线但无地线连接，电源回路接地完整性缺失",
                "topology",
                level="warning",
            )

    def _check_channel_occupancy(self, topo: FixtureTopology, result: ValidationResult) -> None:
        """检查 5 仪器通道占用（error）：同一通道不能同时接多个 DUT 测试点（除非矩阵开关）。"""
        # 通道 -> 连接的 DUT 测试点集合（按测试点 id 去重）
        channel_targets: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for link in topo.links:
            src = link.from_endpoint
            if src.entity_type != LinkEndpointType.INSTRUMENT_CHANNEL:
                continue
            dst = link.to
            if dst.entity_type == LinkEndpointType.DUT_TESTPOINT:
                key = (src.entity_id, src.port_id)
                channel_targets.setdefault(key, set()).add((dst.entity_id, dst.port_id))

        for (inst_id, ch_id), targets in channel_targets.items():
            if len(targets) > 1:
                # 检查是否有经过矩阵开关的路径（routeId 存在）
                has_matrix = any(
                    l.routeId
                    for l in topo.links
                    if l.from_endpoint.entity_id == inst_id
                    and l.from_endpoint.port_id == ch_id
                )
                if not has_matrix:
                    labels = [f"{d}.{p}" for d, p in sorted(targets)]
                    self._add(
                        result,
                        self.CHECK_OCCUPANCY,
                        f"仪器通道 {inst_id}.{ch_id} 同时接多个 DUT 测试点且无矩阵开关隔离: "
                        f"{', '.join(labels)}",
                        f"channel:{inst_id}.{ch_id}",
                    )

    def _check_route_reachable(self, topo: FixtureTopology, result: ValidationResult) -> None:
        """检查 6 矩阵开关路由可达（error）：声明 routeId 的连线必须有对应继电器路径。"""
        routes = {r.id: r for r in topo.routes}
        all_relay_ids = {r.id for f in topo.fixtures for r in f.relays}

        # 路由需闭合的继电器必须存在（独立遍历所有路由）
        for route in topo.routes:
            for relay_id in route.relays:
                if relay_id not in all_relay_ids:
                    self._add(
                        result,
                        self.CHECK_ROUTE,
                        f"路由 {route.name} 引用的继电器 {relay_id} 在拓扑中不存在",
                        f"route:{route.id}",
                    )

        for link in topo.links:
            if not link.routeId:
                continue
            declared_route = routes.get(link.routeId)
            if declared_route is None:
                self._add(
                    result,
                    self.CHECK_ROUTE,
                    f"Link {link.id} 声明路由 {link.routeId} 但拓扑中不存在该路由",
                    f"link:{link.id}",
                )
                continue
            if link.id not in declared_route.links:
                self._add(
                    result,
                    self.CHECK_ROUTE,
                    f"Link {link.id} 声明的路由 {declared_route.name} 未包含该连线",
                    f"link:{link.id}",
                )

    def _check_fixture_control(self, topo: FixtureTopology, result: ValidationResult) -> None:
        """检查 7 夹具控制完整性（error）：夹具气缸/继电器必须有控制源连接。"""
        for fixture in topo.fixtures:
            for relay in fixture.relays:
                if not relay.control_signal:
                    self._add(
                        result,
                        self.CHECK_FIXTURE_CONTROL,
                        f"夹具 {fixture.id} 继电器 {relay.id} 缺少控制信号 (control_signal)",
                        f"fixture:{fixture.id}.relay:{relay.id}",
                    )
            for actuator in fixture.actuators:
                if not actuator.controlMethod:
                    self._add(
                        result,
                        self.CHECK_FIXTURE_CONTROL,
                        f"夹具 {fixture.id} 执行器 {actuator.id} 缺少控制方式 (controlMethod)",
                        f"fixture:{fixture.id}.actuator:{actuator.id}",
                    )

    def _check_dut_coverage(self, topo: FixtureTopology, result: ValidationResult) -> None:
        """检查 8 DUT 测试点覆盖（warning）：产品规格要求的测试点必须全部接线。"""
        # 已接线的 DUT 测试点集合
        connected: set[tuple[str, str]] = set()
        for link in topo.links:
            if link.to.entity_type == LinkEndpointType.DUT_TESTPOINT:
                connected.add((link.to.entity_id, link.to.port_id))

        for dut in topo.duts:
            for tp in dut.test_points:
                # 有期望范围的测试点视为规格要求项
                if tp.expected_range is not None and (dut.id, tp.id) not in connected:
                    self._add(
                        result,
                        self.CHECK_DUT_COVERAGE,
                        f"DUT {dut.id} 规格测试点 {tp.id} (net={tp.net}) 未接线",
                        f"dut:{dut.id}.testpoint:{tp.id}",
                        level="warning",
                    )

    def _check_power_capacity(self, topo: FixtureTopology, result: ValidationResult) -> None:
        """辅助检查 电源容量（warning）：链路 max_current 与通道额定电流校验。"""
        for link in topo.links:
            if link.signal_type != LinkSignalType.POWER or link.max_current is None:
                continue
            src = self._resolve_endpoint(topo, link.from_endpoint)
            if src is None or src.get("channel") is None:
                continue
            rated = self._channel_rated_current(src["channel"])
            if rated is not None and link.max_current > rated:
                self._add(
                    result,
                    self.CHECK_POWER,
                    f"Link {link.id} 载流 {link.max_current}A 超过通道 "
                    f"{link.from_endpoint.entity_id}.{link.from_endpoint.port_id} 额定 "
                    f"{rated}A",
                    f"link:{link.id}",
                    level="warning",
                )

    def _check_gpib_conflict(self, topo: FixtureTopology, result: ValidationResult) -> None:
        """辅助检查 GPIB 地址冲突（error）：同一 GPIB 地址不能复用。"""
        gpib_addresses: dict[str, str] = {}
        for inst in topo.instruments:
            comm = inst.communication
            if comm.type == CommunicationType.GPIB and comm.address:
                addr = comm.address
                if addr in gpib_addresses:
                    self._add(
                        result,
                        self.CHECK_GPIB,
                        f"仪器 {inst.id} 与 {gpib_addresses[addr]} GPIB 地址冲突: {addr}",
                        f"instrument:{inst.id}",
                    )
                else:
                    gpib_addresses[addr] = inst.id

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _add(
        self,
        result: ValidationResult,
        code: str,
        message: str,
        path: str,
        level: Literal["error", "warning"] | None = None,
    ) -> None:
        """按严格度添加问题。"""
        effective_level = level or self._strictness
        issue = ValidationIssue(level=effective_level, code=code, message=message, path=path)
        if effective_level == "error":
            result.errors.append(issue)
        else:
            result.warnings.append(issue)

    def _resolve_endpoint(
        self, topo: FixtureTopology, endpoint: LinkEndpoint
    ) -> dict[str, Any] | None:
        """解析链路端点为实体信息 dict。

        返回 dict 含 ``entity`` 与 ``channel``（仪器通道/夹具端子/测试点）。
        """
        if endpoint.entity_type == LinkEndpointType.INSTRUMENT_CHANNEL:
            for inst in topo.instruments:
                if inst.id == endpoint.entity_id:
                    for ch in inst.channels:
                        if ch.id == endpoint.port_id:
                            return {"entity": inst, "channel": ch}
        elif endpoint.entity_type == LinkEndpointType.FIXTURE_TERMINAL:
            for fixture in topo.fixtures:
                if fixture.id == endpoint.entity_id:
                    for term in fixture.terminals:
                        if term.id == endpoint.port_id:
                            return {"entity": fixture, "channel": term}
        elif endpoint.entity_type == LinkEndpointType.DUT_TESTPOINT:
            for dut in topo.duts:
                if dut.id == endpoint.entity_id:
                    for tp in dut.test_points:
                        if tp.id == endpoint.port_id:
                            return {"entity": dut, "channel": tp}
        return None

    def _signal_type_of(self, resolved: dict[str, Any]) -> str | None:
        """取端点的信号类型。"""
        channel = resolved.get("channel")
        if channel is None:
            return None
        # Channel.type / Terminal.type / TestPoint.type 均为 ChannelType 枚举
        t = getattr(channel, "type", None)
        if t is None:
            return None
        return t.value if isinstance(t, ChannelType) else str(t)

    def _is_output_port(self, resolved: dict[str, Any]) -> bool:
        """端口是否为输出方向（source 端）。

        DUT 测试点/继电器触点等无 direction 字段的端点不能作为输出。
        """
        channel = resolved.get("channel")
        if channel is None:
            return False
        direction = getattr(channel, "direction", None)
        if direction is None:
            return False
        return direction in (ChannelDirection.OUTPUT, ChannelDirection.BIDIRECTIONAL)

    def _is_input_port(self, resolved: dict[str, Any]) -> bool:
        """端口是否为输入方向（target 端）。

        DUT 测试点无 direction 字段（§8.3.2 TestPoint），作为信号接收端
        默认视为输入放行。
        """
        channel = resolved.get("channel")
        if channel is None:
            return False
        direction = getattr(channel, "direction", None)
        if direction is None:
            return True
        return direction in (ChannelDirection.INPUT, ChannelDirection.BIDIRECTIONAL)

    def _channel_rated_current(self, channel: Any) -> float | None:
        """取通道额定电流（specs.max_current / specs.rated_current）。"""
        specs = getattr(channel, "specs", None) or {}
        for key in ("max_current", "rated_current"):
            value = specs.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None


# ---------------------------------------------------------------------------
# Parse / serialize
# ---------------------------------------------------------------------------


def parse_fixture_topology(yaml_str: str) -> FixtureTopology:
    """Parse a YAML string into a FixtureTopology.

    Args:
        yaml_str: YAML content representing a single fixture topology.

    Returns:
        Validated FixtureTopology instance.

    Raises:
        yaml.YAMLError: If the YAML is malformed.
        pydantic.ValidationError: If the parsed data fails schema validation.
    """
    data = yaml.safe_load(yaml_str)
    return FixtureTopology.model_validate(data)


def serialize_fixture_topology(topology: FixtureTopology) -> str:
    """Serialize a FixtureTopology to a YAML string.

    Uses ``sort_keys=False`` to preserve field definition order for
    deterministic, human-readable output.

    Args:
        topology: FixtureTopology instance to serialize.

    Returns:
        YAML string representation.
    """
    result: str = yaml.safe_dump(
        topology.model_dump(mode="json", by_alias=True),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return result
