"""TCP 基类 — 自定义 TCP 协议设备（不依赖 pymeasure/PyVISA）。

设计文档 §6.2.4 的 ``PlatformTCPInstrument`` 是独立类（``connect()`` 无参、
``send/recv`` 字节接口），与平台 BaseDriver(HAL) 统一接口（``connect(address)``、
``write/query/read``）不一致。本实现**继承 BaseDriver** 保持 HAL 统一接口，
使代理进程 / MAL 抽象可直接操作；帧级协议（编解码、校验）由子类覆盖
``_encode_frame`` / ``_decode_frame`` 定制。

地址格式：``TCP::<host>::<port>``（与 VISA TCPIP 地址风格对齐）。
"""

from __future__ import annotations

import socket
from typing import Any, ClassVar

from ate_platform.drivers.base_hal import BaseDriver


class PlatformTCPInstrument(BaseDriver):
    """自定义 TCP 协议设备 HAL 基类。

    属性:
        host: TCP 主机。
        port: TCP 端口。
        timeout: 连接/读写超时秒数。
    """

    # 写命令后是否需要读取一帧响应（部分帧协议设备对写也有 ACK/status 帧）。
    # 需要时由子类置 True，避免响应帧残留在缓冲区导致后续 query 错位。
    READS_AFTER_WRITE: ClassVar[bool] = False

    def __init__(self, resource_manager: Any = None, **kwargs: Any) -> None:
        """初始化 TCP 设备。

        Args:
            resource_manager: 兼容 BaseDriver 签名（TCP 路径不使用）。
            **kwargs: 透传参数（如 ``timeout``）。
        """
        # 不调用 super().__init__()——TCP 路径不使用 pyvisa ResourceManager。
        import threading

        self._resource_manager: Any = None
        self._instrument: Any = None  # 与 BaseDriver 字段对齐（未使用）
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._host: str = ""
        self._port: int = 0
        self._address = ""
        self.timeout: float = float(kwargs.pop("timeout", 5.0))

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    def connect(self, address: str) -> None:  # noqa: PLW0221
        """连接 TCP 设备。

        Args:
            address: ``TCP::<host>::<port>`` 格式地址。

        Raises:
            ValueError: 地址格式非法。
            OSError: 连接失败。
        """
        with self._lock:
            if self._sock is not None:
                return  # 已连接
            host, port = self._parse_address(address)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect((host, port))
            self._sock = sock
            self._host = host
            self._port = port
            self._address = address

    def disconnect(self) -> None:
        """断开 TCP 连接。"""
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None
            self._host = ""
            self._port = 0
            self._address = ""

    @property
    def is_connected(self) -> bool:
        """是否已连接。"""
        return self._sock is not None

    def _parse_address(self, address: str) -> tuple[str, int]:
        """从 ``TCP::host::port`` 解析主机与端口。

        Raises:
            ValueError: 格式非法或端口非数字。
        """
        parts = address.split("::")
        if len(parts) != 3 or parts[0].upper() != "TCP":
            msg = f"Invalid TCP address '{address}'. Expected 'TCP::<host>::<port>'"
            raise ValueError(msg)
        try:
            port = int(parts[2])
        except ValueError as e:
            msg = f"Invalid TCP port in address '{address}'"
            raise ValueError(msg) from e
        return parts[1], port

    # ------------------------------------------------------------------
    # 平台 SCPI 风格接口（帧编解码由子类定制）
    # ------------------------------------------------------------------
    def write(self, command: str) -> None:  # noqa: PLW0221
        """发送命令帧。

        Args:
            command: 协议层命令（子类决定如何编码）。

        Raises:
            RuntimeError: 未连接。
        """
        with self._lock:
            self._require_connected()
            frame = self._encode_frame(command)
            self._sock.sendall(frame)
            if self.READS_AFTER_WRITE:
                # 消费写响应帧，避免残留在缓冲区导致后续 query 读到错位响应
                self._recv_frame()

    def query(self, command: str, delay: float | None = None) -> str:  # noqa: PLW0221
        """发送命令并读取响应。

        Args:
            command: 协议层命令。
            delay: 发送后等待秒数（部分设备需要处理时间）。

        Returns:
            解码后的响应字符串（子类决定解码格式）。
        """
        with self._lock:
            self._require_connected()
            frame = self._encode_frame(command)
            self._sock.sendall(frame)
            if delay is not None:
                import time

                time.sleep(delay)
            raw = self._recv_frame()
            return self._decode_frame(raw)

    def read(self) -> str:  # noqa: PLW0221
        """读取一帧响应。"""
        with self._lock:
            self._require_connected()
            return self._decode_frame(self._recv_frame())

    def reset(self) -> None:  # noqa: PLW0221
        """复位设备（子类定义复位命令）。"""
        self.write("*RST")

    # ------------------------------------------------------------------
    # 子类覆盖点（帧协议）
    # ------------------------------------------------------------------
    def _encode_frame(self, command: str) -> bytes:
        """把协议层命令编码为发送帧（默认文本行 + ``\\n``）。"""
        return command.encode("ascii") + b"\n"

    def _decode_frame(self, raw: bytes) -> str:
        """把接收帧解码为响应字符串（默认去除换行）。"""
        return raw.decode("ascii").strip()

    def _recv_frame(self) -> bytes:
        """接收一帧（默认读一行，直到换行或超时）。"""
        self._require_connected()
        chunks: list[bytes] = []
        while True:
            chunk = self._sock.recv(1)
            if not chunk:
                msg = "Connection closed by instrument"
                raise ConnectionError(msg)
            chunks.append(chunk)
            if chunk == b"\n":
                break
        return b"".join(chunks)

    def _recv_exact(self, size: int) -> bytes:
        """精确读取 ``size`` 字节（供帧协议子类使用）。

        Args:
            size: 需要的字节数。

        Returns:
            读取到的字节序列。

        Raises:
            ConnectionError: 连接在对端关闭前被截断。
        """
        self._require_connected()
        data = b""
        while len(data) < size:
            chunk = self._sock.recv(size - len(data))
            if not chunk:
                msg = "Connection closed by instrument"
                raise ConnectionError(msg)
            data += chunk
        return data

    def _require_connected(self) -> None:
        """确保已连接。"""
        if self._sock is None:
            msg = "Not connected to any instrument. Call connect() first."
            raise RuntimeError(msg)
