"""RecordingInterceptor —— 仪器调用 / 步骤生命周期 / 变量变更事件的 JSONL 录制。

设计文档 §7.9 record/replay 基础（AC-7）。本模块只提供录制原语与读取器；
回放校验（ReplayEngine）与执行对比（ExecutionDiff）由后续模块消费本格式。

JSONL 模式（每行一个 JSON 对象，UTF-8）::

    头部行（首行）:
      {"kind": "recording_header", "version": 1, "execution_id": str,
       "started_at": ISO8601, "pid": int}

    事件行（公共字段: seq 严格递增 int；t 相对会话起点的单调秒数 float；
             execution_id 会话键）:
      {"kind": "instrument_call", ..., "resource": str, "method": str,
       "args": list, "kwargs": dict, "result": Any|None,
       "error": str|None, "elapsed_ms": float|None}
      {"kind": "step_started"|"step_completed"|"step_failed", ...,
       "step_id": str, "error": str|None}
      {"kind": "variable_change", ..., "scope": str, "key": str, "value": Any}

写入语义：
- 事件先进内存缓冲，每累计 ``flush_every`` 条追加写入 ``<最终名>.tmp``
  并 flush 到磁盘——缓冲永不无界增长（沿用 InstrumentProxy 的
  flush-every-100 先例）。
- ``finalize()`` 将 tmp 原子重命名（``os.replace``）为最终文件。
- 进程中途被杀时 tmp 留下可解析的 JSONL 前缀；末尾撕裂行由容错读取器
  （:func:`RecordingInterceptor.load`）跳过。

安全约定：绝不录制明文密钥——kwargs/变量值中命中敏感键名
（password/token/secret/credential/api_key/auth 等）的值一律替换为
``"[REDACTED]"``（递归处理）。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["RecordingInterceptor"]

#: 敏感键名（小写匹配，含子串命中如 "client_secret"）。
_SENSITIVE_KEY_PARTS = ("password", "passwd", "secret", "token", "credential", "api_key", "apikey", "auth")

_REDACTED = "[REDACTED]"

_HEADER_KIND = "recording_header"
_SCHEMA_VERSION = 1

#: EventBus 事件类型值 → 录制事件种类（其余类型忽略）。
_EVENT_TYPE_TO_KIND = {
    "STEP_STARTED": "step_started",
    "STEP_COMPLETED": "step_completed",
    "STEP_FAILED": "step_failed",
    "instrument_call": "instrument_call",
    "variable_change": "variable_change",
}


def _redact(value: Any) -> Any:
    """递归脱敏：dict 键名命中敏感词时替换其值。"""
    if isinstance(value, dict):
        return {
            k: _REDACTED
            if any(part in str(k).lower() for part in _SENSITIVE_KEY_PARTS)
            else _redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class RecordingInterceptor:
    """把执行期事件捕获为带相对时间戳的 JSONL 录制会话。

    Attributes:
        path: 最终 JSONL 文件路径（写入期间为 ``<path>.tmp``）。
        execution_id: 录制会话键（每条事件都携带）。
        flush_every: 触发落盘的缓冲事件数阈值。
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        execution_id: str = "unknown",
        flush_every: int = 100,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """初始化拦截器。

        Args:
            path: 最终 JSONL 文件路径；父目录不存在则自动创建。
                生产环境可指向如 ``/var/log/test_platform/recordings/``，
                测试中通常传 pytest 的 ``tmp_path``。
            execution_id: 录制会话键。
            flush_every: 缓冲刷盘阈值（防无界缓冲）。
            clock: 单调时钟注入点（默认 :func:`time.monotonic`）。
        """
        self.path = Path(path)
        self.execution_id = execution_id
        self.flush_every = max(1, int(flush_every))
        self._clock = clock

        self._tmp_path = self.path.with_name(self.path.name + ".tmp")
        self._buffer: list[str] = []
        self._seq = 0
        self._t0: float | None = None
        self._last_t = 0.0
        self._fh: Any = None
        self._finalized = False

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """开始录制会话：写头部行并打开 tmp 追加句柄（幂等）。

        单调时钟起点 ``_t0`` 延迟到首条事件才采样——首个事件恒为
        ``t == 0.0``，且显式 ``start()`` 不消耗注入时钟的刻度。
        """
        if self._fh is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "kind": _HEADER_KIND,
            "version": _SCHEMA_VERSION,
            "execution_id": self.execution_id,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "pid": os.getpid(),
        }
        self._fh = self._tmp_path.open("a", encoding="utf-8")
        self._buffer.append(json.dumps(header, ensure_ascii=False))
        self._write_buffer()

    def finalize(self) -> Path:
        """结束会话：刷余量、关句柄、原子重命名 tmp → 最终文件。

        Returns:
            最终 JSONL 文件路径。
        """
        if self._finalized:
            return self.path
        self.start()  # 从未记录过事件也要产出合法头部文件
        self._write_buffer()
        fh, self._fh = self._fh, None
        assert fh is not None
        fh.flush()
        fh.close()
        os.replace(self._tmp_path, self.path)
        self._finalized = True
        return self.path

    # ------------------------------------------------------------------
    # 事件录入 API
    # ------------------------------------------------------------------
    def record(self, kind: str, **payload: Any) -> dict[str, Any]:
        """记录一条任意种类的事件（自动补 seq/t/execution_id 并脱敏）。"""
        self.start()
        now = self._clock()
        if self._t0 is None:
            self._t0 = now  # 首条事件即会话原点：t == 0.0
        raw_t = now - self._t0
        # 全局单调保证：同 tick 或时钟回拨时不后退（见 learnings T13）。
        t = max(raw_t, self._last_t)
        self._last_t = t
        seq = self._seq
        self._seq += 1
        event: dict[str, Any] = {
            "kind": kind,
            "seq": seq,
            "t": float(t),
            "execution_id": self.execution_id,
            **_redact(payload),
        }
        self._buffer.append(json.dumps(event, ensure_ascii=False))
        if len(self._buffer) >= self.flush_every:
            self._write_buffer()
        return event

    def record_instrument_call(
        self,
        resource: str,
        method: str,
        args: list[Any] | tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        *,
        result: Any = None,
        error: str | None = None,
        elapsed_ms: float | None = None,
    ) -> dict[str, Any]:
        """记录一次仪器调用（成功记 result，失败记 error）。"""
        return self.record(
            "instrument_call",
            resource=resource,
            method=method,
            args=list(args),
            kwargs=dict(kwargs or {}),
            result=result,
            error=error,
            elapsed_ms=elapsed_ms,
        )

    def record_step_started(self, step_id: str) -> dict[str, Any]:
        """记录步骤开始。"""
        return self.record("step_started", step_id=step_id)

    def record_step_completed(self, step_id: str) -> dict[str, Any]:
        """记录步骤完成。"""
        return self.record("step_completed", step_id=step_id)

    def record_step_failed(self, step_id: str, *, error: str | None = None) -> dict[str, Any]:
        """记录步骤失败。"""
        return self.record("step_failed", step_id=step_id, error=error)

    def record_variable_change(self, scope: str, key: str, value: Any) -> dict[str, Any]:
        """记录变量变更（scope/key/value，值递归脱敏）。"""
        return self.record("variable_change", scope=scope, key=key, value=value)

    # ------------------------------------------------------------------
    # EventBus 集成
    # ------------------------------------------------------------------
    def subscribe(self, bus: Any) -> None:
        """订阅事件总线（通配），仅映射已知事件类型，其余忽略。

        兼容 :class:`ate_platform.scheduler.event_bus.EventBus` 的
        ``subscribe(event_type, callback)`` 约定；回调同步执行，
        不要求总线处于运行状态。
        """
        bus.subscribe(None, self._on_bus_event)

    def _on_bus_event(self, event: Any) -> None:
        type_value = getattr(getattr(event, "type", None), "value", getattr(event, "type", None))
        kind = _EVENT_TYPE_TO_KIND.get(str(type_value))
        if kind is None:
            return
        data = dict(getattr(event, "data", {}) or {})
        self.record(kind, **data)

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------
    def _write_buffer(self) -> None:
        """把缓冲中的行写入 tmp 句柄并 flush（缓冲清空，防无界增长）。"""
        if not self._buffer:
            return
        assert self._fh is not None, "recording session not started"
        self._fh.write("\n".join(self._buffer) + "\n")
        self._fh.flush()
        self._buffer.clear()

    # ------------------------------------------------------------------
    # 读取器（T11 ReplayEngine / T12 ExecutionDiff 消费入口）
    # ------------------------------------------------------------------
    @staticmethod
    def load(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
        """读取 JSONL 录制，返回事件列表（不含头部行）。

        容错语义：崩溃残留的部分文件可读——完整行正常解析，
        末尾撕裂行 / 空行静默跳过。

        Args:
            path: JSONL 文件路径（最终文件或未 finalize 的 tmp 均可）。

        Returns:
            事件字典列表，按文件顺序（即 seq 升序）。
        """
        events: list[dict[str, Any]] = []
        with Path(path).open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 撕裂尾行：崩溃安全前缀之外的部分
                if rec.get("kind") == _HEADER_KIND:
                    continue
                events.append(rec)
        return events

    @staticmethod
    def read_header(path: str | os.PathLike[str]) -> dict[str, Any]:
        """返回录制头部行（首个 ``recording_header`` 记录）；缺失返回空字典。"""
        with Path(path).open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") == _HEADER_KIND:
                    return rec
                break  # 头部只可能在首行
        return {}
