"""Fixture controller — 夹具控制建模（设计文档 §6.7.1，F9）。

夹具是具备主动控制能力的实体（气缸/继电器/传感器），而非被动连接体。
FixtureController 管理夹具动作：
- clamp/release：气缸推进/回缩 + 位置传感器确认
- set_route：矩阵开关路由设置
- read_sensor：传感器实时读数
- get_state：夹具运行时状态快照

proxy_client 为夹具控制 IO 的代理客户端（鸭子类型）：
- ``None``：模拟模式——记录动作到状态，传感器返回 config 默认值（仿真无硬件可跑）
- 具有 ``client(resource_id)``：ProxyManager 风格，先取资源客户端再调用
- 直接 InstrumentClient / ``call_method`` 鸭子类型

同步 IPC 调用（InstrumentClient.call_method 阻塞）用 ``asyncio.to_thread``
包装，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class FixtureError(RuntimeError):
    """夹具控制异常（动作失败/超时/元件缺失）。"""


class FixtureTimeoutError(FixtureError):
    """夹具动作超时（如气缸位置传感器未在时限内到位）。"""


class FixtureController:
    """夹具控制器，管理气缸/继电器/传感器。

    Attributes:
        fixture_id: 夹具标识。
        config: 夹具配置（actuators/relays/sensors，来自工装拓扑 Fixture 模型）。
        proxy_client: 夹具控制 IO 代理客户端（可为 None 模拟）。
        state: 运行时状态（actuators/relays/sensors/status）。
    """

    #: 传感器默认超时（秒）
    DEFAULT_SENSOR_TIMEOUT: float = 5.0

    def __init__(
        self,
        fixture_id: str,
        config: dict[str, Any] | None = None,
        proxy_client: Any = None,
    ) -> None:
        """初始化夹具控制器。

        Args:
            fixture_id: 夹具标识。
            config: 夹具配置 dict（含 actuators/relays/sensors 列表，
                每个元件含 id/control_resource/method 等字段）。
            proxy_client: 夹具控制 IO 代理客户端（None 为模拟模式）。
        """
        self.fixture_id = fixture_id
        self.config: dict[str, Any] = config or {}
        self.proxy: Any = proxy_client
        self._state: dict[str, Any] = {
            "fixture_id": fixture_id,
            "status": "idle",
            "actuators": {},
            "relays": {},
            "sensors": {},
        }
        logger.info(
            "fixture_controller_init",
            fixture_id=fixture_id,
            simulated=self.proxy is None,
        )

    # ------------------------------------------------------------------
    # 主动控制动作
    # ------------------------------------------------------------------

    async def clamp(self, timeout: float | None = None) -> None:
        """夹紧动作：气缸推进 + 位置传感器确认（§6.7.1）。

        Args:
            timeout: 传感器到位超时（秒），默认 DEFAULT_SENSOR_TIMEOUT。

        Raises:
            FixtureTimeoutError: 位置传感器未在时限内到位。
        """
        effective_timeout = timeout or self.DEFAULT_SENSOR_TIMEOUT
        for actuator in self._get("actuators"):
            if actuator.get("type") == "cylinder":
                await self._set_actuator(actuator, "extend")
        # 位置传感器确认到位（value=1）
        position_sensor = self._position_sensor()
        if position_sensor is not None:
            await self._wait_sensor(
                position_sensor, value=1, timeout=effective_timeout,
            )
        self._state["status"] = "clamped"
        logger.info("fixture_clamped", fixture_id=self.fixture_id)

    async def release(self) -> None:
        """松开动作：气缸回缩（§6.7.1）。"""
        for actuator in self._get("actuators"):
            if actuator.get("type") == "cylinder":
                await self._set_actuator(actuator, "retract")
        self._state["status"] = "idle"
        logger.info("fixture_released", fixture_id=self.fixture_id)

    async def set_route(self, relay_id: str, route: str) -> None:
        """设置矩阵开关路由（§6.7.1）。

        Args:
            relay_id: 继电器标识。
            route: 路由目标（如 "DUT1" / "TP2"）。

        Raises:
            FixtureError: 继电器不存在。
        """
        relay = self._get_item("relays", relay_id)
        if relay is None:
            msg = f"Fixture {self.fixture_id} has no relay '{relay_id}'"
            raise FixtureError(msg)
        method = relay.get("method") or "set_route"
        await self._proxy_call(relay, method, route)
        self._state["relays"][relay_id] = route
        logger.info(
            "fixture_route_set", fixture_id=self.fixture_id,
            relay=relay_id, route=route,
        )

    async def read_sensor(self, sensor_id: str) -> float:
        """读取传感器实时值（§6.7.1）。

        Args:
            sensor_id: 传感器标识。

        Returns:
            传感器读数。

        Raises:
            FixtureError: 传感器不存在。
        """
        sensor = self._get_item("sensors", sensor_id)
        if sensor is None:
            msg = f"Fixture {self.fixture_id} has no sensor '{sensor_id}'"
            raise FixtureError(msg)
        method = sensor.get("method") or "read"
        value = await self._proxy_call(sensor, method)
        self._state["sensors"][sensor_id] = value
        return float(value)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        """返回夹具运行时状态快照。

        Returns:
            dict: fixture_id/status/actuators/relays/sensors。
        """
        return dict(self._state)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _get(self, key: str) -> list[dict[str, Any]]:
        """取 config 中指定类别元件列表。"""
        return list(self.config.get(key, []) or [])

    def _get_item(self, category: str, item_id: str) -> dict[str, Any] | None:
        """按 id 查找元件。"""
        for item in self._get(category):
            if item.get("id") == item_id:
                return item
        return None

    def _position_sensor(self) -> dict[str, Any] | None:
        """取位置传感器（clamp_position 或 type=position 的传感器）。"""
        for sensor in self._get("sensors"):
            if sensor.get("id") == "clamp_position":
                return sensor
        for sensor in self._get("sensors"):
            if sensor.get("type") == "position":
                return sensor
        return None

    async def _set_actuator(self, actuator: dict[str, Any], action: str) -> None:
        """执行气缸动作（extend/retract）。"""
        method = actuator.get(f"{action}_method") or actuator.get("method") or action
        await self._proxy_call(actuator, method)
        self._state["actuators"][actuator.get("id", "?")] = action

    async def _wait_sensor(
        self, sensor: dict[str, Any], value: Any, timeout: float,
    ) -> None:
        """轮询传感器直到到位或超时。

        Args:
            sensor: 传感器配置。
            value: 期望值。
            timeout: 超时秒数。

        Raises:
            FixtureTimeoutError: 超时未到位。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = await self.read_sensor(sensor["id"])
            if current == value:
                return
            await asyncio.sleep(0.05)
        msg = (
            f"Fixture {self.fixture_id} sensor {sensor['id']} "
            f"not reached value {value} within {timeout}s"
        )
        raise FixtureTimeoutError(msg)

    async def _proxy_call(
        self, item: dict[str, Any], method: str, *args: Any, **kwargs: Any,
    ) -> Any:
        """调用夹具控制 IO（模拟模式或经 proxy 转发）。

        优先使用元件 config 的 ``control_resource`` 定位资源；
        proxy 形态：
        - None：模拟模式——记录动作，传感器返回 config 默认值。
        - 有 ``client(resource_id)``：取资源客户端再 call_method。
        - 否则直接 call_method（鸭子类型）。
        """
        if self.proxy is None:
            # 模拟模式：返回 config 默认值
            default = item.get("default_value")
            if default is not None:
                return default
            if "read" in method or method == "read":
                return item.get("simulate_value", 1.0)
            return None

        target = self.proxy
        if hasattr(self.proxy, "client"):
            # 传感器用 read_resource，执行器/继电器用 control_resource（§6.7.1）
            resource = item.get("control_resource") or item.get("read_resource")
            if resource:
                target = self.proxy.client(resource)

        call = getattr(target, "call_method", None)
        if call is None:
            # 直接方法调用（模拟客户端/测试替身）
            func = getattr(target, method)
        else:
            func = lambda *a, **kw: call(method, *a, **kw)  # noqa: E731

        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        # 同步阻塞调用（InstrumentClient 等）：避免阻塞事件循环
        return await asyncio.to_thread(lambda: result)

    def __repr__(self) -> str:
        """简洁表示便于诊断。"""
        return (
            f"FixtureController(fixture_id={self.fixture_id}, "
            f"status={self._state.get('status')}, simulated={self.proxy is None})"
        )
