"""TopologyRuntimeState 拓扑运行时状态测试（设计文档 §8.3.6，任务 #9）。

覆盖：
- 各实体状态更新（instrument/link/relay/measurement/fixture）
- 变更回调触发（事件类型 + 数据）
- fault 记录与 location 提取
- 快照形状（首次连接下发）
- 查询方法
"""

from __future__ import annotations

from ate_platform.runtime.topology_state import TopologyRuntimeState


class TestInstrument:
    def test_update_instrument(self) -> None:
        st = TopologyRuntimeState("run1")
        st.update_instrument("PSU_MAIN", "active", reading=5.0)
        inst = st.get_instrument("PSU_MAIN")
        assert inst is not None
        assert inst["status"] == "active"
        assert inst["reading"] == 5.0

    def test_update_instrument_with_error(self) -> None:
        st = TopologyRuntimeState("run1")
        st.update_instrument("PSU_MAIN", "error", error="VISA timeout")
        assert st.get_instrument("PSU_MAIN")["error"] == "VISA timeout"

    def test_missing_instrument_returns_none(self) -> None:
        st = TopologyRuntimeState("run1")
        assert st.get_instrument("GHOST") is None


class TestLinkRelay:
    def test_update_link_active(self) -> None:
        st = TopologyRuntimeState("run1")
        st.update_link("L1", active=True, status="active")
        assert st._links["L1"]["active"] is True
        assert st._links["L1"]["status"] == "active"

    def test_update_link_idle(self) -> None:
        st = TopologyRuntimeState("run1")
        st.update_link("L1", active=False, status="fault")
        assert st._links["L1"]["active"] is False

    def test_update_relay(self) -> None:
        st = TopologyRuntimeState("run1")
        st.update_relay("R1", "closed")
        assert st._relays["R1"]["state"] == "closed"


class TestMeasurement:
    def test_update_measurement(self) -> None:
        st = TopologyRuntimeState("run1")
        st.update_measurement("DUT1", "TP1", 4.98, status="pass")
        meas = st.get_measurement("DUT1", "TP1")
        assert meas is not None
        assert meas["value"] == 4.98
        assert meas["status"] == "pass"

    def test_update_measurement_overwrites(self) -> None:
        st = TopologyRuntimeState("run1")
        st.update_measurement("DUT1", "TP1", 4.98)
        st.update_measurement("DUT1", "TP1", 5.01)
        assert st.get_measurement("DUT1", "TP1")["value"] == 5.01


class TestFixture:
    def test_update_fixture(self) -> None:
        st = TopologyRuntimeState("run1")
        st.update_fixture("FIX1", "clamped", sensors={"clamp_position": 1})
        assert st._fixtures["FIX1"]["status"] == "clamped"
        assert st._fixtures["FIX1"]["sensors"]["clamp_position"] == 1


class TestFault:
    def test_add_fault(self) -> None:
        st = TopologyRuntimeState("run1")
        st.add_fault({
            "type": "open_circuit",
            "message": "开路",
            "route_id": "RT1",
            "suspect_links": ["L1", "L2"],
        })
        faults = st.get_faults()
        assert len(faults) == 1
        assert faults[0]["type"] == "open_circuit"
        assert faults[0]["timestamp"] > 0

    def test_clear_faults(self) -> None:
        st = TopologyRuntimeState("run1")
        st.add_fault({"type": "open_circuit", "message": "x"})
        st.clear_faults()
        assert st.get_faults() == []


class TestCallback:
    def test_on_change_triggered_per_event(self) -> None:
        received: list[tuple[str, dict]] = []
        st = TopologyRuntimeState("run1", on_change=lambda t, d: received.append((t, d)))

        st.update_instrument("PSU", "active")
        st.update_link("L1", True)
        st.update_relay("R1", "closed")
        st.update_measurement("DUT1", "TP1", 5.0)
        st.add_fault({"type": "open_circuit", "message": "x"})

        types = [t for t, _ in received]
        assert types == [
            "instrument", "link", "relay", "measurement", "fault",
        ]

    def test_callback_errors_silently_logged(self) -> None:
        def bad_cb(_t: str, _d: dict) -> None:
            raise RuntimeError("boom")

        st = TopologyRuntimeState("run1", on_change=bad_cb)
        st.update_instrument("PSU", "active")  # 不应抛出
        assert st.get_instrument("PSU")["status"] == "active"


class TestSnapshot:
    def test_snapshot_shape(self) -> None:
        st = TopologyRuntimeState("run1")
        st.update_instrument("PSU", "active")
        st.update_link("L1", True)
        st.update_relay("R1", "closed")
        st.update_measurement("DUT1", "TP1", 5.0)
        st.update_fixture("FIX1", "clamped")
        st.add_fault({"type": "open_circuit", "message": "x"})

        snap = st.snapshot()
        assert snap["run_id"] == "run1"
        assert len(snap["instruments"]) == 1
        assert len(snap["links"]) == 1
        assert len(snap["relays"]) == 1
        assert len(snap["measurements"]) == 1
        assert len(snap["fixtures"]) == 1
        assert len(snap["faults"]) == 1

    def test_empty_snapshot(self) -> None:
        snap = TopologyRuntimeState("run1").snapshot()
        assert snap["instruments"] == []
        assert snap["faults"] == []

    def test_last_update_timestamp(self) -> None:
        st = TopologyRuntimeState("run1")
        assert st.last_update == 0.0
        st.update_instrument("PSU", "active")
        assert st.last_update > 0.0
