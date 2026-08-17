"""夹具控制模块（设计文档 §6.7.1，V3.2 新增）。

FixtureController 管理夹具的主动控制实体（气缸/继电器/传感器）：
- clamp/release: 夹紧/松开（气缸 + 位置传感器确认）
- set_route: 矩阵开关路由设置
- read_sensor: 传感器实时读数
- get_state: 运行时状态快照

模拟模式（proxy_client=None）无硬件即可驱动夹具动作序列，
与虚拟仿真调试系统（§7）协同工作。
"""

from .fixture_controller import (
    FixtureController,
    FixtureError,
    FixtureTimeoutError,
)

__all__ = [
    "FixtureController",
    "FixtureError",
    "FixtureTimeoutError",
]
