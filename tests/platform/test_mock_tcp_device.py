"""MockTCPDevice 虚拟仪器测试 — asyncio TCP 服务器 + 协议 YAML 帧协议。

帧字节经 tests/platform/helpers_mock_tcp.py 独立构造/解析（不依赖被测模块
编解码器），保证线路格式断言独立于实现。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from ate_platform.drivers.mock.mock_tcp_device import (
    ERR_BAD_ADDR,
    ERR_BAD_CHECKSUM,
    ERR_BAD_HEAD,
    ERR_LEN_TOO_LONG,
    ERR_UNKNOWN_CMD,
    FACTORY_KEY,
    MockTCPDevice,
    ProtocolConfigError,
    ProtocolSpec,
    load_protocol,
)
from tests.platform.helpers_mock_tcp import (
    HEAD,
    FrameClient,
    build_frame,
    checksum,
    parse_response,
    wait_connections,
)

FIXTURE_YAML = Path(__file__).resolve().parents[1] / "fixtures" / "mock_tcp_protocol.yaml"


# ---------------------------------------------------------------------------
# 设备生命周期辅助 — 每个测试结束确保服务器停止（无残留端口/连接）
# ---------------------------------------------------------------------------


@asynccontextmanager
async def running_device(
    protocol: ProtocolSpec | None = None, **kwargs: object
) -> AsyncIterator[MockTCPDevice]:
    device = MockTCPDevice(protocol if protocol is not None else load_protocol(FIXTURE_YAML), **kwargs)
    await device.start()
    try:
        yield device
    finally:
        await device.stop()
        assert device.connection_count == 0, "server stopped with lingering connections"


def xor_protocol_spec() -> ProtocolSpec:
    """与 fixture 相同布局但校验算法为 xor 的协议（程序化构造）。"""
    payload = load_protocol(FIXTURE_YAML).model_dump()
    payload["name"] = "mock_tcp_xor"
    payload["frame"]["checksum"]["algorithm"] = "xor"
    return ProtocolSpec.model_validate(payload)


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


async def test_start_stop_clean() -> None:
    device = MockTCPDevice(load_protocol(FIXTURE_YAML))
    await device.start()
    assert device.port > 0
    await device.stop()
    assert device.connection_count == 0


async def test_ephemeral_port_bind() -> None:
    async with running_device() as device:
        assert 0 < device.port <= 65535
        client = await FrameClient.connect(device.port)
        addr, cmd, data = await client.roundtrip(build_frame(cmd=1, data=b"*IDN?"))
        assert (addr, cmd) == (1, 1)
        assert data == b"MockTCP,MTX-1000,SN0001"
        await client.close()


async def test_port_raises_before_start() -> None:
    device = MockTCPDevice(load_protocol(FIXTURE_YAML))
    with pytest.raises(RuntimeError, match="not started"):
        _ = device.port


async def test_stop_before_start_is_safe() -> None:
    device = MockTCPDevice(load_protocol(FIXTURE_YAML))
    await device.stop()  # 不应抛异常


# ---------------------------------------------------------------------------
# 帧收发
# ---------------------------------------------------------------------------


async def test_framing_roundtrip_happy_path() -> None:
    async with running_device() as device:
        client = await FrameClient.connect(device.port)
        addr, cmd, data = await client.roundtrip(build_frame(addr=1, cmd=1, data=b"*IDN?"))
        assert (addr, cmd) == (1, 1)
        assert data == b"MockTCP,MTX-1000,SN0001"
        await client.close()


async def test_bad_head_rejected_with_error_frame() -> None:
    async with running_device() as device:
        client = await FrameClient.connect(device.port)
        await client.send(b"\x00\x01" + build_frame()[2:])
        _, cmd, data = parse_response(await client.recv())
        assert cmd == ERR_BAD_HEAD
        assert b"head" in data.lower()
        assert await client.recv_eof(), "connection should be closed after bad head"
        await client.close()


async def test_len_mismatch_rejected_as_checksum_error() -> None:
    """LEN 声明 2 但实际发 4 字节 DATA：服务器按声明长度截断后校验失败。"""
    async with running_device() as device:
        client = await FrameClient.connect(device.port)
        body = bytes([1, 3]) + (2).to_bytes(2, "big") + b"WXYZ"
        frame = HEAD + body + bytes([checksum(body, "sum")])
        await client.send(frame)
        _, cmd, _ = parse_response(await client.recv())
        assert cmd == ERR_BAD_CHECKSUM
        await client.close()


async def test_bad_checksum_rejected_then_connection_recovers() -> None:
    async with running_device() as device:
        client = await FrameClient.connect(device.port)
        good = build_frame(cmd=1)
        corrupted = good[:-1] + bytes([good[-1] ^ 0xFF])
        await client.send(corrupted)
        _, cmd, _ = parse_response(await client.recv())
        assert cmd == ERR_BAD_CHECKSUM
        # 同一连接继续服务后续合法帧
        addr, cmd2, data = await client.roundtrip(build_frame(cmd=3))
        assert (addr, cmd2, data) == (1, 3, b"OK")
        await client.close()


async def test_addr_routing_rejects_unserved_address_and_echoes_addr() -> None:
    async with running_device() as device:
        client = await FrameClient.connect(device.port)
        addr, cmd, _ = await client.roundtrip(build_frame(addr=0x7F, cmd=1))
        assert addr == 0x7F, "error frame should echo request addr"
        assert cmd == ERR_BAD_ADDR
        addr, cmd, data = await client.roundtrip(build_frame(addr=1, cmd=1, data=b"*IDN?"))
        assert (addr, cmd, data) == (1, 1, b"MockTCP,MTX-1000,SN0001")
        await client.close()


async def test_command_table_lookup_hit() -> None:
    async with running_device() as device:
        client = await FrameClient.connect(device.port)
        expected_map = (
            (1, b"*IDN?", b"MockTCP,MTX-1000,SN0001"),
            (2, b"", b"25.4 C"),
            (3, b"", b"OK"),
        )
        for cmd_byte, payload, expected in expected_map:
            _, cmd, data = await client.roundtrip(build_frame(cmd=cmd_byte, data=payload))
            assert cmd == cmd_byte
            assert data == expected
        await client.close()


async def test_unknown_command_error_frame() -> None:
    async with running_device() as device:
        client = await FrameClient.connect(device.port)
        addr, cmd, data = await client.roundtrip(build_frame(cmd=0x55))
        assert (addr, cmd) == (1, ERR_UNKNOWN_CMD)
        assert b"command" in data.lower()
        await client.close()


async def test_latency_floor_applied() -> None:
    async with running_device() as device:
        client = await FrameClient.connect(device.port)
        start = time.monotonic()
        await client.roundtrip(build_frame(cmd=2))  # read_temperature latency_ms=80
        elapsed = time.monotonic() - start
        assert elapsed >= 0.06, f"latency floor violated: {elapsed:.4f}s < 60ms"
        assert elapsed < 3.0, f"latency unreasonably long: {elapsed:.4f}s"
        await client.close()


# ---------------------------------------------------------------------------
# 网络故障钩子
# ---------------------------------------------------------------------------


async def test_disconnect_fault_drops_socket() -> None:
    async with running_device() as device:
        client = await FrameClient.connect(device.port)
        assert await wait_connections(device, 1) == 1
        await device.inject_disconnect()
        assert await client.recv_eof(), "disconnect fault should drop the socket"
        await client.close()


async def test_reconnect_after_fault_works() -> None:
    t0 = time.monotonic()

    def mark(label: str) -> None:
        print(f"T={time.monotonic() - t0:.3f}s {label}", flush=True)

    async with running_device() as device:
        first = await FrameClient.connect(device.port)
        assert await wait_connections(device, 1) == 1
        await device.inject_disconnect()
        assert await first.recv_eof()
        await first.close()

        second = await FrameClient.connect(device.port)
        addr, cmd, data = await second.roundtrip(build_frame(cmd=1, data=b"*IDN?"))
        assert (addr, cmd, data) == (1, 1, b"MockTCP,MTX-1000,SN0001")
        await second.close()


# ---------------------------------------------------------------------------
# 协议 YAML 解析（malformed spec → actionable config error）
# ---------------------------------------------------------------------------


def _mutated_fixture(tmp_path: Path, old: str, new: str) -> Path:
    text = FIXTURE_YAML.read_text(encoding="utf-8").replace(old, new)
    path = tmp_path / "protocol.yaml"
    path.write_text(text, encoding="utf-8")
    return path


async def test_malformed_yaml_missing_checksum_field(tmp_path: Path) -> None:
    path = _mutated_fixture(tmp_path, "    - {name: checksum, size: 1}\n", "")
    with pytest.raises(ProtocolConfigError, match="checksum"):
        load_protocol(path)


async def test_malformed_yaml_unknown_checksum_algorithm(tmp_path: Path) -> None:
    path = _mutated_fixture(tmp_path, "algorithm: sum", "algorithm: crc32")
    with pytest.raises(ProtocolConfigError, match="crc32"):
        load_protocol(path)


async def test_malformed_yaml_duplicate_cmd_bytes(tmp_path: Path) -> None:
    path = _mutated_fixture(tmp_path, "cmd: 3", "cmd: 1")
    with pytest.raises(ProtocolConfigError, match="duplicate"):
        load_protocol(path)


async def test_checksum_algorithm_from_yaml_respected() -> None:
    """同一帧字节在 sum 协议下被拒、在 xor 协议下被接受。"""
    sum_frame = build_frame(cmd=3, data=b"OK", algo="sum")

    async with running_device(xor_protocol_spec()) as device:
        client = await FrameClient.connect(device.port)
        await client.send(sum_frame)
        _, cmd, _ = parse_response(await client.recv())
        assert cmd == ERR_BAD_CHECKSUM, "sum-framed request must fail under xor protocol"

        xor_frame = build_frame(cmd=3, data=b"OK", algo="xor")
        await client.send(xor_frame)
        resp = await client.recv()  # xor 响应校验手工解析，绕过 sum 断言
        assert resp[3] == 3 and resp[6:8] == b"OK"
        await client.close()


# ---------------------------------------------------------------------------
# 边界与并发
# ---------------------------------------------------------------------------


async def test_max_len_boundary_enforced() -> None:
    async with running_device() as device:
        client = await FrameClient.connect(device.port)
        # max_data_len=64：恰好 64 字节通过
        _, cmd, data = await client.roundtrip(build_frame(cmd=3, data=b"A" * 64))
        assert (cmd, data) == (3, b"OK")
        # 65 字节声明 → ERR_LEN_TOO_LONG 且连接关闭
        await client.send(build_frame(cmd=3, data=b"B" * 65))
        _, cmd, _ = parse_response(await client.recv())
        assert cmd == ERR_LEN_TOO_LONG
        assert await client.recv_eof(), "oversize LEN must close the desynced connection"
        await client.close()


async def test_concurrent_clients_interleaved() -> None:
    async with running_device() as device:
        c1 = await FrameClient.connect(device.port)
        c2 = await FrameClient.connect(device.port)
        # 交错请求：两个连接各自独立收发
        await c1.send(build_frame(cmd=1, data=b"*IDN?"))
        await c2.send(build_frame(cmd=2))
        _, cmd1, data1 = parse_response(await c1.recv())
        _, cmd2, data2 = parse_response(await c2.recv())
        assert (cmd1, data1) == (1, b"MockTCP,MTX-1000,SN0001")
        assert (cmd2, data2) == (2, b"25.4 C")
        await c1.send(build_frame(cmd=3))
        _, cmd3, data3 = parse_response(await c1.recv())
        assert (cmd3, data3) == (3, b"OK")
        await c1.close()
        await c2.close()


# ---------------------------------------------------------------------------
# 注册钩子（集中注册稍后接入 MockDriverFactory）
# ---------------------------------------------------------------------------


class _FakeFactory:
    def __init__(self) -> None:
        self.registered: dict[str, type] = {}

    def register(self, key: str, driver_cls: type) -> None:
        self.registered[key] = driver_cls


def test_register_hook_uses_tcp_device_key() -> None:
    from ate_platform.drivers.mock.mock_tcp_device import register

    factory = _FakeFactory()
    register(factory)
    assert factory.registered[FACTORY_KEY] is MockTCPDevice
    assert FACTORY_KEY == "tcp_device"

