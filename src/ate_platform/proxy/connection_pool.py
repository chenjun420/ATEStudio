"""Instrument connection pool — 在代理进程内管理仪器连接。

避免执行进程频繁建立/断开与仪器的连接：连接创建后按 ``resource_id``
缓存复用，空闲超过 ``max_idle_time`` 的连接被清理。

设计要点：
- 仅在**代理进程**内使用（真实硬件连接不跨进程传递）。
- ``threading.Lock`` 保护字典，代理进程内多线程安全。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class ConnectionPool:
    """仪器连接池，在代理进程内管理。

    Attributes:
        max_idle_time: 连接最长空闲秒数，超过后下一次获取时关闭重建。
    """

    def __init__(self, max_idle_time: float = 300.0) -> None:
        self._max_idle = max_idle_time
        self._connections: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def get(self, resource_id: str, create_func: Callable[[], Any]) -> Any:
        """获取连接；不存在或已过期则通过 ``create_func`` 创建。

        Args:
            resource_id: 资源标识（仪器 ID）。
            create_func: 创建新连接的回调（返回连接对象）。

        Returns:
            连接对象（缓存的或新建的）。
        """
        now = time.time()
        with self._lock:
            cached = self._connections.get(resource_id)
            if cached is not None:
                conn, last_used = cached
                if now - last_used < self._max_idle:
                    self._connections[resource_id] = (conn, now)
                    return conn
                # 过期：先关闭再重建
                self._close_unsafe(resource_id)
            conn = create_func()
            self._connections[resource_id] = (conn, now)
            return conn

    def release(self, resource_id: str) -> None:
        """标记连接为空闲（不关闭，保留复用）。

        Args:
            resource_id: 资源标识。
        """
        with self._lock:
            cached = self._connections.get(resource_id)
            if cached is not None:
                conn, _ = cached
                self._connections[resource_id] = (conn, time.time())

    def cleanup_expired(self) -> int:
        """清理并关闭所有超过空闲阈值的连接。

        Returns:
            被关闭的连接数量。
        """
        now = time.time()
        with self._lock:
            expired = [
                rid
                for rid, (_, last_used) in self._connections.items()
                if now - last_used > self._max_idle
            ]
            for rid in expired:
                self._close_unsafe(rid)
            return len(expired)

    def close_all(self) -> None:
        """关闭并清空全部连接（代理进程退出时调用）。"""
        with self._lock:
            for rid in list(self._connections):
                self._close_unsafe(rid)

    @property
    def size(self) -> int:
        """当前缓存中的连接数。"""
        with self._lock:
            return len(self._connections)

    def _close_unsafe(self, resource_id: str) -> None:
        """关闭连接（调用方须已持有锁）。

        Args:
            resource_id: 资源标识。
        """
        cached = self._connections.pop(resource_id, None)
        if cached is None:
            return
        conn, _ = cached
        close = getattr(conn, "close", None) or getattr(conn, "disconnect", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001 — 关闭失败不阻断清理
                logger.warning(
                    "connection_close_failed",
                    resource_id=resource_id,
                    exc_info=True,
                )
