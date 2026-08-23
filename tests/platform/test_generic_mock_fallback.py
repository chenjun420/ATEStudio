"""T9 — GenericMockSCPI/TCP 兜底 mock + MockDriverFactory 字符串注册表接线测试。

覆盖：未知键回退 + 大声告警（caplog）、已知键 dmm/psu/eload/gpib_gateway/
tcp_device 解析、既有注册不被覆盖、GenericMockSCPI 的 *IDN?/responses 映射/
未知命令记录、GenericMockTCP 字节回显。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from ate_platform.drivers.examples.chroma_eload import ChromaEloadAbstraction, _MockEloadDriver
from ate_platform.drivers.examples.dmm import DMMAbstraction
from ate_platform.drivers.examples.psu import PSUAbstraction
from ate_platform.drivers.mock.generic_mock import DEFAULT_SCPI_IDENTITY, GenericMockSCPI, GenericMockTCP
from ate_platform.drivers.mock.mock_gpib_gateway import FACTORY_KEY as GPIB_KEY
from ate_platform.drivers.mock.mock_gpib_gateway import MockGPIBGateway
from ate_platform.drivers.mock.mock_tcp_device import FACTORY_KEY as TCP_KEY
from ate_platform.drivers.mock.mock_tcp_device import MockTCPDevice
from ate_platform.drivers.mock_factory import (
    MockDriverFactory,
    _MockDMMDriver,
    _MockPSUDriver,
)

_ECHO_PAYLOAD = b"\xaa\x55PING-frame-01"


async def _echo_roundtrip(port: int, payload: bytes = _ECHO_PAYLOAD) -> bytes:
    """连接 → 发送 → 等待等长回显（socket 操作置于测试函数之外的辅助协程）。"""
    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=5.0)
    try:
        writer.write(payload)
        await asyncio.wait_for(writer.drain(), timeout=5.0)
        return await asyncio.wait_for(reader.readexactly(len(payload)), timeout=5.0)
    finally:
        writer.close()


class TestFactoryStringRegistry:
    """字符串注册表：已知键解析 + T7/T8 钩子接线。"""

    def test_known_scpi_keys_resolve_to_their_mocks(self) -> None:
        assert MockDriverFactory.get_mock_class("dmm") is _MockDMMDriver
        assert MockDriverFactory.get_mock_class("psu") is _MockPSUDriver
        assert MockDriverFactory.get_mock_class("eload") is _MockEloadDriver

    def test_gpib_and_tcp_hooks_registered_under_factory_keys(self) -> None:
        """T7/T8 模块级 register(factory) 钩子已接入集中工厂。"""
        assert GPIB_KEY == "gpib_gateway"
        assert TCP_KEY == "tcp_device"
        assert MockDriverFactory._MOCK_REGISTRY[GPIB_KEY] is MockGPIBGateway
        assert MockDriverFactory._MOCK_REGISTRY[TCP_KEY] is MockTCPDevice

    def test_specific_abstraction_registrations_not_overridden(self) -> None:
        """既有抽象类键注册（create_mock 路径）行为不变。"""
        dmm = MockDriverFactory.create_mock(DMMAbstraction)
        psu = MockDriverFactory.create_mock(PSUAbstraction)
        eload = MockDriverFactory.create_mock(ChromaEloadAbstraction)
        assert isinstance(dmm, DMMAbstraction)
        assert isinstance(psu, PSUAbstraction)
        assert isinstance(eload, ChromaEloadAbstraction)

    def test_register_is_case_insensitive_and_normalizes(self) -> None:
        try:
            MockDriverFactory.register("  CUSTOM-KEY ", GenericMockSCPI)
            assert MockDriverFactory.get_mock_class("custom-key") is GenericMockSCPI
        finally:
            MockDriverFactory._MOCK_REGISTRY.pop("custom-key", None)


class TestFallbackResolution:
    """未知型号 → 可用通用 mock + 大声告警（绝不静默掩盖配置笔误）。"""

    def test_unknown_key_returns_generic_scpi_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="ate_platform.drivers.mock_factory"):
            driver_cls = MockDriverFactory.get_mock_class("typo-model-xyz")
        assert driver_cls is GenericMockSCPI
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("typo-model-xyz" in r.getMessage() for r in warnings), "warning must name the unmatched key"

    def test_tcp_prefixed_unknown_id_returns_generic_tcp(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="ate_platform.drivers.mock_factory"):
            driver_cls = MockDriverFactory.get_mock_class("TCP::127.0.0.1:4001")
        assert driver_cls is GenericMockTCP
        assert any("TCP::127.0.0.1:4001" in r.getMessage() for r in caplog.records)

    def test_resolve_mock_unknown_key_yields_usable_mock(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="ate_platform.drivers.mock_factory"):
            mock = MockDriverFactory.resolve_mock("mystery-instrument")
        assert isinstance(mock, GenericMockSCPI)
        mock.connect("MOCK::MYSTERY")
        assert mock.is_connected
        assert mock.query("*IDN?") == DEFAULT_SCPI_IDENTITY
        assert any("mystery-instrument" in r.getMessage() for r in caplog.records)

    def test_resolve_mock_rejects_bespoke_server_mocks(self) -> None:
        """网关/TCP 设备需定制构造参数——不得被错误实例化。"""
        with pytest.raises(TypeError, match="bespoke constructor"):
            MockDriverFactory.resolve_mock("gpib_gateway")


class TestGenericMockSCPI:
    """:class:`GenericMockSCPI` 行为。"""

    def test_idn_query_returns_identity(self) -> None:
        mock = GenericMockSCPI(identity="Acme,MODEL-X,123,1.0")
        mock.connect("MOCK::X")
        assert mock.query("*idn?") == "Acme,MODEL-X,123,1.0"  # 大小写不敏感

    def test_configurable_responses_map_case_insensitive(self) -> None:
        mock = GenericMockSCPI(responses={"meas:volt:dc?": "4.200000"})
        mock.connect("MOCK::X")
        assert mock.query("MEAS:VOLT:DC?") == "4.200000"
        assert mock.query("meas:volt:dc?") == "4.200000"

    def test_unknown_commands_recorded_in_order(self) -> None:
        mock = GenericMockSCPI(default_response="0")
        mock.connect("MOCK::X")
        assert mock.query("CONF:MYSTERY 1") == "0"
        mock.write("CAL:SECRET")
        assert mock.unknown_commands == ["CONF:MYSTERY 1", "CAL:SECRET"]
        assert mock.written_commands == ["CAL:SECRET"]

    def test_requires_connect_before_use(self) -> None:
        mock = GenericMockSCPI()
        with pytest.raises(RuntimeError, match="Not connected"):
            mock.query("*IDN?")


class TestGenericMockTCPEcho:
    """:class:`GenericMockTCP` 原始字节透传回显。"""

    async def test_echoes_frames_back(self) -> None:
        device = GenericMockTCP(resource_id="TCP::fallback")
        await device.start()
        try:
            assert device.port > 0
            echoed = await _echo_roundtrip(device.port)
            assert echoed == _ECHO_PAYLOAD
        finally:
            await device.stop()
        assert device.connection_count == 0  # stop 后无残留连接

    async def test_stop_is_idempotent(self) -> None:
        device = GenericMockTCP()
        await device.start()
        await device.stop()
        await device.stop()  # 二次 stop 不抛错
        with pytest.raises(RuntimeError, match="not started"):
            _ = device.port
