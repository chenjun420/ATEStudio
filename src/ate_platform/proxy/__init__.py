"""InstrumentProxy — 仪器代理进程（V3.2 架构核心）。

所有仪器操作（真实/Mock、SCPI/TCP/串口）统一经代理进程单入口处理，
从根本上解决多进程执行下 ``threading.Lock`` 无法跨进程互斥的问题（A2），
并附带获得统一调用录制与故障注入点。

组件：
- :class:`~ate_platform.proxy.instrument_proxy.InstrumentProxy` — 代理进程本体
- :class:`~ate_platform.proxy.instrument_client.InstrumentClient` — 执行进程侧 IPC 客户端
- :class:`~ate_platform.proxy.connection_pool.ConnectionPool` — 连接池/会话管理
"""

from ate_platform.proxy.connection_pool import ConnectionPool
from ate_platform.proxy.instrument_client import InstrumentClient
from ate_platform.proxy.instrument_proxy import InstrumentProxy, serve
from ate_platform.proxy.proxy_manager import ProxyManager

__all__ = [
    "ConnectionPool",
    "InstrumentClient",
    "InstrumentProxy",
    "ProxyManager",
    "serve",
]
