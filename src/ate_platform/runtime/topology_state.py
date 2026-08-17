"""TopologyRuntimeState — 工装拓扑运行时状态（设计文档 §8.3.6，任务 #9）。

维护仪器/链路/继电器/测量/夹具的运行时状态快照，作为 SSE 推送的数据源：
- update_instrument / update_link / update_relay / update_measurement /
  update_fixture：各实体状态更新
- 每次更新触发 on_change 回调（SSE 发布钩子），并记录最近一次变更
- snapshot()：完整状态快照（前端首次连接时下发）

事件类型（§8.3.6）：instrument / link / relay / measurement / fault。
"""

from __future__ import annotations

import time
from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)


class TopologyRuntimeState:
    """工装拓扑运行时状态（单执行实例）。

    Args:
        run_id: 执行运行标识（SSE 关联用）。
        on_change: 状态变更回调 (event_type, data) —— 用于发布到 SSE。
    """

    #: 事件类型
    EVT_INSTRUMENT = "instrument"
    EVT_LINK = "link"
    EVT_RELAY = "relay"
    EVT_MEASUREMENT = "measurement"
    EVT_FAULT = "fault"
    EVT_FIXTURE = "fixture"

    def __init__(
        self,
        run_id: str,
        on_change: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.run_id = run_id
        self._on_change: Callable[[str, dict[str, Any]], None] | None = on_change
        # 各实体状态（初始化自拓扑或懒创建）
        self._instruments: dict[str, dict[str, Any]] = {}
        self._links: dict[str, dict[str, Any]] = {}
        self._relays: dict[str, dict[str, Any]] = {}
        self._fixtures: dict[str, dict[str, Any]] = {}
        self._measurements: dict[str, dict[str, Any]] = {}
        self._faults: list[dict[str, Any]] = []
        self._last_update: float = 0.0

    # ------------------------------------------------------------------
    # 状态更新
    # ------------------------------------------------------------------

    def update_instrument(
        self, instrument_id: str, status: str,
        reading: float | None = None, error: str | None = None,
    ) -> None:
        """更新仪器状态（§8.3.6 仪器节点）。"""
        self._instruments[instrument_id] = {
            "instrument_id": instrument_id,
            "status": status,
            "reading": reading,
            "error": error,
        }
        self._emit(self.EVT_INSTRUMENT, self._instruments[instrument_id])

    def update_link(self, link_id: str, active: bool,
                    status: str = "idle") -> None:
        """更新链路状态（idle/active/fault，§8.3.6 链路状态机）。"""
        self._links[link_id] = {
            "link_id": link_id,
            "active": active,
            "status": status,
        }
        self._emit(self.EVT_LINK, self._links[link_id])

    def update_relay(self, relay_id: str, state: str) -> None:
        """更新继电器状态（open/closed，§8.3.6 继电器指示器）。"""
        self._relays[relay_id] = {
            "relay_id": relay_id,
            "state": state,
        }
        self._emit(self.EVT_RELAY, self._relays[relay_id])

    def update_measurement(
        self, dut_id: str, testpoint_id: str, value: float | None,
        status: str = "idle",
    ) -> None:
        """更新 DUT 测试点测量值（§8.3.6 测量事件）。"""
        key = f"{dut_id}:{testpoint_id}"
        self._measurements[key] = {
            "dut_id": dut_id,
            "testpoint_id": testpoint_id,
            "value": value,
            "status": status,
        }
        self._emit(self.EVT_MEASUREMENT, self._measurements[key])

    def update_fixture(self, fixture_id: str, status: str,
                       sensors: dict[str, Any] | None = None) -> None:
        """更新夹具状态（夹紧状态/传感器读数，§8.3.6 夹具节点）。"""
        self._fixtures[fixture_id] = {
            "fixture_id": fixture_id,
            "status": status,
            "sensors": sensors or {},
        }
        self._emit(self.EVT_FIXTURE, self._fixtures[fixture_id])

    def add_fault(self, fault: dict[str, Any]) -> None:
        """记录故障（§8.3.6 fault 事件 + §8.3.7 故障定位结果）。"""
        fault = dict(fault)
        fault.setdefault("timestamp", time.time())
        self._faults.append(fault)
        self._emit(self.EVT_FAULT, {
            "fault": fault,
            "location": {
                "route_id": fault.get("route_id"),
                "testpoint_id": fault.get("testpoint_id"),
                "suspect_links": fault.get("suspect_links", []),
                "suspect_relays": fault.get("suspect_relays", []),
            },
        })

    def clear_faults(self) -> None:
        """清除全部故障（故障清除 -> 链路回 idle）。"""
        self._faults.clear()

    # ------------------------------------------------------------------
    # 快照与查询
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """完整状态快照（前端首次连接时下发）。"""
        return {
            "run_id": self.run_id,
            "instruments": list(self._instruments.values()),
            "links": list(self._links.values()),
            "relays": list(self._relays.values()),
            "fixtures": list(self._fixtures.values()),
            "measurements": list(self._measurements.values()),
            "faults": list(self._faults),
        }

    def get_instrument(self, instrument_id: str) -> dict[str, Any] | None:
        """取仪器状态。"""
        return self._instruments.get(instrument_id)

    def get_measurement(self, dut_id: str, testpoint_id: str) -> dict[str, Any] | None:
        """取测量状态。"""
        return self._measurements.get(f"{dut_id}:{testpoint_id}")

    def get_faults(self) -> list[dict[str, Any]]:
        """取全部故障。"""
        return list(self._faults)

    @property
    def last_update(self) -> float:
        """最近一次状态更新时间戳。"""
        return self._last_update

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """触发变更回调并更新时间戳。"""
        self._last_update = time.time()
        if self._on_change is not None:
            try:
                self._on_change(event_type, data)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "topology_state_callback_error",
                    run_id=self.run_id,
                    event_type=event_type,
                    error=str(e),
                )
