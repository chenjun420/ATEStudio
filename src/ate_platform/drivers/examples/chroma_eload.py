"""Chroma 63000 系列电子负载驱动（HAL + MAL + Mock）。

基于 TCP 自定义帧协议（设计文档 §6.2.5）：

    帧格式: [HEAD(2)][ADDR(1)][CMD(1)][LEN(2)][DATA][CHECKSUM(1)]
    HEAD    = 0xAA 0x55
    ADDR    = 0x01（默认设备地址）
    CMD     = 0x01（写命令）/ 0x02（查询）
    LEN     = DATA 长度（大端）
    DATA    = ASCII SCPI 命令或响应体
    CHECKSUM= ADDR..DATA 逐字节累加和 & 0xFF

> 注：校验与帧头按公开协议实现；产线对接时以设备手册实际帧为准
> （调整 ``_encode_frame`` / ``_decode_frame`` 即可，不影响上层接口）。
"""

from __future__ import annotations

import random
from typing import ClassVar

from ate_platform.drivers import DriverRegistry
from ate_platform.drivers.base_mal import BaseAbstraction
from ate_platform.drivers.capabilities import ELoadCapabilities
from ate_platform.drivers.mock_factory import MockDriverFactory, _MockBaseDriver
from ate_platform.drivers.tcp_instrument import PlatformTCPInstrument

ELOAD_DRIVER_NAME = "chroma_eload"

# Chroma 帧常量
_FRAME_HEAD = b"\xaa\x55"
_FRAME_ADDR = 0x01
_CMD_WRITE = 0x01
_CMD_QUERY = 0x02


# ---------------------------------------------------------------------------
# HAL Layer — 帧协议编解码 + SCPI 收发
# ---------------------------------------------------------------------------


class ChromaEloadHALDriver(PlatformTCPInstrument):
    """Chroma 电子负载 HAL 驱动——自定义 TCP 帧协议层。

    实现设计文档 §6.2.5 帧格式；DATA 段承载 ASCII SCPI 命令。
    仅负责通信，无语义方法（语义方法在 MAL 层）。
    """

    # Chroma 设备对写命令也有响应帧，需在 write() 时消费避免错位
    READS_AFTER_WRITE = True

    def _encode_frame(self, command: str) -> bytes:
        """把 SCPI 命令封装为 Chroma 请求帧。"""
        data = command.encode("ascii")
        is_query = command.rstrip().endswith("?")
        cmd_byte = _CMD_QUERY if is_query else _CMD_WRITE
        body = bytes([_FRAME_ADDR, cmd_byte]) + len(data).to_bytes(2, "big") + data
        checksum = (sum(body) & 0xFF).to_bytes(1, "big")
        return _FRAME_HEAD + body + checksum

    def _decode_frame(self, raw: bytes) -> str:
        """解析 Chroma 响应帧，返回 DATA 段 ASCII 文本。"""
        # 兼容纯文本回包（部分设备支持 SCPI 直通）；帧格式则严格解析
        if len(raw) < 6 or raw[:2] != _FRAME_HEAD:
            return raw.decode("ascii", errors="replace").strip()
        data_len = int.from_bytes(raw[4:6], "big")
        data = raw[6 : 6 + data_len]
        return data.decode("ascii").strip()

    def _recv_frame(self) -> bytes:
        """按 Chroma 帧格式精确读取一帧（不能依赖换行分隔）。"""
        header = self._recv_exact(6)
        data_len = int.from_bytes(header[4:6], "big")
        data = self._recv_exact(data_len)
        checksum = self._recv_exact(1)
        return header + data + checksum


# ---------------------------------------------------------------------------
# MAL Layer — 语义方法
# ---------------------------------------------------------------------------


class ChromaEloadAbstraction(BaseAbstraction):
    """Chroma 电子负载 MAL 抽象——语义控制方法。

    把负载控制/测量操作翻译为 SCPI 命令经 HAL 帧协议下发。
    """

    capabilities: ClassVar = ELoadCapabilities

    def set_load_current(self, amps: float) -> None:
        """设置恒流（CC）负载电流。

        Args:
            amps: 目标电流（A）。
        """
        self._driver.write(f":LOAD:CC {amps:.4f}")

    def enable_load(self, enable: bool = True) -> None:
        """启用/禁用负载输入。

        Args:
            enable: True 加载，False 卸载。
        """
        self._driver.write(f":LOAD:ON {1 if enable else 0}")

    def measure_current(self) -> float:
        """测量当前负载电流（A）。"""
        return float(self._driver.query(":MEAS:CURR?").strip())

    def measure_voltage(self) -> float:
        """测量输入电压（V）。"""
        return float(self._driver.query(":MEAS:VOLT?").strip())


# ---------------------------------------------------------------------------
# Mock 驱动 — simulation 模式
# ---------------------------------------------------------------------------


class _MockEloadDriver(_MockBaseDriver):
    """Mock 电子负载——SCPI 响应生成（含负载状态跟踪）。"""

    def __init__(self, mock_values: dict[str, str] | None = None) -> None:
        super().__init__(mock_values=mock_values)
        self._load_on = False
        self._load_current = 0.0

    def write(self, command: str) -> None:  # noqa: PLW0221
        """处理 SCPI 写命令并更新内部状态。"""
        self._check_connected()
        parts = command.upper().strip().split()
        if not parts:
            return
        if parts[0] == ":LOAD:ON" and len(parts) >= 2:
            self._load_on = parts[1] == "1"
        elif parts[0] == ":LOAD:CC" and len(parts) >= 2:
            self._load_current = float(parts[1])

    def _generate_response(self, command: str) -> str:
        command_upper = command.upper().strip()
        if "CURR" in command_upper:
            value = self._load_current if self._load_on else 0.0
        elif "VOLT" in command_upper:
            value = 12.0 + random.uniform(-0.1, 0.1) if self._load_on else 0.0
        else:
            value = random.uniform(1.0, 10.0)
        return f"{value:.6E}"


# ---------------------------------------------------------------------------
# 注册（模块导入时执行）
# ---------------------------------------------------------------------------

DriverRegistry.register(
    ELOAD_DRIVER_NAME,
    hal_cls=ChromaEloadHALDriver,
    mal_cls=ChromaEloadAbstraction,
)
MockDriverFactory.register_mock(ChromaEloadAbstraction, _MockEloadDriver)
