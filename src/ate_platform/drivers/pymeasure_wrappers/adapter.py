"""pymeasure 预建驱动适配器（组合模式）。

pymeasure 驱动类（如 ``Keysight34465A``）自带构造器与语义方法，
但它的接口与平台 BaseDriver(HAL) 不一致。本模块以**组合**而非继承
的方式把它们适配到平台统一接口：

- ``PyMeasureAdapter`` 是一个 ``BaseDriver``(HAL) 子类，内部持有
  pymeasure 驱动实例；``write/query/read/connect/disconnect/reset``
  转发到 pymeasure 的对应接口，``__getattr__`` 透传语义方法。
- ``PyMeasureAbstraction`` 是平台 MAL 抽象，``__getattr__`` 转发到
  pymeasure 实例，使脚本侧（含代理进程 method 转发）能直接调用
  如 ``measure_voltage()`` 等语义方法。

组合模式规避了设计文档 §6.2.3 多重继承方案的 MRO 冲突风险，同时与
现有 HAL/MAL 双基类架构及 InstrumentProxy 完全兼容。
"""

from __future__ import annotations

import threading
from typing import Any, ClassVar

import structlog

from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.base_mal import BaseAbstraction

logger = structlog.get_logger(__name__)


class PyMeasureAdapter(BaseDriver):
    """把 pymeasure 驱动实例组合进平台 HAL 接口。

    Attributes:
        pymeasure_class: 被包装的 pymeasure 驱动类。
        address: pymeasure 资源地址（VISA 或 TCP 描述）。
    """

    pymeasure_class: ClassVar[type[Any] | None] = None
    """待包装的 pymeasure 驱动类；子类通过 :meth:`wrap` 绑定。"""

    def __init__(self, resource_manager: object = None, **kwargs: Any) -> None:
        """初始化适配器（不立即创建 pymeasure 实例——连接时才创建）。

        Args:
            resource_manager: 兼容 BaseDriver 签名，pymeasure 路径不使用。
            **kwargs: 透传给 pymeasure 驱动构造器的关键字参数。

        Note:
            不调用 ``super().__init__()``——pymeasure 路径不使用 pyvisa
            ResourceManager，避免无谓的 pyvisa 后端初始化。
        """
        self._resource_manager = None  # pymeasure 路径不使用 pyvisa ResourceManager
        self._instrument = None  # 与 BaseDriver 字段对齐（未使用）
        self._lock = threading.Lock()
        self._pm_kwargs: dict[str, Any] = kwargs
        self._pm: Any | None = None  # pymeasure 实例（connect 时创建）
        self._address = ""

    # ------------------------------------------------------------------
    # 生命周期（转发 pymeasure）
    # ------------------------------------------------------------------
    def connect(self, address: str) -> None:  # noqa: PLW0221
        """创建 pymeasure 驱动实例并连接。

        Args:
            address: 资源地址（如 ``'TCPIP0::192.168.1.5::INSTR'``）。

        Raises:
            RuntimeError: 未绑定 pymeasure_class 或实例化失败。
        """
        if self._pm is not None:
            return  # 已连接
        if self.pymeasure_class is None:
            msg = "PyMeasureAdapter subclass must set pymeasure_class via wrap()"
            raise RuntimeError(msg)
        try:
            self._pm = self.pymeasure_class(address, **self._pm_kwargs)
        except Exception as e:  # noqa: BLE001 — 向上抛为 RuntimeError
            msg = f"Failed to instantiate pymeasure driver {self.pymeasure_class.__name__}: {e}"
            raise RuntimeError(msg) from e
        self._address = address
        logger.info("pymeasure_connected", driver=self.pymeasure_class.__name__, address=address)

    def disconnect(self) -> None:
        """关闭 pymeasure 驱动（调用 ``shutdown()`` 若可用）。"""
        if self._pm is not None:
            shutdown = getattr(self._pm, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown()
                except Exception:  # noqa: BLE001 — 关闭失败不掩盖
                    logger.warning("pymeasure_shutdown_failed", exc_info=True)
            self._pm = None
        self._address = ""

    @property
    def is_connected(self) -> bool:
        """是否已连接。"""
        return self._pm is not None

    # ------------------------------------------------------------------
    # SCPI 底层操作（转发 pymeasure）
    # ------------------------------------------------------------------
    def write(self, command: str) -> None:  # noqa: PLW0221
        """写命令（pymeasure 的 ``write``）。"""
        pm = self._require_connected()
        pm.write(command)

    def query(self, command: str, delay: float | None = None) -> str:  # noqa: PLW0221
        """查询（pymeasure 的 ``ask``）。"""
        pm = self._require_connected()
        if delay is not None:
            import time

            time.sleep(delay)
        return str(pm.ask(command))

    def read(self) -> str:  # noqa: PLW0221
        """读取（pymeasure 的 ``read``）。"""
        pm = self._require_connected()
        return str(pm.read())

    def reset(self) -> None:  # noqa: PLW0221
        """复位（``*RST``）。"""
        pm = self._require_connected()
        pm.write("*RST")

    # ------------------------------------------------------------------
    # 语义方法透传
    # ------------------------------------------------------------------
    def unwrap(self) -> Any:
        """返回底层 pymeasure 实例（供 MAL 抽象 / 脚本直接调用语义方法）。"""
        return self._require_connected()

    def __getattr__(self, name: str) -> Any:
        """透明转发未定义的属性到 pymeasure 实例（如 ``measure_voltage``）。

        Args:
            name: 属性/方法名。

        Returns:
            pymeasure 实例上的对应属性。

        Raises:
            AttributeError: pymeasure 实例上不存在该属性。
        """
        pm = self.__dict__.get("_pm")
        if pm is None or name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(pm, name)
        if callable(attr):
            return attr
        return attr

    def _require_connected(self) -> Any:
        """确保已连接并返回底层 pymeasure 实例。"""
        if self._pm is None:
            msg = "Not connected to any instrument. Call connect() first."
            raise RuntimeError(msg)
        return self._pm

    @classmethod
    def wrap(cls, pymeasure_class: type[Any], **kwargs: Any) -> type[PyMeasureAdapter]:
        """动态生成绑定指定 pymeasure 类的适配 HAL 驱动类。

        Args:
            pymeasure_class: pymeasure 驱动类。
            **kwargs: 传给 pymeasure 驱动构造器的默认关键字参数。

        Returns:
            一个新的 ``PyMeasureAdapter`` 子类。
        """

        # 动态子类化：基类为类方法参数 cls，静态上无法作为类型/基类表达，
        # 只能对 class 语句本身做定点抑制（运行时行为不受影响）。
        class WrappedDriver(cls):  # type: ignore[valid-type, misc]
            """绑定单个 pymeasure 类的适配驱动。"""

            def __init__(self, *args: Any, **overrides: Any) -> None:
                merged = {**kwargs, **overrides}  # 构造参数默认值可被覆盖
                super().__init__(*args, **merged)

        # 类体无法引用外层闭包变量，故定义后显式绑定
        WrappedDriver.pymeasure_class = pymeasure_class
        WrappedDriver.__name__ = f"Platform_{pymeasure_class.__name__}"
        WrappedDriver.__qualname__ = WrappedDriver.__name__
        return WrappedDriver


class PyMeasureAbstraction(BaseAbstraction):
    """pymeasure 驱动的平台 MAL 抽象。

    通过 :attr:`driver`（PyMeasureAdapter HAL）访问底层 pymeasure 实例，
    语义方法（如 ``measure_voltage``）经 ``__getattr__`` 透传到 pymeasure。
    """

    def __init__(self, driver: PyMeasureAdapter) -> None:
        super().__init__(driver)

    # ------------------------------------------------------------------
    # 平台 SCPI 底层操作（委托 HAL，不参与 __getattr__ 透传）
    # ------------------------------------------------------------------
    def write(self, command: str) -> None:
        """写命令。"""
        self._driver.write(command)

    def query(self, command: str, delay: float | None = None) -> str:
        """查询。"""
        return self._driver.query(command, delay)

    def read(self) -> str:
        """读取。"""
        return self._driver.read()

    def reset(self) -> None:
        """复位。"""
        self._driver.reset()

    def __getattr__(self, name: str) -> Any:
        """透传语义方法到 pymeasure 实例。

        Args:
            name: 方法名。

        Returns:
            pymeasure 实例上的方法。

        Raises:
            AttributeError: 实例不存在该属性。
        """
        if name.startswith("_"):
            raise AttributeError(name)
        driver = self.__dict__.get("_driver")
        if driver is None:
            raise AttributeError(name)
        unwrapped = getattr(driver, "unwrap", None)
        if unwrapped is None:
            raise AttributeError(name)
        pm = unwrapped()
        attr = getattr(pm, name)
        if callable(attr):
            return attr
        return attr
