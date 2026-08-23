"""MockTCPDevice 虚拟仪器 — 协议 YAML 驱动的 asyncio TCP 服务器。

设计文档 §6.2/§7：profiles 携带 tcp_devices[port, protocol]；本设备按协议
YAML 的帧格式/命令表应答，支持每命令时延模拟与 disconnect 网络故障钩子。
port=0 时绑定临时端口（CI 友好），实际端口经 :attr:`MockTCPDevice.port` 暴露。
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from ate_platform.drivers.mock.tcp_protocol import (
    ERR_BAD_ADDR,
    ERR_BAD_CHECKSUM,
    ERR_BAD_HEAD,
    ERR_LEN_TOO_LONG,
    ERR_REQUEST_MISMATCH,
    ERR_UNKNOWN_CMD,
    DecodedFrame,
    FrameCodec,
    MalformedFrameError,
    ProtocolConfigError,
    ProtocolSpec,
    load_protocol,
)

FACTORY_KEY = "tcp_device"

_DRAIN_TIMEOUT_S = 0.05
_STOP_TIMEOUT_S = 2.0


class MockRegistryLike(Protocol):
    """集中注册目标的最小接口（MockDriverFactory 稍后接线）。"""

    def register(self, key: str, driver_cls: type) -> None: ...


def register(factory: MockRegistryLike) -> None:
    """把 MockTCPDevice 以 ``'tcp_device'`` 键注册到 factory。"""
    factory.register(FACTORY_KEY, MockTCPDevice)


class MockTCPDevice:
    """虚拟 TCP 仪器：帧协议应答 + 命令表 + 时延/disconnect 故障模拟。"""

    def __init__(self, protocol: ProtocolSpec, host: str = "127.0.0.1", port: int = 0) -> None:
        self._protocol = protocol
        self._codec = FrameCodec(protocol.frame)
        self._commands = {spec.cmd: spec for spec in protocol.commands.values()}
        self._host = host
        self._requested_port = port
        self._server: asyncio.Server | None = None
        self._peers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int:
        """实际绑定的端口（start 前访问抛 RuntimeError）。"""
        server = self._server
        if server is None or not server.sockets:
            raise RuntimeError("server not started")
        return server.sockets[0].getsockname()[1]

    @property
    def connection_count(self) -> int:
        """当前活跃客户端连接数（stop 后应为 0）。"""
        return len(self._peers)

    async def start(self) -> None:
        """启动监听；port=0 由内核分配临时端口。"""
        self._server = await asyncio.start_server(
            self._on_connect, self._host, self._requested_port
        )

    async def stop(self) -> None:
        """关闭所有连接并停止监听（幂等；取消处理器任务，无残留端口）。"""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        self._drop_all()
        await self._reap_tasks()
        if server is not None:
            await server.wait_closed()

    async def inject_disconnect(self) -> None:
        """网络故障钩子：立即中断当前所有客户端连接（abrupt，模拟线缆拔出）。"""
        await asyncio.sleep(0)  # 让已完成握手的 accept 回调先完成注册，避免空注册表竞态
        for writer in list(self._peers):
            writer.transport.abort()
        for task in list(self._tasks):
            task.cancel()
        await self._reap_tasks()

    def _drop_all(self) -> None:
        for writer in list(self._peers):
            writer.close()
        for task in list(self._tasks):
            task.cancel()

    async def _reap_tasks(self) -> None:
        if not self._tasks:
            self._peers.clear()
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True), _STOP_TIMEOUT_S
            )
        except TimeoutError:
            pass  # 尽力而为；任务随后随事件循环关闭被丢弃
        self._tasks.clear()
        self._peers.clear()

    # ------------------------------------------------------------------
    # 连接处理
    # ------------------------------------------------------------------

    def _on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """accept 回调：同步注册连接，杜绝处理器首步前的关闭竞态。"""
        task = asyncio.create_task(self._handle_client(reader, writer))
        self._tasks.add(task)
        self._peers.add(writer)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                try:
                    frame = await self._codec.decode_stream(reader.readexactly)
                except asyncio.IncompleteReadError:
                    break
                except MalformedFrameError as exc:
                    await self._write_response(writer, exc.addr, exc.code, exc.message.encode("ascii"))
                    await self._apply_fault_recovery(reader, exc)
                    if exc.fatal:
                        break
                    continue
                resp_addr, resp_cmd, payload, latency_ms = self._dispatch(frame)
                if latency_ms:
                    await asyncio.sleep(latency_ms / 1000)
                if not await self._write_response(writer, resp_addr, resp_cmd, payload):
                    break
        finally:
            self._peers.discard(writer)
            task = asyncio.current_task()
            if task is not None:
                self._tasks.discard(task)
            writer.close()

    def _dispatch(self, frame: DecodedFrame) -> tuple[int, int, bytes, int]:
        """命令路由 → (响应地址, 响应 CMD, DATA, 时延 ms)。"""
        if frame.addr not in self._protocol.addresses:
            return frame.addr, ERR_BAD_ADDR, b"ERR bad address", 0
        command = self._commands.get(frame.cmd)
        if command is None:
            return frame.addr, ERR_UNKNOWN_CMD, b"ERR unknown command", 0
        if command.request_data is not None and frame.data != command.request_data.encode("ascii"):
            return frame.addr, ERR_REQUEST_MISMATCH, b"ERR request data mismatch", 0
        latency = command.latency_ms if command.latency_ms is not None else self._protocol.latency_ms
        return frame.addr, frame.cmd, command.response_data.encode("ascii"), latency

    async def _write_response(
        self, writer: asyncio.StreamWriter, addr: int, cmd: int, data: bytes
    ) -> bool:
        writer.write(self._codec.encode(addr=addr, cmd=cmd, data=data))
        try:
            await writer.drain()
        except ConnectionError:
            return False
        return True

    async def _apply_fault_recovery(self, reader: asyncio.StreamReader, exc: MalformedFrameError) -> None:
        """致命错误断连前排空在途字节，避免 Windows RST 吞掉错误帧。"""
        if exc.discard:
            try:
                await reader.readexactly(exc.discard)
            except asyncio.IncompleteReadError:
                pass
        elif exc.resync:
            try:
                await asyncio.wait_for(reader.read(65536), _DRAIN_TIMEOUT_S)
            except (TimeoutError, ConnectionError, OSError):
                pass


__all__ = [
    "ERR_BAD_ADDR",
    "ERR_BAD_CHECKSUM",
    "ERR_BAD_HEAD",
    "ERR_LEN_TOO_LONG",
    "ERR_REQUEST_MISMATCH",
    "ERR_UNKNOWN_CMD",
    "FACTORY_KEY",
    "MockRegistryLike",
    "MockTCPDevice",
    "ProtocolConfigError",
    "ProtocolSpec",
    "load_protocol",
    "register",
]
