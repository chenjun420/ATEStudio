"""Generic fallback mocks — 未知型号的兜底虚拟仪器（设计文档 §6.2.7）。

MockDriverFactory 的字符串注册表未命中时，不得让配置笔误静默通过，也不得
直接崩溃：工厂回退到本模块的通用 mock 并大声告警（logging.warning 指明未
匹配的键），保证仿真模式对任意 instrument type 都可用。

- :class:`GenericMockSCPI` — SCPI 兜底：应答 ``*IDN?`` 与可配置 responses
  映射（大小写不敏感）；其余命令按序记录到 :attr:`unknown_commands` 并返回
  可配置默认值。接口与 ``_MockBaseDriver`` 鸭子类型兼容（connect/query/
  write/read/is_connected），可直接包进 MAL 抽象类。
- :class:`GenericMockTCP` — TCP 兜底：原始字节透传回显（echo）服务器，对
  任意帧协议通用；生命周期与 :class:`~ate_platform.drivers.mock.mock_tcp_device.
  MockTCPDevice` 一致（port=0 临时端口、幂等 stop、干净关闭连接）。

本模块不依赖 mock_factory（避免循环导入）；仅继承 base_hal.BaseDriver。
"""

from __future__ import annotations

import asyncio
import logging

from ate_platform.drivers.base_hal import BaseDriver

logger = logging.getLogger(__name__)

DEFAULT_SCPI_IDENTITY = "GenericMock,GENERIC-SCPI,0,1.0"
_DEFAULT_RESPONSE = "0"

_READ_CHUNK = 65536
_STOP_TIMEOUT_S = 2.0


class GenericMockSCPI(BaseDriver):
    """接受一切 SCPI 命令的通用 mock — *IDN? + 可配置响应表 + 未知命令记录。

    Attributes:
        unknown_commands: 未被 *IDN?/responses 命中的命令（原样、按序记录，
            查询与写命令都算）——供测试断言"设备收到了什么"。
        written_commands: 全部写命令（大写规范化、按序）。
    """

    def __init__(
        self,
        identity: str = DEFAULT_SCPI_IDENTITY,
        responses: dict[str, str] | None = None,
        default_response: str = _DEFAULT_RESPONSE,
    ) -> None:
        """绕过 BaseDriver.__init__（避免创建真实 ResourceManager）。

        Args:
            identity: ``*IDN?`` 应答串。
            responses: 命令 → 固定应答映射（键大小写不敏感、自动 strip）。
            default_response: 未命中 responses 时的查询应答。
        """
        self._instrument = None
        self._address: str = ""
        self._mock_connected: bool = False
        self._identity = identity
        self._responses: dict[str, str] = {
            k.upper().strip(): v for k, v in (responses or {}).items()
        }
        self._default_response = default_response
        self.unknown_commands: list[str] = []
        self.written_commands: list[str] = []

    def connect(self, address: str) -> None:  # noqa: PLW0221
        """模拟连接——任意地址均接受。"""
        self._address = address
        self._mock_connected = True

    def disconnect(self) -> None:  # noqa: PLW0221
        """模拟断开。"""
        self._mock_connected = False
        self._address = ""

    @property
    def is_connected(self) -> bool:
        """连接状态。"""
        return self._mock_connected

    @property
    def identity(self) -> str:
        """"*IDN?" 应答串。"""
        return self._identity

    @property
    def responses(self) -> dict[str, str]:
        """当前响应映射（大写键）。"""
        return dict(self._responses)

    def _check_connected(self) -> None:
        if not self._mock_connected:
            msg = "Not connected to any instrument. Call connect() first."
            raise RuntimeError(msg)

    def query(self, command: str, delay: float | None = None) -> str:  # noqa: PLW0221
        """查询：*IDN? → 身份；responses 命中 → 配置值；否则记录未知并返回默认值。

        Args:
            command: SCPI 查询命令。
            delay: mock 忽略。

        Returns:
            应答字符串。
        """
        self._check_connected()
        cmd = command.upper().strip()
        if cmd == "*IDN?":
            return self._identity
        if cmd in self._responses:
            return self._responses[cmd]
        self.unknown_commands.append(command)
        return self._default_response

    def write(self, command: str) -> None:  # noqa: PLW0221
        """写命令：记录全部写命令；responses 未命中的同时记入 unknown_commands。

        Args:
            command: SCPI 写命令。
        """
        self._check_connected()
        cmd = command.upper().strip()
        self.written_commands.append(cmd)
        if cmd not in self._responses:
            self.unknown_commands.append(command)

    def read(self) -> str:  # noqa: PLW0221
        """读取：返回默认应答（透传型 mock 无独立读缓冲语义）。"""
        self._check_connected()
        return self._default_response


class GenericMockTCP:
    """通用 TCP 兜底 mock — 原始字节透传回显服务器。

    不解析帧协议：收到什么字节就原样回什么，对任意自定义帧协议通用。
    生命周期与 MockTCPDevice 对齐：``port=0`` 绑定临时端口（CI 友好）、
    :attr:`port` 暴露实际端口、stop 幂等且无残留任务/连接。
    """

    def __init__(self, resource_id: str = "", host: str = "127.0.0.1", port: int = 0) -> None:
        self.resource_id = resource_id
        self._host = host
        self._requested_port = port
        self._server: asyncio.Server | None = None
        self._peers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int:
        """实际绑定端口（start 前访问抛 RuntimeError）。"""
        server = self._server
        if server is None or not server.sockets:
            raise RuntimeError("server not started")
        return int(server.sockets[0].getsockname()[1])

    @property
    def connection_count(self) -> int:
        """当前活跃客户端连接数（stop 后应为 0）。"""
        return len(self._peers)

    async def start(self) -> None:
        """启动监听；port=0 由内核分配临时端口。"""
        self._server = await asyncio.start_server(self._on_connect, self._host, self._requested_port)

    async def stop(self) -> None:
        """关闭所有连接并停止监听（幂等；取消处理器任务，无残留端口）。"""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for writer in list(self._peers):
            writer.close()
        await self._reap_tasks()
        if server is not None:
            await server.wait_closed()

    async def echo_once(self, payload: bytes) -> bytes:
        """便捷入口：起临时服务器 → 发一段字节 → 收回显 → 关停。"""
        await self.start()
        try:
            reader, writer = await asyncio.open_connection(self._host, self.port)
            try:
                writer.write(payload)
                await writer.drain()
                data = await asyncio.wait_for(reader.readexactly(len(payload)), _STOP_TIMEOUT_S)
            finally:
                writer.close()
            return data
        finally:
            await self.stop()

    def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle_client(reader, writer))
        self._tasks.add(task)
        self._peers.add(writer)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await reader.read(_READ_CHUNK)
                if not chunk:
                    break
                writer.write(chunk)
                try:
                    await writer.drain()
                except ConnectionError:
                    break
        except ConnectionError:
            pass
        finally:
            self._peers.discard(writer)
            task = asyncio.current_task()
            if task is not None:
                self._tasks.discard(task)
            writer.close()

    async def _reap_tasks(self) -> None:
        if not self._tasks:
            self._peers.clear()
            return
        try:
            await asyncio.wait_for(asyncio.gather(*self._tasks, return_exceptions=True), _STOP_TIMEOUT_S)
        except TimeoutError:
            pass  # 尽力而为；任务随后随事件循环关闭被丢弃
        self._tasks.clear()
        self._peers.clear()


__all__ = [
    "DEFAULT_SCPI_IDENTITY",
    "GenericMockSCPI",
    "GenericMockTCP",
]
