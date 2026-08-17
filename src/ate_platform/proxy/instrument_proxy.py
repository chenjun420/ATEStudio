"""InstrumentProxy — 仪器代理进程（V3.2 架构核心，A2 解决方案）。

集中所有仪器操作的唯一入口：执行子进程不再直接持有仪器驱动，而是
通过 IPC（``multiprocessing.Queue``）把操作请求转发给本代理进程；
代理进程内以 **per-instrument 锁** 串行化同一仪器的并发操作，
并用线程池并发处理不同仪器的请求。

附带能力：
- 连接池/会话管理（:class:`~ate_platform.proxy.connection_pool.ConnectionPool`）
- 调用录制（JSONL，供录制/回放引擎作为数据源）
- 故障注入拦截点（网络/协议级故障统一在代理层注入）

使用（Windows spawn 兼容）：

    from multiprocessing import Process, Queue
    from ate_platform.proxy.instrument_proxy import serve

    req_q, resp_q = Queue(), Queue()
    proc = Process(target=serve, args=(req_q, resp_q, config), daemon=True)
    proc.start()

代理进程可独立启动（``python -m ate_platform.proxy.instrument_proxy``）。
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ate_platform.drivers.mock_factory import MockDriverFactory
from ate_platform.proxy.connection_pool import ConnectionPool

import structlog

logger = structlog.get_logger(__name__)

# 支持的 SCPI 底层驱动操作（与 BaseDriver API 对齐）。
# 这些操作作用于 HAL 驱动；method 转发作用于 MAL 抽象。
_ACTIONS = {"write", "query", "read", "connect", "disconnect", "reset"}


@dataclass
class _DriverPair:
    """单台仪器的双层驱动引用。

    - ``hal``: HAL 驱动（SCPI 底层操作：write/query/read/connect/...）。
    - ``mal``: MAL 抽象（语义方法，如 ``measure_voltage``）；可为 None。
    """

    hal: Any
    mal: Any | None = None


class InstrumentProxy:
    """仪器代理进程：所有仪器操作的唯一入口。

    Attributes:
        config: 仪器配置 ``{"instruments": {resource_id: {...}}}``。
        simulation: 为 True 时通过 MockDriverFactory 创建 Mock 驱动。
        log_dir: 调用录制 JSONL 的写入目录。
    """

    def __init__(
        self,
        request_queue: multiprocessing.Queue,
        response_queue: multiprocessing.Queue,
        config: dict[str, Any],
        simulation: bool = False,
        log_dir: str | Path | None = None,
        worker_threads: int = 8,
    ) -> None:
        self.request_queue = request_queue
        self.response_queue = response_queue
        self.config = config
        self.simulation = simulation
        self.log_dir = Path(log_dir or "data/recordings")
        self.worker_threads = worker_threads

        self._instruments: dict[str, Any] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._pool = ConnectionPool()
        self._running = False
        self._pool_executor: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """初始化仪器与线程池并开始处理请求（阻塞运行）。"""
        self._running = True
        self._pool_executor = ThreadPoolExecutor(
            max_workers=self.worker_threads,
            thread_name_prefix="instrument-proxy",
        )
        self._init_instruments()
        logger.info("instrument_proxy_started", simulation=self.simulation)

    def stop(self) -> None:
        """停止请求处理，关闭线程池与连接。"""
        self._running = False
        if self._pool_executor is not None:
            self._pool_executor.shutdown(wait=False)
        self._pool.close_all()
        logger.info("instrument_proxy_stopped")

    def run_forever(self) -> None:
        """阻塞式主循环：从请求队列取请求，交由线程池处理。"""
        self.start()
        try:
            while self._running:
                try:
                    request = self.request_queue.get(timeout=1.0)
                except multiprocessing.queues.Empty:
                    continue
                if request is None:  # 停止信号
                    break
                if self._pool_executor is not None:
                    self._pool_executor.submit(self._handle_request, request)
                else:
                    logger.error("instrument_proxy_not_running")
        finally:
            self.stop()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _init_instruments(self) -> None:
        """根据配置初始化所有仪器驱动（真实或 Mock），并为每台仪器建锁。"""
        instruments = self.config.get("instruments", {})
        for res_id, inst_config in instruments.items():
            self._locks[res_id] = threading.Lock()
            try:
                self._instruments[res_id] = self._create_driver(res_id, inst_config)
            except Exception:  # noqa: BLE001 — 单仪器失败不阻断代理启动
                logger.error("instrument_init_failed", resource_id=res_id, exc_info=True)

    def _create_driver(self, res_id: str, inst_config: dict[str, Any]) -> _DriverPair:
        """创建单台仪器的双层驱动引用（真实或 Mock）。

        HAL 层提供 SCPI 底层操作（write/query/read/connect/...），
        MAL 层提供语义方法（如 ``measure_voltage``）——两者在代理进程内
        共存，method 转发才能作用于正确的一层。

        Args:
            res_id: 资源标识。
            inst_config: 驱动配置（type/profile/driver/abstraction 等）。

        Returns:
            :class:`_DriverPair`（hal + mal）。

        Raises:
            ValueError: 无法为配置创建驱动。
        """
        if self.simulation:
            # MockDriverFactory.create_mock(abstraction_cls) 一次返回包装了
            # Mock HAL 的 MAL 抽象（MAL.driver 即 HAL），两层齐备。
            _ensure_mock_types_registered()
            driver_type = inst_config.get("type", "").upper()
            abstraction_cls = _MOCK_ABSTRACTION_BY_TYPE.get(driver_type)
            if abstraction_cls is None:
                msg = f"No mock driver registered for resource '{res_id}' (type={driver_type})"
                raise ValueError(msg)
            mal = MockDriverFactory.create_mock(abstraction_cls)
            return _DriverPair(hal=mal.driver, mal=mal)

        # 导入 examples 包触发内置驱动注册（dmm/psu/chroma_eload），幂等
        from ate_platform.drivers import examples as _examples  # noqa: F401
        from ate_platform.drivers.base import DriverRegistry

        driver_name = inst_config.get("driver", res_id)
        hal_cls = DriverRegistry.get_driver(driver_name, layer="hal")
        hal = hal_cls()
        # 配置了 address 时尝试预连接；失败仅告警，保留未连接状态，
        # 脚本侧可稍后通过 connect() 再试（设备可能在测试开始前才上电）。
        address = inst_config.get("address")
        if address is not None and hasattr(hal, "connect"):
            try:
                hal.connect(address)
            except Exception as e:  # noqa: BLE001 — 预连接失败不阻断代理启动
                logger.warning(
                    "instrument_preconnect_failed",
                    resource_id=res_id,
                    error=str(e),
                )
        try:
            mal_cls = DriverRegistry.get_driver(driver_name, layer="mal")
            mal = mal_cls(driver=hal)
        except KeyError:
            mal = None  # 仅注册了 HAL 的旧式驱动：method 转发回退到 hal
        return _DriverPair(hal=hal, mal=mal)

    # ------------------------------------------------------------------
    # 请求处理
    # ------------------------------------------------------------------
    def _handle_request(self, request: dict[str, Any]) -> None:
        """处理单个仪器操作请求（在线程池线程中执行）。"""
        req_id = request.get("req_id")
        resource_id = request.get("resource_id")
        action = request.get("action")
        args = request.get("args", [])
        kwargs = request.get("kwargs", {})

        if req_id is None or resource_id is None or action is None:
            self._response(req_id, {"error": "Malformed request"})
            return

        lock = self._locks.get(resource_id)
        if lock is None:
            self._response(req_id, {"error": f"Unknown instrument: {resource_id}"})
            return

        with lock:  # per-instrument 互斥：同一仪器操作串行化
            try:
                result = self._dispatch(resource_id, action, args, kwargs)
                self._response(req_id, {"result": result})
            except Exception as e:  # noqa: BLE001 — 跨进程边界仅传字符串
                self._response(req_id, {"error": str(e), "error_type": type(e).__name__})

    def _dispatch(
        self,
        resource_id: str,
        action: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        """执行仪器操作并录制调用日志。

        Args:
            resource_id: 资源标识。
            action: 操作类型（write/query/read/connect/disconnect/reset/method）。
            args: 位置参数。
            kwargs: 关键字参数。

        Returns:
            操作结果（可 picklable）。

        Raises:
            ValueError: action 不受支持。
        """
        pair = self._instruments.get(resource_id)
        if pair is None:
            msg = f"Instrument not initialized: {resource_id}"
            raise ValueError(msg)

        start = time.time()
        try:
            if action == "method":
                # 语义方法转发作用于 MAL 抽象；无 MAL 时回退到 HAL。
                method_name = kwargs.pop("method_name")
                target = pair.mal if pair.mal is not None else pair.hal
                method = getattr(target, method_name)
                result = method(*args, **kwargs)
            elif action in _ACTIONS:
                # SCPI 底层操作作用于 HAL 驱动。
                result = getattr(pair.hal, action)(*args, **kwargs)
            else:
                msg = f"Unknown action: {action}"
                raise ValueError(msg)
            self._log_call(resource_id, action, args, kwargs, result, time.time() - start)
            return result
        except Exception as e:
            self._log_call(
                resource_id, action, args, kwargs, None, time.time() - start, error=str(e)
            )
            raise

    # ------------------------------------------------------------------
    # IPC 响应
    # ------------------------------------------------------------------
    def _response(self, req_id: str, data: dict[str, Any]) -> None:
        """向响应队列写入一条响应。"""
        self.response_queue.put({"req_id": req_id, **data})

    # ------------------------------------------------------------------
    # 调用录制（JSONL）
    # ------------------------------------------------------------------
    def _log_call(
        self,
        resource_id: str,
        action: str,
        args: list[Any],
        kwargs: dict[str, Any],
        result: Any,
        elapsed: float,
        error: str | None = None,
    ) -> None:
        """录制一次仪器调用（追加到 JSONL 文件）。"""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            log_file = self.log_dir / f"recording_{stamp}.jsonl"
            entry = {
                "timestamp": time.time(),
                "iso_time": datetime.now(UTC).isoformat(),
                "resource_id": resource_id,
                "action": action,
                "args": _safe_repr(args),
                "kwargs": {k: _safe_repr(v) for k, v in kwargs.items() if k != "method_name"},
                "result": _safe_repr(result),
                "elapsed_ms": round(elapsed * 1000, 2),
                "error": error,
            }
            with log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 — 录制失败不影响仪器操作
            logger.error("instrument_call_log_failed", exc_info=True)


def serve(
    request_queue: multiprocessing.Queue,
    response_queue: multiprocessing.Queue,
    config: dict[str, Any],
    simulation: bool = False,
    log_dir: str | Path | None = None,
) -> None:
    """代理进程入口函数（供 ``multiprocessing.Process(target=serve, ...)`` 使用）。

    Args:
        request_queue: 请求队列（执行进程写入）。
        response_queue: 响应队列（代理进程写入）。
        config: 仪器配置字典。
        simulation: 是否使用 Mock 驱动。
        log_dir: 录制日志目录。
    """
    proxy = InstrumentProxy(
        request_queue,
        response_queue,
        config,
        simulation=simulation,
        log_dir=log_dir,
    )
    proxy.run_forever()


# Mock 抽象按类型名映射（simulation 兜底路径）。
# 值是对应的 MAL 抽象类；Mock HAL 通过 MockDriverFactory 的注册表关联。
_MOCK_ABSTRACTION_BY_TYPE: dict[str, Any] = {}


def _ensure_mock_types_registered() -> None:
    """确保 Mock 抽象类型映射已注册（幂等，避免 import 循环）。"""
    if _MOCK_ABSTRACTION_BY_TYPE:
        return
    # 导入 examples 模块触发 register_mock，使 MockDriverFactory.create_mock
    # 能把 abstraction 关联到对应的 Mock HAL 驱动。
    from ate_platform.drivers.examples.chroma_eload import ChromaEloadAbstraction
    from ate_platform.drivers.examples.dmm import DMMAbstraction
    from ate_platform.drivers.examples.psu import PSUAbstraction

    _MOCK_ABSTRACTION_BY_TYPE.update(
        {
            "DMM": DMMAbstraction,
            "PSU": PSUAbstraction,
            "ELOAD": ChromaEloadAbstraction,
        }
    )


def _safe_repr(value: Any) -> Any:
    """将值转为可 JSON 序列化的安全表示（结果截断至 500 字符）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        text = str(value)[:500]
    except Exception:  # noqa: BLE001
        text = "<unrepr>"
    return text


if __name__ == "__main__":  # pragma: no cover — 独立启动入口
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Start the ATE instrument proxy process")
    parser.add_argument("--config", default="config/instruments.yaml", help="Instrument config file")
    parser.add_argument("--simulation", action="store_true", help="Use mock drivers")
    args = parser.parse_args()

    # 懒加载注册 Mock 类型
    _ensure_mock_types_registered()

    request_queue: multiprocessing.Queue = multiprocessing.Queue()
    response_queue: multiprocessing.Queue = multiprocessing.Queue()

    # 独立启动模式下从配置文件加载仪器配置
    import yaml

    config_data: dict[str, Any] = {"instruments": {}}
    cfg_path = Path(args.config)
    if cfg_path.exists():
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        config_data = loaded

    proxy = InstrumentProxy(request_queue, response_queue, config_data, simulation=args.simulation)
    try:
        proxy.run_forever()
    except KeyboardInterrupt:
        proxy.stop()
