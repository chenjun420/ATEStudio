"""MockGPIBGateway 虚拟 GPIB 网关测试 — 总线互斥/寻址/SCPI 分发/串行查询/SRQ。

设计文档 §7.5 AC-4：五类虚拟仪器与真实硬件路径 API 兼容。测试不依赖任何
socket/端口 —— 网关为纯内存总线，客户端经 ``execute()`` 收发 SCPI。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from ate_platform.drivers.mock.mock_gpib_gateway import (
    FACTORY_KEY,
    STB_SRQ_BIT,
    GPIBAddressError,
    GPIBTimeoutError,
    MeasurementDevice,
    MockGPIBGateway,
    VirtualGPIBDevice,
)
from ate_platform.simulation.fault_injector import FaultInjector, FaultRule

ADDR_A = 8
ADDR_B = 9


# ---------------------------------------------------------------------------
# 构造辅助 — 双设备总线（地址 8/9），身份可区分
# ---------------------------------------------------------------------------


def make_gateway(**kwargs: object) -> tuple[MockGPIBGateway, MeasurementDevice, MeasurementDevice]:
    """构造挂了两台 DMM 的网关（addr 8 = 3.3V 表，addr 9 = 12V 表）。"""
    gateway = MockGPIBGateway(**kwargs)  # type: ignore[arg-type]
    dmm_a = MeasurementDevice("MockDMM,MTX-1000,SN000A", voltage=3.3)
    dmm_b = MeasurementDevice("MockDMM,MTX-2000,SN000B", voltage=12.0)
    gateway.attach(dmm_a, ADDR_A)
    gateway.attach(dmm_b, ADDR_B)
    return gateway, dmm_a, dmm_b


# ---------------------------------------------------------------------------
# 注册钩子（集中注册稍后接入 MockDriverFactory — T9 接线）
# ---------------------------------------------------------------------------


class _FakeFactory:
    def __init__(self) -> None:
        self.registered: dict[str, type] = {}

    def register(self, key: str, driver_cls: type) -> None:
        self.registered[key] = driver_cls


def test_register_hook_uses_gpib_gateway_key() -> None:
    from ate_platform.drivers.mock.mock_gpib_gateway import register

    factory = _FakeFactory()
    register(factory)
    assert factory.registered[FACTORY_KEY] is MockGPIBGateway
    assert FACTORY_KEY == "gpib_gateway"


# ---------------------------------------------------------------------------
# 挂载 / 地址表
# ---------------------------------------------------------------------------


def test_attach_lists_addresses_and_detach() -> None:
    gateway, _dmm_a, _dmm_b = make_gateway()
    assert gateway.addresses == (ADDR_A, ADDR_B)
    gateway.detach(ADDR_B)
    assert gateway.addresses == (ADDR_A,)
    with pytest.raises(GPIBAddressError, match="not attached"):
        gateway.detach(ADDR_B)


# ---------------------------------------------------------------------------
# 寻址与路由
# ---------------------------------------------------------------------------


async def test_select_then_execute_routes_to_addressed_device() -> None:
    gateway, _a, _b = make_gateway()
    gateway.select(ADDR_A)
    assert "MTX-1000" in await gateway.execute("*IDN?")
    gateway.select(ADDR_B)
    assert "MTX-2000" in await gateway.execute("*IDN?")


async def test_explicit_address_overrides_selection() -> None:
    gateway, _a, _b = make_gateway()
    gateway.select(ADDR_A)
    assert "MTX-2000" in await gateway.execute("*IDN?", ADDR_B)
    assert gateway.selected_address == ADDR_A, "explicit address must not move selection"


async def test_adr_scpi_style_select_query_and_alt_form() -> None:
    gateway, _a, _b = make_gateway()
    gateway.select(ADDR_A)
    await gateway.execute(f"ADR {ADDR_B}")
    assert gateway.selected_address == ADDR_B
    assert await gateway.execute("ADR?") == str(ADDR_B)
    # 备用写法 *ADR（任务书 "*ADR?-style"）
    await gateway.execute(f"*ADR {ADDR_A}")
    assert gateway.selected_address == ADDR_A
    assert await gateway.execute("*ADR?") == str(ADDR_A)


async def test_adr_to_unknown_address_raises() -> None:
    gateway, _a, _b = make_gateway()
    gateway.select(ADDR_A)
    with pytest.raises(GPIBAddressError, match="99"):
        await gateway.execute("ADR 99")
    assert gateway.selected_address == ADDR_A, "failed select must not change state"


async def test_execute_without_address_raises() -> None:
    gateway, _a, _b = make_gateway()
    with pytest.raises(GPIBAddressError, match="no device addressed"):
        await gateway.execute("*IDN?")


async def test_execute_at_unattached_address_raises() -> None:
    gateway, _a, _b = make_gateway()
    with pytest.raises(GPIBAddressError, match="12"):
        await gateway.execute("*IDN?", 12)


# ---------------------------------------------------------------------------
# SCPI 分发 — IEEE-488.2 公共命令 + 测量查询
# ---------------------------------------------------------------------------


async def test_idn_returns_identity() -> None:
    gateway, _a, _b = make_gateway()
    gateway.select(ADDR_A)
    assert await gateway.execute("*IDN?") == "MockDMM,MTX-1000,SN000A"


async def test_rst_restores_default_state() -> None:
    gateway, dmm_a, _b = make_gateway()
    gateway.select(ADDR_A)
    default_voltage = float(await gateway.execute("MEAS:VOLT:DC?"))
    dmm_a.voltage = 24.0
    assert float(await gateway.execute("MEAS:VOLT:DC?")) == pytest.approx(24.0)
    await gateway.execute("*RST")
    assert float(await gateway.execute("MEAS:VOLT:DC?")) == pytest.approx(default_voltage)


async def test_measurement_queries_return_scientific_notation() -> None:
    gateway, _a, _b = make_gateway()
    gateway.select(ADDR_A)
    volt = float(await gateway.execute("MEAS:VOLT:DC?"))
    curr = float(await gateway.execute("MEAS:CURR:DC?"))
    res = float(await gateway.execute("MEAS:RES?"))
    assert volt == pytest.approx(3.3)
    assert curr == pytest.approx(0.5)
    assert res == pytest.approx(1000.0)


async def test_opc_and_stb_common_commands() -> None:
    gateway, _a, _b = make_gateway()
    gateway.select(ADDR_A)
    assert await gateway.execute("*OPC?") == "1"
    assert int(await gateway.execute("*STB?")) == 0


async def test_unknown_command_returns_error_and_queues() -> None:
    gateway, _a, _b = make_gateway()
    gateway.select(ADDR_A)
    resp = await gateway.execute("FROB:MODE ON")
    assert resp.startswith("-113"), f"unknown query must answer -113, got {resp!r}"
    assert "Undefined header" in resp
    # 错误入队：SYST:ERR? 弹出后再读为空
    err = await gateway.execute("SYST:ERR?")
    assert err.startswith("-113")
    assert await gateway.execute("SYST:ERR?") == '0,"No error"'


async def test_cls_clears_errors_and_status() -> None:
    gateway, dmm_a, _b = make_gateway()
    gateway.select(ADDR_A)
    await gateway.execute("FROB?")  # 入队一条 -113
    dmm_a.set_status_bits(0x04)
    dmm_a.request_service()
    await gateway.execute("*CLS")
    assert int(await gateway.execute("*STB?")) == 0
    assert await gateway.execute("SYST:ERR?") == '0,"No error"'


# ---------------------------------------------------------------------------
# 总线互斥 — 同板串行、跨板并行
# ---------------------------------------------------------------------------


async def test_bus_mutex_serializes_concurrent_commands() -> None:
    latency = 0.08
    gateway, _a, _b = make_gateway(command_latency_s=latency)
    gateway.select(ADDR_A)

    t1 = asyncio.create_task(gateway.execute("MEAS:VOLT:DC?"))
    await asyncio.sleep(0.01)  # 确保 t1 先持锁
    t2 = asyncio.create_task(gateway.execute("MEAS:VOLT:DC?"))

    start = time.monotonic()
    r1, r2 = await asyncio.gather(t1, t2)
    elapsed = time.monotonic() - start

    assert float(r1) == pytest.approx(3.3)
    assert float(r2) == pytest.approx(3.3)
    # 串行化：第二条命令必须等第一条的 80ms 时延结束才开始 → 总时长 ≥ ~160ms。
    # 若互斥失效则两条并发 ≈80ms 完成。
    assert elapsed >= latency * 1.75, f"bus mutex violated: {elapsed:.4f}s for 2x{latency}s"


async def test_same_board_lock_shared_across_gateway_instances() -> None:
    """同 board_id 的两个网关实例共享模块级总线锁（真实 GPIB 板语义）。"""
    latency = 0.08
    gw1, _a1, _b1 = make_gateway(command_latency_s=latency)
    gw2, _a2, _b2 = make_gateway(command_latency_s=latency)
    assert gw1.board_id == gw2.board_id == 0

    t1 = asyncio.create_task(gw1.execute("MEAS:VOLT:DC?", ADDR_A))
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(gw2.execute("MEAS:VOLT:DC?", ADDR_A))

    start = time.monotonic()
    await asyncio.gather(t1, t2)
    elapsed = time.monotonic() - start
    assert elapsed >= latency * 1.75, f"cross-instance bus lock missing: {elapsed:.4f}s"


async def test_different_boards_do_not_serialize() -> None:
    gw_slow, _a, _b = make_gateway(board_id=0, command_latency_s=0.10)
    gw_fast, _c, _d = make_gateway(board_id=1, command_latency_s=0.02)

    start = time.monotonic()
    fast_result = await gw_fast.execute("MEAS:VOLT:DC?", ADDR_A)
    fast_elapsed = time.monotonic() - start
    assert float(fast_result) == pytest.approx(3.3)
    # 快板（20ms）不被慢板（100ms）阻塞：不等慢板完成即应返回
    assert fast_elapsed < 0.09, f"independent boards serialized unexpectedly: {fast_elapsed:.4f}s"
    await gw_slow.execute("MEAS:VOLT:DC?", ADDR_A)  # 收尾，确保无悬挂任务


# ---------------------------------------------------------------------------
# 串行查询（serial poll）与 SRQ
# ---------------------------------------------------------------------------


async def test_serial_poll_reflects_srq_bit_set_and_clear() -> None:
    gateway, dmm_a, _b = make_gateway()
    gateway.select(ADDR_A)
    assert await gateway.read_status_byte(ADDR_A) == 0

    dmm_a.set_status_bits(0x04)
    sb = await gateway.read_status_byte(ADDR_A)
    assert sb & 0x04 and not sb & STB_SRQ_BIT

    dmm_a.request_service()
    sb = await gateway.read_status_byte(ADDR_A)
    assert sb & STB_SRQ_BIT, "request_service must set bit 6"

    dmm_a.clear_service_request()
    sb = await gateway.read_status_byte(ADDR_A)
    assert not sb & STB_SRQ_BIT and sb & 0x04, "clear must only drop bit 6"

    # read_status_byte 无参时走当前选址
    assert await gateway.read_status_byte() == await gateway.read_status_byte(ADDR_A)


async def test_srq_wait_receives_fifo_addresses() -> None:
    gateway, dmm_a, dmm_b = make_gateway()
    assert not gateway.srq_pending

    dmm_a.request_service()
    dmm_b.request_service()
    assert gateway.srq_pending

    first = await gateway.wait_srq(timeout=1.0)
    second = await gateway.wait_srq(timeout=1.0)
    assert (first, second) == (ADDR_A, ADDR_B), "SRQ queue must be FIFO"
    assert not gateway.srq_pending


async def test_srq_listener_fires_with_address() -> None:
    gateway, dmm_a, _b = make_gateway()
    seen: list[int] = []
    gateway.add_srq_listener(seen.append)
    dmm_a.request_service()
    assert seen == [ADDR_A], "listener must fire synchronously with asserting address"
    gateway.remove_srq_listener(seen.append)
    dmm_a.clear_service_request()
    dmm_a.request_service()
    assert seen == [ADDR_A], "removed listener must not fire again"


async def test_wait_srq_timeout_raises() -> None:
    gateway, _a, _b = make_gateway()
    start = time.monotonic()
    with pytest.raises(GPIBTimeoutError):
        await gateway.wait_srq(timeout=0.05)
    assert time.monotonic() - start < 1.0, "timeout must be honored promptly"


# ---------------------------------------------------------------------------
# 多设备隔离
# ---------------------------------------------------------------------------


async def test_multi_device_state_isolation() -> None:
    gateway, dmm_a, _b = make_gateway()
    gateway.select(ADDR_A)
    dmm_a.voltage = 42.0
    assert float(await gateway.execute("MEAS:VOLT:DC?", ADDR_A)) == pytest.approx(42.0)
    assert float(await gateway.execute("MEAS:VOLT:DC?", ADDR_B)) == pytest.approx(12.0)
    # 各自独立的错误队列
    await gateway.execute("FROB?", ADDR_A)
    assert (await gateway.execute("SYST:ERR?", ADDR_A)).startswith("-113")
    assert await gateway.execute("SYST:ERR?", ADDR_B) == '0,"No error"'


# ---------------------------------------------------------------------------
# 故障钩子 — 手动一次性注入 + FaultInjector 集成
# ---------------------------------------------------------------------------


async def test_inject_timeout_once_raises_then_recovers() -> None:
    gateway, _a, _b = make_gateway()
    gateway.select(ADDR_A)
    gateway.inject_timeout_once()
    with pytest.raises(GPIBTimeoutError, match="injected"):
        await gateway.execute("*IDN?")
    # 一次性：下一条命令恢复正常
    assert "MockDMM" in await gateway.execute("*IDN?")


async def test_inject_error_once_returns_error_response() -> None:
    gateway, _a, _b = make_gateway()
    gateway.select(ADDR_A)
    gateway.inject_error_once(code=-110, message="Injected comm error")
    resp = await gateway.execute("*IDN?")
    assert resp.startswith("-110")
    assert "Injected comm error" in resp
    # 错误同样入队，且后续命令恢复
    assert (await gateway.execute("SYST:ERR?")).startswith("-110")
    assert "MockDMM" in await gateway.execute("*IDN?")


async def test_fault_injector_timeout_rule_raises_once() -> None:
    injector = FaultInjector(seed=7)
    injector.add_rule(
        FaultRule(
            "gpib_timeout_once",
            layer="network",
            target="gpib0",
            method="transfer",
            trigger={"type": "count", "value": 1},
            action={"type": "timeout"},
            once=True,
        )
    )
    gateway, _a, _b = make_gateway(fault_injector=injector)
    gateway.select(ADDR_A)
    with pytest.raises(GPIBTimeoutError, match="gpib_timeout_once"):
        await gateway.execute("*IDN?")
    assert "MockDMM" in await gateway.execute("*IDN?"), "once-rule must not fire twice"


async def test_fault_injector_scpi_error_rule_returns_code() -> None:
    injector = FaultInjector(seed=7)
    injector.add_rule(
        FaultRule(
            "gpib_scpi_err_once",
            layer="protocol",
            target="gpib0",
            method="transfer",
            trigger={"type": "count", "value": 1},
            action={"type": "scpi_error", "code": -123, "message": "Injected protocol fault"},
            once=True,
        )
    )
    gateway, _a, _b = make_gateway(fault_injector=injector)
    gateway.select(ADDR_A)
    resp = await gateway.execute("*IDN?")
    assert resp.startswith("-123")
    assert "Injected protocol fault" in resp
    assert "MockDMM" in await gateway.execute("*IDN?")


# ---------------------------------------------------------------------------
# 设备扩展性 — 自定义 handler 子类
# ---------------------------------------------------------------------------


async def test_custom_device_subclass_dispatch() -> None:
    class RelayDevice(VirtualGPIBDevice):
        def __init__(self) -> None:
            super().__init__("MockRELAY,MRL-4000,SN000R")
            self.closed = True

        def handle_scpi(self, command: str) -> str | None:
            cmd = command.upper().strip()
            if cmd == "CLOSE":
                self.closed = True
                return None
            if cmd == "OPEN":
                self.closed = False
                return None
            if cmd == "CLOSE?":
                return "1" if self.closed else "0"
            raise LookupError(command)

    gateway = MockGPIBGateway()
    relay = RelayDevice()
    gateway.attach(relay, 5)
    gateway.select(5)
    assert await gateway.execute("OPEN") is None
    assert await gateway.execute("CLOSE?") == "0"
    await gateway.execute("CLOSE")
    assert await gateway.execute("CLOSE?") == "1"
