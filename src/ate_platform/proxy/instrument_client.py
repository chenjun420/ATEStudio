"""InstrumentClient — 执行进程侧仪器客户端。

通过 IPC（``multiprocessing.Queue``）把仪器操作转发给
:class:`~ate_platform.proxy.instrument_proxy.InstrumentProxy` 代理进程，
接口与 :class:`~ate_platform.drivers.base_hal.BaseDriver` 对齐
（``write`` / ``query`` / ``read`` / ``connect`` / ``disconnect`` / ``reset``），
并通过 ``__getattr__`` 透明转发语义方法（如 ``measure_voltage()``）。

响应匹配：共享响应队列按 ``req_id`` 轮询匹配，不匹配的响应放回队列
（多客户端共享同一响应队列时的简单分发策略）。
"""

from __future__ import annotations

import multiprocessing
import threading
import time
import uuid
from queue import Empty
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class InstrumentClient:
    """执行进程使用的仪器客户端，所有操作通过 IPC 转发到代理进程。

    Attributes:
        request_queue: 代理进程的请求队列。
        response_queue: 代理进程的响应队列。
        resource_id: 本客户端对应的仪器资源 ID。
        timeout: 单次调用的超时秒数。
    """

    def __init__(
        self,
        request_queue: multiprocessing.Queue[Any],
        response_queue: multiprocessing.Queue[Any],
        resource_id: str,
        timeout: float = 30.0,
        stopped_event: threading.Event | None = None,
    ) -> None:
        self.request_queue = request_queue
        self.response_queue = response_queue
        self.resource_id = resource_id
        self.timeout = timeout
        # 代理停止事件：置位后 _call 立即失败，避免在已停止的代理上挂起超时。
        self.stopped_event = stopped_event

    # ------------------------------------------------------------------
    # 底层调用
    # ------------------------------------------------------------------
    def _call(self, action: str, *args: Any, **kwargs: Any) -> Any:
        """发送一个操作请求并等待响应。

        Args:
            action: 操作类型（write/query/read/connect/disconnect/reset/method）。
            *args: 位置参数。
            **kwargs: 关键字参数。

        Returns:
            操作结果。

        Raises:
            RuntimeError: 代理进程返回错误或代理已停止。
            TimeoutError: 超时未收到响应。
        """
        if self.stopped_event is not None and self.stopped_event.is_set():
            msg = (
                f"Instrument proxy stopped [{self.resource_id}]: "
                f"cannot execute '{action}'"
            )
            raise RuntimeError(msg)

        req_id = str(uuid.uuid4())
        self.request_queue.put(
            {
                "req_id": req_id,
                "resource_id": self.resource_id,
                "action": action,
                "args": args,
                "kwargs": kwargs,
            }
        )

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                resp = self.response_queue.get(timeout=min(0.5, self.timeout))
            except (Empty, EOFError):
                continue
            if resp.get("req_id") != req_id:
                # 非本请求的响应：放回队列供其他客户端匹配
                self.response_queue.put(resp)
                continue
            if "error" in resp:
                error_type = resp.get("error_type", "RuntimeError")
                msg = f"Instrument error [{self.resource_id}] ({error_type}): {resp['error']}"
                raise RuntimeError(msg)
            return resp.get("result")

        raise TimeoutError(
            f"Instrument call timeout [{self.resource_id}]: {action} "
            f"(>{self.timeout}s)"
        )

    # ------------------------------------------------------------------
    # BaseDriver 对齐接口
    # ------------------------------------------------------------------
    def connect(self, address: str) -> Any:
        """连接仪器（转发给代理进程）。

        Args:
            address: VISA 资源地址。

        Returns:
            代理进程返回的结果。
        """
        return self._call("connect", address)

    def disconnect(self) -> Any:
        """断开仪器连接。"""
        return self._call("disconnect")

    def write(self, command: str) -> Any:
        """向仪器写入命令。

        Args:
            command: SCPI 命令字符串。
        """
        return self._call("write", command)

    def query(self, command: str, delay: float | None = None) -> Any:
        """向仪器写入并读取响应。

        Args:
            command: SCPI 命令字符串。
            delay: 可选的读取前延时。
        """
        if delay is not None:
            return self._call("query", command, delay)
        return self._call("query", command)

    def read(self) -> Any:
        """从仪器读取响应。"""
        return self._call("read")

    def reset(self) -> Any:
        """复位仪器（*RST）。"""
        return self._call("reset")

    def call_method(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """调用驱动的语义方法（如 ``measure_voltage``）。

        Args:
            method_name: 方法名。
            *args: 位置参数。
            **kwargs: 关键字参数。
        """
        return self._call("method", method_name=method_name, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """透明转发任意方法调用（如 ``client.measure_voltage()``）。

        Args:
            name: 方法名。

        Returns:
            转发调用的可调用对象。

        Raises:
            AttributeError: 私有属性不转发。
        """
        if name.startswith("_"):
            raise AttributeError(name)

        def _method(*args: Any, **kwargs: Any) -> Any:
            return self.call_method(name, *args, **kwargs)

        return _method

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------
    def __enter__(self) -> "InstrumentClient":
        """上下文入口（与 BaseDriver 一致）。"""
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """上下文出口：断开连接。"""
        try:
            self.disconnect()
        except Exception:  # noqa: BLE001 — 断开失败不掩盖原异常
            logger.warning("client_disconnect_failed", resource_id=self.resource_id)
