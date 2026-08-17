"""FixtureController 夹具控制建模测试（设计文档 §6.7.1，F9，任务 #7）。

覆盖：
- clamp：气缸 extend + 位置传感器确认 + status=clamped
- release：气缸 retract + status=idle
- set_route：矩阵开关路由（模拟 + proxy 转发）
- read_sensor：传感器读数（模拟 + proxy）
- 传感器超时 -> FixtureTimeoutError
- 缺失继电器/传感器 -> FixtureError
- proxy 鸭子类型：同步 call_method / async call_method / ProxyManager.client
- 模拟模式（proxy=None）无硬件可跑动作序列
"""

from __future__ import annotations

import pytest

from ate_platform.fixture import FixtureController, FixtureError, FixtureTimeoutError


def _fixture_config() -> dict[str, object]:
    return {
        "actuators": [
            {
                "id": "cyl1",
                "type": "cylinder",
                "control_resource": "FIX_IO",
                "method": "set_actuator",
                "extend_method": "extend",
                "retract_method": "retract",
            },
        ],
        "relays": [
            {"id": "R1", "type": "spdt", "control_resource": "MATRIX",
             "method": "set_route"},
        ],
        "sensors": [
            {"id": "clamp_position", "type": "position", "unit": "",
             "read_resource": "SENSOR_IO", "method": "read",
             "simulate_value": 1.0},
            {"id": "temperature", "type": "temperature", "unit": "C",
             "read_resource": "SENSOR_IO", "method": "read",
             "simulate_value": 25.0},
        ],
    }


class TestSimulatedMode:
    """proxy=None：无硬件可跑动作序列。"""

    async def test_clamp_simulated(self) -> None:
        ctrl = FixtureController("FIX1", _fixture_config())
        await ctrl.clamp()
        state = ctrl.get_state()
        assert state["status"] == "clamped"
        assert state["actuators"]["cyl1"] == "extend"

    async def test_release_simulated(self) -> None:
        ctrl = FixtureController("FIX1", _fixture_config())
        await ctrl.clamp()
        await ctrl.release()
        state = ctrl.get_state()
        assert state["status"] == "idle"
        assert state["actuators"]["cyl1"] == "retract"

    async def test_set_route_simulated(self) -> None:
        ctrl = FixtureController("FIX1", _fixture_config())
        await ctrl.set_route("R1", "DUT1")
        assert ctrl.get_state()["relays"]["R1"] == "DUT1"

    async def test_read_sensor_simulated(self) -> None:
        ctrl = FixtureController("FIX1", _fixture_config())
        value = await ctrl.read_sensor("clamp_position")
        assert value == 1.0

    async def test_full_action_sequence(self) -> None:
        """仿真动作序列：clamp -> set_route -> read -> release。"""
        ctrl = FixtureController("FIX1", _fixture_config())
        await ctrl.clamp()
        await ctrl.set_route("R1", "TP2")
        temp = await ctrl.read_sensor("temperature")
        await ctrl.release()
        state = ctrl.get_state()
        assert state["status"] == "idle"
        assert temp == 25.0
        assert state["relays"]["R1"] == "TP2"


class TestProxyForwarding:
    """proxy 转发：同步/异步 call_method、ProxyManager.client。"""

    async def test_sync_call_method_proxy(self) -> None:
        calls: list[tuple[str, tuple, str]] = []

        class _SyncProxy:
            def call_method(self, method: str, *args: object) -> object:
                calls.append((method, args, "SYNC"))
                if method == "read":
                    return 1.0
                return None

        ctrl = FixtureController("FIX1", _fixture_config(), proxy_client=_SyncProxy())
        await ctrl.clamp()
        assert calls, "夹具动作应转发到 proxy"
        methods = [c[0] for c in calls]
        assert "extend" in methods
        assert calls[0][2] == "SYNC"

    async def test_async_call_method_proxy(self) -> None:
        calls: list[str] = []

        class _AsyncProxy:
            async def call_method(self, method: str, *args: object) -> object:
                calls.append(method)
                if method == "read":
                    return 1.0
                return None

        ctrl = FixtureController("FIX1", _fixture_config(), proxy_client=_AsyncProxy())
        await ctrl.clamp()
        assert "extend" in calls

    async def test_proxy_manager_client_resolution(self) -> None:
        """ProxyManager 风格：proxy.client(resource_id) 分发到资源客户端。"""
        resource_calls: dict[str, list[str]] = {}

        class _ResourceClient:
            def __init__(self, resource_id: str) -> None:
                self.resource_id = resource_id

            def call_method(self, method: str, *args: object) -> object:
                resource_calls.setdefault(self.resource_id, []).append(method)
                if method == "read":
                    return 1.0
                return None

        class _Manager:
            def client(self, resource_id: str) -> _ResourceClient:
                return _ResourceClient(resource_id)

        ctrl = FixtureController("FIX1", _fixture_config(), proxy_client=_Manager())
        await ctrl.clamp()
        await ctrl.set_route("R1", "TP1")
        await ctrl.read_sensor("clamp_position")
        assert "extend" in resource_calls["FIX_IO"]
        assert "set_route" in resource_calls["MATRIX"]
        assert "read" in resource_calls["SENSOR_IO"]

    async def test_read_sensor_proxy_value(self) -> None:
        class _Proxy:
            def call_method(self, method: str, *args: object) -> object:
                if method == "read":
                    return 24.7
                return None

        ctrl = FixtureController("FIX1", _fixture_config(), proxy_client=_Proxy())
        assert await ctrl.read_sensor("temperature") == 24.7


class TestErrors:
    async def test_sensor_timeout(self) -> None:
        """位置传感器未到位 -> FixtureTimeoutError。"""
        config = _fixture_config()
        config["sensors"] = [
            {"id": "clamp_position", "type": "position", "simulate_value": 0.0},
        ]
        ctrl = FixtureController("FIX1", config)
        with pytest.raises(FixtureTimeoutError):
            await ctrl.clamp(timeout=0.1)

    async def test_missing_relay(self) -> None:
        ctrl = FixtureController("FIX1", _fixture_config())
        with pytest.raises(FixtureError, match="no relay"):
            await ctrl.set_route("R_NOPE", "TP1")

    async def test_missing_sensor(self) -> None:
        ctrl = FixtureController("FIX1", _fixture_config())
        with pytest.raises(FixtureError, match="no sensor"):
            await ctrl.read_sensor("s_nope")

    async def test_proxy_error_propagates(self) -> None:
        class _FailingProxy:
            def call_method(self, method: str, *args: object) -> object:
                raise RuntimeError("IO down")

        ctrl = FixtureController("FIX1", _fixture_config(), proxy_client=_FailingProxy())
        with pytest.raises(RuntimeError, match="IO down"):
            await ctrl.read_sensor("temperature")


class TestState:
    def test_get_state_shape(self) -> None:
        ctrl = FixtureController("FIX1", _fixture_config())
        state = ctrl.get_state()
        assert state["fixture_id"] == "FIX1"
        assert state["status"] == "idle"
        assert set(state) == {"fixture_id", "status", "actuators", "relays", "sensors"}

    def test_repr(self) -> None:
        ctrl = FixtureController("FIX1", _fixture_config())
        assert "FIX1" in repr(ctrl)
        assert "simulated=True" in repr(ctrl)
