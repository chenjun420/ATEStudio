"""Mock TCP 设备协议层 — 协议 YAML 解析 + 帧编解码。

帧格式仿 Chroma 风格（设计文档 §6.2.5）::

    [HEAD(n)][ADDR][CMD][LEN(2)][DATA][CHECKSUM(1)]

字段顺序/大小、校验算法（sum|xor）、命令表全部由协议 YAML 声明，
启动时解析校验；非法配置抛 :class:`ProtocolConfigError`。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from functools import reduce
from operator import xor
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# 错误响应 CMD 字节约定
ERR_BAD_HEAD = 0xE1
ERR_BAD_CHECKSUM = 0xE2
ERR_LEN_TOO_LONG = 0xE4
ERR_UNKNOWN_CMD = 0xE5
ERR_BAD_ADDR = 0xE6
ERR_REQUEST_MISMATCH = 0xE7

_ALL_ROLES = ("addr", "cmd", "len", "data", "checksum")
FieldRole = Literal["addr", "cmd", "len", "data", "checksum"]


class ProtocolConfigError(ValueError):
    """协议 YAML 配置非法（消息含文件路径与具体原因）。"""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FieldSpec(_FrozenModel):
    """单个帧字段：角色名 + 固定字节大小（data 角色为动态长度）。"""

    name: FieldRole
    size: int = Field(default=1, ge=0)
    endian: Literal["big", "little"] = "big"


class ChecksumSpec(_FrozenModel):
    """校验算法与覆盖范围（按角色名，依 coverage 顺序拼接计算）。"""

    algorithm: Literal["sum", "xor"] = "sum"
    coverage: list[FieldRole] = Field(default_factory=lambda: ["addr", "cmd", "len", "data"], min_length=1)


class FrameSpec(_FrozenModel):
    """帧布局：HEAD 魔数 + 有序字段表 + 校验声明 + DATA 上限。"""

    head: list[int] = Field(min_length=1, max_length=4)
    fields: list[FieldSpec]
    checksum: ChecksumSpec = ChecksumSpec()
    max_data_len: int = Field(default=256, ge=1, le=65535)

    @field_validator("head")
    @classmethod
    def _head_bytes(cls, value: list[int]) -> list[int]:
        if not all(0 <= b <= 255 for b in value):
            raise ValueError(f"frame.head bytes must be within 0..255, got {value}")
        return value

    @model_validator(mode="after")
    def _validate_layout(self) -> FrameSpec:
        counts = Counter(f.name for f in self.fields)
        missing = [role for role in _ALL_ROLES if counts[role] == 0]
        if missing:
            raise ValueError(f"frame.fields missing required role(s): {', '.join(missing)}")
        duplicated = sorted(role for role, n in counts.items() if n > 1)
        if duplicated:
            raise ValueError(f"frame.fields duplicated role(s): {', '.join(duplicated)}")
        for spec in self.fields:
            if spec.name != "data" and spec.size < 1:
                raise ValueError(f"field {spec.name!r} size must be >= 1 (data is dynamic)")
        positions = {spec.name: i for i, spec in enumerate(self.fields)}
        ck_pos = positions["checksum"]
        late = [role for role in self.checksum.coverage if positions[role] > ck_pos]
        if late:
            raise ValueError(f"checksum coverage role(s) after checksum field: {', '.join(late)}")
        if ck_pos < positions["data"]:
            raise ValueError("frame.fields: checksum role must come after data role")
        premature = [
            spec.name
            for spec in self.fields
            if spec.name not in ("data", "checksum") and positions[spec.name] > positions["data"]
        ]
        if premature:
            raise ValueError(f"frame.fields: role(s) {', '.join(premature)} must come before data role")
        return self


class CommandSpec(_FrozenModel):
    """命令表条目：CMD 字节 → 响应模板（request_data 非 None 时校验请求体）。"""

    cmd: int = Field(ge=0, le=255)
    request_data: str | None = None
    response_data: str = ""
    latency_ms: int | None = Field(default=None, ge=0)


class ProtocolSpec(_FrozenModel):
    """完整协议规格：帧布局 + 服务地址表 + 命令表 + 默认时延。"""

    name: str
    frame: FrameSpec
    commands: dict[str, CommandSpec]
    addresses: list[int] = Field(default_factory=lambda: [1], min_length=1)
    latency_ms: int = Field(default=0, ge=0)

    @field_validator("addresses")
    @classmethod
    def _address_range(cls, value: list[int]) -> list[int]:
        if not all(0 <= a <= 255 for a in value):
            raise ValueError(f"addresses must be within 0..255, got {value}")
        return value

    @model_validator(mode="after")
    def _validate_commands(self) -> ProtocolSpec:
        seen: dict[int, str] = {}
        for cmd_name, spec in self.commands.items():
            if spec.cmd in seen:
                raise ValueError(
                    f"duplicate command byte 0x{spec.cmd:02X} between {seen[spec.cmd]!r} and {cmd_name!r}"
                )
            seen[spec.cmd] = cmd_name
        return self


def load_protocol(path: str | Path) -> ProtocolSpec:
    """解析协议 YAML → :class:`ProtocolSpec`；任何问题抛可读的 ProtocolConfigError。"""
    file = Path(path)
    if not file.is_file():
        raise ProtocolConfigError(f"protocol yaml not found: {file}")
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProtocolConfigError(f"invalid YAML syntax in {file}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProtocolConfigError(f"protocol yaml {file} must be a mapping, got {type(raw).__name__}")
    try:
        return ProtocolSpec.model_validate(raw)
    except ValidationError as exc:
        raise ProtocolConfigError(f"invalid protocol spec in {file}:\n{exc}") from exc


class MalformedFrameError(Exception):
    """请求帧校验失败；携带错误码、是否致命（断连）及丢弃字节数。"""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        addr: int = 0,
        fatal: bool = False,
        discard: int = 0,
        resync: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.addr = addr
        self.fatal = fatal
        self.discard = discard
        self.resync = resync


class DecodedFrame:
    """解码后的请求帧。"""

    __slots__ = ("addr", "cmd", "data")

    def __init__(self, addr: int, cmd: int, data: bytes) -> None:
        self.addr = addr
        self.cmd = cmd
        self.data = data


class FrameCodec:
    """按 :class:`FrameSpec` 编码/流式解码帧（HEAD 校验、LEN 边界、CHECKSUM）。"""

    def __init__(self, spec: FrameSpec) -> None:
        self._spec = spec
        self.head = bytes(spec.head)
        self._fields = {f.name: f for f in spec.fields}
        data_index = next(i for i, f in enumerate(spec.fields) if f.name == "data")
        # 前缀 = HEAD + data 之前的全部固定字段（checksum 在 data 之后单独读）
        self._fixed_prefix = len(self.head) + sum(f.size for f in spec.fields[:data_index])
        self._ck_size = self._fields["checksum"].size

    def compute_checksum(self, parts: dict[str, bytes]) -> int:
        blob = b"".join(parts[role] for role in self._spec.checksum.coverage)
        if self._spec.checksum.algorithm == "sum":
            return sum(blob) & 0xFF
        return reduce(xor, blob, 0)

    def encode(self, *, addr: int, cmd: int, data: bytes) -> bytes:
        parts: dict[str, bytes] = {"data": data}
        for role, value in (("addr", addr), ("cmd", cmd), ("len", len(data))):
            fspec = self._fields[role]
            parts[role] = value.to_bytes(fspec.size, fspec.endian)
        ckspec = self._fields["checksum"]
        parts["checksum"] = self.compute_checksum(parts).to_bytes(ckspec.size, ckspec.endian)
        ordered = [f.name for f in self._spec.fields]
        return self.head + b"".join(parts[name] for name in ordered)

    async def decode_stream(self, readexactly: Callable[[int], Awaitable[bytes]]) -> DecodedFrame:
        """从流中读取一帧；布局/校验失败抛 :class:`MalformedFrameError`。"""
        prefix = await readexactly(self._fixed_prefix)
        if prefix[: len(self.head)] != self.head:
            raise MalformedFrameError(
                ERR_BAD_HEAD,
                f"ERR frame head mismatch: got {prefix[: len(self.head)]!r}",
                fatal=True,
                resync=True,
            )
        parts = {role: self._slice(prefix, role) for role in ("addr", "cmd", "len")}
        declared = int.from_bytes(parts["len"], self._fields["len"].endian)
        if declared > self._spec.max_data_len:
            raise MalformedFrameError(
                ERR_LEN_TOO_LONG,
                f"ERR data length {declared} exceeds limit {self._spec.max_data_len}",
                addr=int.from_bytes(parts["addr"], self._fields["addr"].endian),
                fatal=True,
                discard=declared + self._ck_size,
            )
        data = await readexactly(declared)
        ck_raw = await readexactly(self._ck_size)
        parts["data"] = data
        expected = self.compute_checksum(parts).to_bytes(self._ck_size, self._fields["checksum"].endian)
        if ck_raw != expected:
            raise MalformedFrameError(
                ERR_BAD_CHECKSUM,
                f"ERR checksum mismatch: got {ck_raw!r} want {expected!r}",
                addr=int.from_bytes(parts["addr"], self._fields["addr"].endian),
            )
        return DecodedFrame(
            addr=int.from_bytes(parts["addr"], self._fields["addr"].endian),
            cmd=int.from_bytes(parts["cmd"], self._fields["cmd"].endian),
            data=data,
        )

    def _slice(self, prefix: bytes, role: str) -> bytes:
        offset = len(self.head)
        for fspec in self._spec.fields:
            if fspec.name == "data":
                break  # data 及其后的 checksum 不在固定前缀内
            end = offset + fspec.size
            if fspec.name == role:
                return prefix[offset:end]
            offset = end
        raise KeyError(role)  # pragma: no cover - layout validator guarantees presence


__all__ = [
    "ERR_BAD_ADDR",
    "ERR_BAD_CHECKSUM",
    "ERR_BAD_HEAD",
    "ERR_LEN_TOO_LONG",
    "ERR_REQUEST_MISMATCH",
    "ERR_UNKNOWN_CMD",
    "DecodedFrame",
    "FrameCodec",
    "FrameSpec",
    "MalformedFrameError",
    "ProtocolConfigError",
    "ProtocolSpec",
    "load_protocol",
]
