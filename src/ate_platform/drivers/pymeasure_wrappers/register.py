"""pymeasure 预建驱动注册（懒加载 + 优雅降级）。

pymeasure 未安装（或安装失败）时不阻塞平台启动：仅记录日志，
相关驱动名缺席注册表，代理进程/脚本引用时按「无此驱动」处理。

注册的驱动（HAL + MAL 对，对齐双基类架构）：
- ``keysight_34465a`` — Keysight 34465A 数字万用表（DMM）
- ``keysight_e36312a`` — Keysight E36312A 三路电源（PSU）
- ``rigol_dp800`` — 普源 DP800 系列电源（PSU）
"""

from __future__ import annotations

from typing import Any, cast

import structlog

logger = structlog.get_logger(__name__)

_registered: bool = False

# 预建驱动清单：driver 名 → (pymeasure 模块路径, 类名)
_PYMEASURE_DRIVERS: list[tuple[str, str, str]] = [
    ("keysight_34465a", "pymeasure.instruments.keysight", "Keysight34465A"),
    ("keysight_e36312a", "pymeasure.instruments.keysight", "KeysightE36312A"),
    ("rigol_dp800", "pymeasure.instruments.rigol", "RigolDP800"),
]


def _load_pymeasure_class(module_path: str, class_name: str) -> type[Any] | None:
    """懒加载 pymeasure 驱动类；失败返回 None 并记录日志。"""
    try:
        import importlib

        module = importlib.import_module(module_path)
        return cast(type[Any], getattr(module, class_name))
    except Exception as e:  # noqa: BLE001 — pymeasure 缺失/版本差异均优雅降级
        logger.warning(
            "pymeasure_driver_unavailable",
            module=module_path,
            class_name=class_name,
            error=str(e),
        )
        return None


def register_pymeasure_drivers() -> dict[str, bool]:
    """注册所有可用的 pymeasure 预建驱动。

    Returns:
        已成功注册的驱动名 → True 的字典（未注册的驱动名缺席或为 False）。
    """
    global _registered
    from ate_platform.drivers.base import DriverRegistry
    from ate_platform.drivers.pymeasure_wrappers.adapter import (
        PyMeasureAbstraction,
        PyMeasureAdapter,
    )

    results: dict[str, bool] = {}
    for driver_name, module_path, class_name in _PYMEASURE_DRIVERS:
        pm_cls = _load_pymeasure_class(module_path, class_name)
        if pm_cls is None:
            results[driver_name] = False
            continue
        hal_cls = PyMeasureAdapter.wrap(pm_cls)
        try:
            DriverRegistry.register(
                driver_name,
                hal_cls=hal_cls,
                mal_cls=PyMeasureAbstraction,
            )
            results[driver_name] = True
        except Exception as e:  # noqa: BLE001 — 单驱动注册失败不影响其余
            logger.error("pymeasure_register_failed", driver=driver_name, error=str(e))
            results[driver_name] = False
    _registered = True
    return results


def ensure_pymeasure_drivers_registered() -> dict[str, bool]:
    """幂等注册：已注册过则直接返回当前结果。"""
    if _registered:
        from ate_platform.drivers.base import DriverRegistry

        return {
            name: name in DriverRegistry.list_drivers()
            for name, _, _ in _PYMEASURE_DRIVERS
        }
    return register_pymeasure_drivers()
