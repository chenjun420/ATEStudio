"""pymeasure 预建驱动适配层（F1）。

把 pymeasure 生态预建的仪表驱动适配到平台 HAL/MAL 双基类架构：
- :class:`PyMeasureAdapter` — 组合模式包装（规避多重继承 MRO 风险，见设计文档 §6.2.3 降级路径）
- :func:`register_pymeasure_drivers` — 懒加载注册，pymeasure 未安装时优雅降级

设计文档 §6.2.2/6.2.3 的「多重继承 pymeasure.Instrument」方案在现有
BaseDriver(HAL)/BaseAbstraction(MAL) 架构下不可行（MRO 冲突风险），
故按文档给出的降级路径采用组合模式：HAL 驱动内部持有 pymeasure 实例，
``__getattr__`` 透传语义方法；MAL 抽象提供平台统一接口。
"""

from ate_platform.drivers.pymeasure_wrappers.adapter import PyMeasureAdapter, PyMeasureAbstraction
from ate_platform.drivers.pymeasure_wrappers.register import (
    ensure_pymeasure_drivers_registered,
    register_pymeasure_drivers,
)

__all__ = [
    "PyMeasureAdapter",
    "PyMeasureAbstraction",
    "register_pymeasure_drivers",
    "ensure_pymeasure_drivers_registered",
]
