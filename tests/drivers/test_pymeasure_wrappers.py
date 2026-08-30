"""pymeasure 适配层测试（F1）。

使用 stub pymeasure 驱动类模拟 Keysight 34465A 等预建驱动的接口
（write/ask/read/shutdown + 语义方法），验证组合式适配逻辑——真实
pymeasure / 硬件不可用时同样覆盖转发、降级与 MAL 透传路径。
"""

from __future__ import annotations

import pytest

from ate_platform.drivers.base import DriverRegistry
from ate_platform.drivers.pymeasure_wrappers.adapter import (
    PyMeasureAbstraction,
    PyMeasureAdapter,
)
from ate_platform.drivers.pymeasure_wrappers.register import (
    _load_pymeasure_class,
    ensure_pymeasure_drivers_registered,
)


class StubKeysightDMM:
    """模拟 pymeasure Keysight34465A 的最小接口。"""

    def __init__(self, address: str, **kwargs: Any) -> None:
        self.address = address
        self._log: list[str] = []
        self.shutdown_called = False

    def write(self, command: str) -> None:
        self._log.append(f"write:{command}")

    def ask(self, command: str) -> str:
        self._log.append(f"ask:{command}")
        return "5.000000E+00"

    def read(self) -> str:
        self._log.append("read")
        return "1.000000E+03"

    def shutdown(self) -> None:
        self.shutdown_called = True

    # 语义方法（pymeasure 预建驱动自带）
    def measure_voltage(self) -> float:
        return 5.0


from typing import Any  # noqa: E402

# ---------------------------------------------------------------------------
# 适配 HAL 驱动
# ---------------------------------------------------------------------------


def test_wrap_creates_bound_subclass() -> None:
    wrapped = PyMeasureAdapter.wrap(StubKeysightDMM)
    assert issubclass(wrapped, PyMeasureAdapter)
    assert wrapped.pymeasure_class is StubKeysightDMM
    assert wrapped.__name__ == "Platform_StubKeysightDMM"


def test_connect_instantiates_pymeasure_driver() -> None:
    wrapped = PyMeasureAdapter.wrap(StubKeysightDMM)
    driver = wrapped()
    assert not driver.is_connected
    driver.connect("TCPIP0::192.168.1.5::INSTR")
    assert driver.is_connected
    assert driver.address == "TCPIP0::192.168.1.5::INSTR"
    assert driver.unwrap().address == "TCPIP0::192.168.1.5::INSTR"


def test_query_write_forwarded() -> None:
    wrapped = PyMeasureAdapter.wrap(StubKeysightDMM)
    driver = wrapped()
    driver.connect("MOCK::DMM")
    assert driver.query("MEAS:VOLT:DC?") == "5.000000E+00"
    driver.write("CONF:VOLT:DC 10")
    log = driver.unwrap()._log
    assert "ask:MEAS:VOLT:DC?" in log
    assert "write:CONF:VOLT:DC 10" in log


def test_read_and_reset_forwarded() -> None:
    wrapped = PyMeasureAdapter.wrap(StubKeysightDMM)
    driver = wrapped()
    driver.connect("MOCK::DMM")
    assert driver.read() == "1.000000E+03"
    driver.reset()
    assert "write:*RST" in driver.unwrap()._log


def test_semantic_method_via_getattr() -> None:
    wrapped = PyMeasureAdapter.wrap(StubKeysightDMM)
    driver = wrapped()
    driver.connect("MOCK::DMM")
    assert driver.measure_voltage() == 5.0


def test_disconnect_calls_shutdown() -> None:
    wrapped = PyMeasureAdapter.wrap(StubKeysightDMM)
    driver = wrapped()
    driver.connect("MOCK::DMM")
    pm = driver.unwrap()
    driver.disconnect()
    assert pm.shutdown_called  # shutdown() 被调用
    assert not driver.is_connected
    # 断开后调用应报错（含 unwrap）
    with pytest.raises(RuntimeError, match="Not connected"):
        driver.query("MEAS:VOLT:DC?")
    with pytest.raises(RuntimeError, match="Not connected"):
        driver.unwrap()


def test_operation_before_connect_raises() -> None:
    wrapped = PyMeasureAdapter.wrap(StubKeysightDMM)
    driver = wrapped()
    with pytest.raises(RuntimeError, match="Not connected"):
        driver.query("MEAS:VOLT:DC?")


def test_wrap_without_class_raises_on_connect() -> None:
    driver = PyMeasureAdapter()
    with pytest.raises(RuntimeError, match="pymeasure_class"):
        driver.connect("MOCK::DMM")


def test_connect_failure_wraps_as_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        def __init__(self, address: str) -> None:
            raise OSError("no device")

    wrapped = PyMeasureAdapter.wrap(Boom)
    driver = wrapped()
    with pytest.raises(RuntimeError, match="Failed to instantiate"):
        driver.connect("MOCK::BOOM")


# ---------------------------------------------------------------------------
# MAL 抽象
# ---------------------------------------------------------------------------


def test_mal_abstraction_semantic_forwarding() -> None:
    wrapped = PyMeasureAdapter.wrap(StubKeysightDMM)
    hal = wrapped()
    hal.connect("MOCK::DMM")
    mal = PyMeasureAbstraction(driver=hal)  # type: ignore[arg-type]
    assert mal.measure_voltage() == 5.0
    # MAL 的 SCPI 底层操作也走 HAL
    assert mal.query("MEAS:VOLT:DC?") == "5.000000E+00"  # type: ignore[attr-defined]


def test_mal_unknown_attribute_raises() -> None:
    wrapped = PyMeasureAdapter.wrap(StubKeysightDMM)
    hal = wrapped()
    hal.connect("MOCK::DMM")
    mal = PyMeasureAbstraction(driver=hal)  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        mal.no_such_semantic_method()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


def test_load_pymeasure_class_missing_returns_none() -> None:
    assert _load_pymeasure_class("pymeasure.instruments.does_not_exist", "Nope") is None


def test_ensure_registration_degrades_gracefully() -> None:
    """pymeasure 未安装时注册不抛异常，驱动名缺席注册表。"""
    from ate_platform.drivers.pymeasure_wrappers import register as reg_module

    # 重置模块级注册标志，强制重新走注册路径
    reg_module._registered = False
    try:
        results = ensure_pymeasure_drivers_registered()
    finally:
        # 恢复标志，避免影响其他测试
        reg_module._registered = False
    # 环境里未安装 pymeasure → 至少 keysight_34465a 不应可用
    if "pymeasure" in results and results["pymeasure"] is False:
        assert "keysight_34465a" not in DriverRegistry.list_drivers()
    assert isinstance(results, dict)


def test_registered_pairs_are_proxy_compatible() -> None:
    """已注册的 pymeasure 驱动能被代理进程 _create_driver 的双层取法取到。"""
    results = ensure_pymeasure_drivers_registered()
    if not results.get("keysight_34465a"):
        pytest.skip("pymeasure 未安装，跳过代理兼容性检查")
    hal_cls = DriverRegistry.get_driver("keysight_34465a", layer="hal")
    mal_cls = DriverRegistry.get_driver("keysight_34465a", layer="mal")
    assert hal_cls is not None
    assert mal_cls is not None
    assert issubclass(mal_cls, PyMeasureAbstraction)
