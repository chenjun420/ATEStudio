"""MockTCPDevice 测试辅助 — 手工帧构造/解析 + asyncio 帧客户端。

帧字节在测试侧独立构造（不依赖被测模块编解码器），保证线路格式断言独立于实现。
"""

from __future__ import annotations

import asyncio
import functools
import time

from ate_platform.drivers.mock.mock_tcp_device import MockTCPDevice

HEAD = b"\xaa\x55"
TIMEOUT = 5.0


def checksum(body: bytes, algo: str) -> int:
    """计算 sum/xor 校验和（测试侧独立实现）。"""
    if algo == "sum":
        return sum(body) & 0xFF
    if algo == "xor":
        return functools.reduce(lambda a, b: a ^ b, body, 0)
    raise ValueError(f"unknown algo {algo}")


def build_frame(
    addr: int = 1,
    cmd: int = 1,
    data: bytes = b"",
    *,
    algo: str = "sum",
    head: bytes = HEAD,
    declared_len: int | None = None,
) -> bytes:
    """按 [HEAD][ADDR][CMD][LEN(2)][DATA][CKSUM] 构造请求帧。"""
    ln = len(data) if declared_len is None else declared_len
    body = bytes([addr, cmd]) + ln.to_bytes(2, "big") + data
    return head + body + bytes([checksum(body, algo)])


def parse_response(frame: bytes) -> tuple[int, int, bytes]:
    """解析响应帧 → (addr, cmd, data)，并校验 sum 校验和。"""
    assert frame[:2] == HEAD, f"bad response head: {frame!r}"
    addr, cmd = frame[2], frame[3]
    ln = int.from_bytes(frame[4:6], "big")
    data = frame[6 : 6 + ln]
    assert len(data) == ln, f"truncated response data: {frame!r}"
    assert frame[6 + ln] == sum(frame[2 : 6 + ln]) & 0xFF, f"resp checksum mismatch: {frame!r}"
    return addr, cmd, data


class FrameClient:
    """极简 asyncio 帧测试客户端。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(cls, port: int) -> FrameClient:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), TIMEOUT)
        return cls(reader, writer)

    async def send(self, frame: bytes) -> None:
        self._writer.write(frame)
        await asyncio.wait_for(self._writer.drain(), TIMEOUT)

    async def recv(self) -> bytes:
        prefix = await asyncio.wait_for(self._reader.readexactly(6), TIMEOUT)
        ln = int.from_bytes(prefix[4:6], "big")
        rest = await asyncio.wait_for(self._reader.readexactly(ln + 1), TIMEOUT)
        return prefix + rest

    async def recv_eof(self) -> bool:
        """连接被对端断开（EOF 或 RST）返回 True。"""
        try:
            data = await asyncio.wait_for(self._reader.readexactly(1), TIMEOUT)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return True
        return data == b""

    async def roundtrip(self, frame: bytes) -> tuple[int, int, bytes]:
        await self.send(frame)
        return parse_response(await self.recv())

    async def close(self) -> None:
        self._writer.close()
        try:
            await asyncio.wait_for(self._writer.wait_closed(), 1.0)
        except (ConnectionError, RuntimeError, TimeoutError):
            pass  # RST 后 proactor 的 wait_closed 可能不返回，尽力关闭即可


async def wait_connections(device: MockTCPDevice, count: int) -> int:
    """轮询等待连接数到位（处理器任务调度与 TCP 握手异步）。"""
    deadline = time.monotonic() + 2.0
    while device.connection_count < count and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return device.connection_count
