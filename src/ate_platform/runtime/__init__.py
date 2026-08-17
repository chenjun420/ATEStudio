"""运行时模块（设计文档 §8.3.6/§8.3.7，任务 #9）。

- FaultLocalizer / FaultLocation：故障定位器（§8.3.7）。
"""

from .fault_localizer import FaultLocalizer, FaultLocation

__all__ = ["FaultLocalizer", "FaultLocation"]
