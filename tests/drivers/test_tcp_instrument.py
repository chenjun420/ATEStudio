"""TCP 基类 + Chroma 电子负载驱动测试（F1 自研补充驱动）。

用本地 TCP server 验证：
- PlatformTCPInstrument 的地址解析 / 连接 / 文本行协议
- Chroma 帧编解码（HEAD/ADDR/CMD/LEN/DATA/CHECKSUM）端到端
- Chroma MAL 语义方法 → SCPI 帧 → server 响应
- Mock 驱动 + 代理进程 simulation 集成
"""

from __future__ import annotations

import socket
import threading
from typing import Any

import pytest

from ate_platform.drivers.base import DriverRegistry
from ate_platform.drivers.examples.chroma_eload import (
    ChromaEloadAbstraction,
    ChromaEloadHALDriver,
)
from ate_platform.drivers.tcp_instrument import PlatformTCPInstrument


class _BaseEchoServer:
    """共享：绑定本机端口 + 后台线程 + 收集接收到的字节。"""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(5.0)
        self.port = self._sock.getsockname()[1]
        self.host = "127.0.0.1"
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.received: list[bytes] = []

    def start(self) -> None:
        self._thread.start()

    @staticmethod
    def _recv_exact(conn: socket.socket, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                return data
            data += chunk
        return data

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class TextEchoServer(_BaseEchoServer):
    """原始文本回声：逐字节读到换行，原样回（含换行）。"""

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
            conn.settimeout(5.0)
            with conn:
                line = b""
                while True:
                    chunk = conn.recv(1)
                    if not chunk:
                        break
                    line += chunk
                    if chunk == b"\n":
                        self.received.append(line)
                        conn.sendall(line)
                        line = b""
        except (socket.timeout, OSError):
            return


class ChromaEchoServer(_BaseEchoServer):
    """Chroma 帧回声：模拟真实设备——查询回数值帧，写命令回空 DATA 帧。"""

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
            conn.settimeout(5.0)
            with conn:
                while True:
                    header = self._recv_exact(conn, 6)
                    if not header:
                        break
                    data_len = int.from_bytes(header[4:6], "big")
                    data = self._recv_exact(conn, data_len)
                    checksum = self._recv_exact(conn, 1)
                    frame = header + data + checksum
                    self.received.append(frame)
                    command = data.decode("ascii").strip()
                    # 查询命令回数值；写命令回空 DATA（状态帧）
                    resp_data = (
                        b"1.500000E+00" if command.endswith("?") else b""
                    )
                    resp_body = header[2:4] + len(resp_data).to_bytes(2, "big") + resp_data
                    resp_checksum = (sum(resp_body) & 0xFF).to_bytes(1, "big")
                    conn.sendall(b"\xaa\x55" + resp_body + resp_checksum)
        except (socket.timeout, OSError):
            return


@pytest.fixture
def text_server() -> Any:
    server = TextEchoServer()
    server.start()
    yield server
    server.close()


@pytest.fixture
def chroma_server() -> Any:
    server = ChromaEchoServer()
    server.start()
    yield server
    server.close()


# ---------------------------------------------------------------------------
# TCP 基类
# ---------------------------------------------------------------------------


def test_tcp_address_parsing() -> None:
    dev = PlatformTCPInstrument()
    assert dev._parse_address("TCP::192.168.1.100::5025") == ("192.168.1.100", 5025)
    with pytest.raises(ValueError):
        dev._parse_address("INVALID")
    with pytest.raises(ValueError):
        dev._parse_address("TCP::192.168.1.100::notaport")


def test_tcp_text_protocol_roundtrip(text_server: TextEchoServer) -> None:
    dev = PlatformTCPInstrument(timeout=2.0)
    dev.connect(f"TCP::{text_server.host}::{text_server.port}")
    assert dev.is_connected
    dev.write("*IDN?")
    # 基类默认文本行协议：帧就是 SCPI 文本 + \n；query 交互后服务端必已收到
    assert dev.query("*IDN?") == "*IDN?"
    assert text_server.received, "server should have received a line"
    assert text_server.received[0] == b"*IDN?\n"
    dev.disconnect()
    assert not dev.is_connected
    with pytest.raises(RuntimeError, match="Not connected"):
        dev.query("*IDN?")


def test_tcp_operation_before_connect_raises() -> None:
    dev = PlatformTCPInstrument()
    with pytest.raises(RuntimeError, match="Not connected"):
        dev.write("FOO")


# ---------------------------------------------------------------------------
# Chroma 帧协议
# ---------------------------------------------------------------------------


def test_chroma_frame_encode_decode() -> None:
    dev = ChromaEloadHALDriver()
    frame = dev._encode_frame(":MEAS:CURR?")
    # HEAD
    assert frame[:2] == b"\xaa\x55"
    # ADDR
    assert frame[2] == 0x01
    # CMD = query (0x02)
    assert frame[3] == 0x02
    # LEN = 12
    data_len = int.from_bytes(frame[4:6], "big")
    assert data_len == len(":MEAS:CURR?")
    assert frame[6 : 6 + data_len] == b":MEAS:CURR?"
    # CHECKSUM = sum(ADDR..DATA) & 0xFF
    assert frame[-1] == sum(frame[2:-1]) & 0xFF
    # 回解码
    assert dev._decode_frame(frame) == ":MEAS:CURR?"


def test_chroma_e2e_through_tcp(chroma_server: ChromaEchoServer) -> None:
    dev = ChromaEloadHALDriver(timeout=2.0)
    dev.connect(f"TCP::{chroma_server.host}::{chroma_server.port}")
    resp = dev.query(":MEAS:CURR?")
    assert resp == "1.500000E+00"  # 模拟设备返回数值
    # 帧格式确实是 Chroma 帧（非纯文本）
    assert chroma_server.received[0][:2] == b"\xaa\x55"
    dev.disconnect()


def test_chroma_mal_semantic_methods(chroma_server: ChromaEchoServer) -> None:
    hal = ChromaEloadHALDriver(timeout=2.0)
    hal.connect(f"TCP::{chroma_server.host}::{chroma_server.port}")
    mal = ChromaEloadAbstraction(driver=hal)  # type: ignore[arg-type]
    mal.set_load_current(2.5)
    mal.enable_load(True)
    # 模拟设备对查询返回数值 → 语义方法可直接解析
    assert mal.measure_current() == 1.5
    assert mal.measure_voltage() == 1.5
    # 写成帧后 DATA 应为对应 SCPI 命令
    sent_commands = [f[6 : 6 + int.from_bytes(f[4:6], "big")].decode() for f in chroma_server.received]
    assert ":LOAD:CC 2.5000" in sent_commands
    assert ":LOAD:ON 1" in sent_commands
    assert ":MEAS:CURR?" in sent_commands
    hal.disconnect()


# ---------------------------------------------------------------------------
# 注册表 + Mock + 代理进程集成
# ---------------------------------------------------------------------------


def test_chroma_registered_in_registry() -> None:
    from ate_platform.drivers.examples.chroma_eload import ELOAD_DRIVER_NAME

    assert ELOAD_DRIVER_NAME in DriverRegistry.list_drivers()
    hal = DriverRegistry.get_driver(ELOAD_DRIVER_NAME, layer="hal")
    mal = DriverRegistry.get_driver(ELOAD_DRIVER_NAME, layer="mal")
    assert hal is ChromaEloadHALDriver
    assert mal is ChromaEloadAbstraction


def test_chroma_mock_through_proxy() -> None:
    """模拟 Chroma 负载经代理进程（simulation）端到端调用。"""
    from ate_platform.proxy import ProxyManager

    config = {"instruments": {"ELOAD_1": {"type": "ELOAD"}}}
    manager = ProxyManager(config, simulation=True, log_dir="data/recordings")
    manager.start()
    try:
        client = manager.client("ELOAD_1")
        client.connect("MOCK::ELOAD")
        client.set_load_current(3.0)
        client.enable_load(True)
        current = client.measure_current()
        assert isinstance(current, float)
    finally:
        manager.stop()


def test_chroma_mock_factory() -> None:
    from ate_platform.drivers.mock_factory import MockDriverFactory

    eload = MockDriverFactory.create_mock(ChromaEloadAbstraction)
    eload.connect("MOCK::ELOAD")
    eload.set_load_current(2.0)
    eload.enable_load(True)
    value = eload.measure_current()
    assert value == 2.0  # mock 跟踪状态
