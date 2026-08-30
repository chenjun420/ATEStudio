"""ProxyManager — 仪器代理进程的管理器。

负责：
- 启动/停止 InstrumentProxy 代理进程（独立 ``multiprocessing.Process``）
- 为执行进程提供 :class:`~ate_platform.proxy.instrument_client.InstrumentClient`
- 提供**内联模式**（无独立进程，代理逻辑在后台线程运行），便于单进程
  测试与轻量部署

典型用法：

    manager = ProxyManager(instrument_config, simulation=True)
    manager.start()
    client = manager.client("DMM_CH1")
    value = client.query("MEAS:VOLT?")
    manager.stop()

上下文管理器 ``with ProxyManager(...) as m`` 自动管理生命周期。
"""

from __future__ import annotations

import multiprocessing
import threading
from typing import Any

import structlog

from ate_platform.proxy.instrument_client import InstrumentClient
from ate_platform.proxy.instrument_proxy import InstrumentProxy, serve

logger = structlog.get_logger(__name__)


class ProxyManager:
    """仪器代理进程管理器。

    Attributes:
        config: 仪器配置 ``{"instruments": {resource_id: {type: ...}}}``。
        simulation: 是否使用 Mock 驱动。
        log_dir: 调用录制目录。
        inline: 为 True 时在后台线程运行代理逻辑（无独立进程）。
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        simulation: bool = False,
        log_dir: str | None = None,
        inline: bool = False,
    ) -> None:
        self.config = config or {"instruments": {}}
        self.simulation = simulation
        self.log_dir = log_dir
        self.inline = inline

        self._request_queue: multiprocessing.Queue[Any] = multiprocessing.Queue()
        self._response_queue: multiprocessing.Queue[Any] = multiprocessing.Queue()
        self._proxy: InstrumentProxy | None = None
        self._process: multiprocessing.Process | None = None
        self._thread: threading.Thread | None = None
        # 停止事件：stop() 后置位，客户端据此快速失败而非挂起超时。
        self._stopped = threading.Event()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动代理进程（或内联线程）。"""
        self._stopped.clear()
        if self.inline:
            self._proxy = InstrumentProxy(
                self._request_queue,
                self._response_queue,
                self.config,
                simulation=self.simulation,
                log_dir=self.log_dir,
            )
            self._thread = threading.Thread(
                target=self._proxy.run_forever,
                name="instrument-proxy-inline",
                daemon=True,
            )
            self._thread.start()
            logger.info("instrument_proxy_inline_started")
        else:
            self._process = multiprocessing.Process(
                target=serve,
                args=(
                    self._request_queue,
                    self._response_queue,
                    self.config,
                    self.simulation,
                    self.log_dir,
                ),
                name="instrument-proxy",
                daemon=True,
            )
            self._process.start()
            logger.info("instrument_proxy_process_started", pid=self._process.pid)

    def stop(self, join_timeout: float = 3.0) -> None:
        """停止代理进程/线程。

        Args:
            join_timeout: 等待进程退出的秒数。
        """
        self._stopped.set()  # 客户端后续调用快速失败
        if self.inline and self._thread is not None:
            self._request_queue.put(None)  # 停止信号
            self._thread.join(timeout=join_timeout)
            self._thread = None
        elif self._process is not None:
            self._request_queue.put(None)  # 停止信号
            self._process.join(timeout=join_timeout)
            if self._process.is_alive():
                self._process.terminate()
            self._process = None
        self._proxy = None

    # ------------------------------------------------------------------
    # 客户端访问
    # ------------------------------------------------------------------
    def client(self, resource_id: str, timeout: float = 30.0) -> InstrumentClient:
        """为指定仪器创建客户端。

        Args:
            resource_id: 仪器资源 ID。
            timeout: 调用超时秒数。

        Returns:
            InstrumentClient 实例（通过 IPC 访问代理进程）。

        Raises:
            RuntimeError: 代理未启动。
        """
        if self._process is None and self._thread is None:
            msg = "ProxyManager not started. Call start() first."
            raise RuntimeError(msg)
        return InstrumentClient(
            self._request_queue,
            self._response_queue,
            resource_id,
            timeout=timeout,
            stopped_event=self._stopped,
        )

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------
    def __enter__(self) -> ProxyManager:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.stop()
