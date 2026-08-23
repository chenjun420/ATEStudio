"""Tests for GET /fixtures/{fixture_id}/fault-stats (T35, v41-gap-analysis #35).

设计文档 §8.3 历史故障热力图数据源端点。

覆盖：
- 匿名请求（dev_mode 关闭）→ 401（fixtures router 挂在 _PROTECTED_ROUTERS）
- 未知 fixture → 404
- 已存在 fixture、零历史 → 200 + {links: {}, generated_at}（诚实空，见
  _load_fault_events 文档：当前无故障事件持久化表）
- 响应形状：仅 links/generated_at 两键；generated_at 为可解析 ISO-8601 UTC
- 纯聚合函数 aggregate_fault_events：多事件计数 + last_seen 取最大时间戳
- 纯聚合函数防御：缺 link_id / 非法条目跳过
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ate_cloud.api.v1.fixtures import aggregate_fault_events
from ate_cloud.config import settings


def _valid_topo_dict(name: str = "heat-fixture") -> dict[str, object]:
    """合法最小拓扑（PSU--夹具--DUT），与 test_fixtures.py 同构。"""
    return {
        "name": name,
        "product_model": "comm_module_v2",
        "instruments": [
            {
                "id": "PSU_MAIN",
                "name": "电源",
                "type": "psu",
                "communication": {"type": "gpib", "address": "5"},
                "channels": [
                    {
                        "id": "CH1",
                        "type": "voltage",
                        "direction": "output",
                        "specs": {"max_current": 5.0, "rated_current": 10.0},
                    },
                ],
            },
        ],
        "fixtures": [
            {
                "id": "FIX1",
                "name": "产测夹具",
                "terminals": [{"id": "T1", "type": "voltage", "direction": "bidirectional"}],
                "dut_slot_count": 1,
            },
        ],
        "duts": [
            {
                "id": "DUT1",
                "product_model": "comm_module_v2",
                "test_points": [
                    {"id": "TP1", "net": "VOUT", "type": "voltage",
                     "expected_range": {"min": 4.5, "max": 5.5}},
                ],
            },
        ],
        "links": [
            {
                "id": "L1",
                "from": {"entity_type": "instrument_channel",
                         "entity_id": "PSU_MAIN", "port_id": "CH1"},
                "to": {"entity_type": "fixture_terminal",
                       "entity_id": "FIX1", "port_id": "T1"},
                "signal_type": "power",
            },
        ],
        "routes": [],
    }


async def _create_fixture(client) -> str:
    """创建一个合法工装拓扑，返回其 id。"""
    resp = await client.post(
        "/api/v1/fixtures",
        json={"name": "热力图工装", "topology_data": _valid_topo_dict("heat-fixture")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestFaultStatsEndpoint:
    @pytest.mark.asyncio
    async def test_anonymous_401(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """匿名请求（dev_mode off）必须被 JWT 保护拒绝为 401（T17 先例）。"""
        monkeypatch.setattr(settings, "dev_mode", False)
        resp = await client.get("/api/v1/fixtures/some-id/fault-stats")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_fixture_404(self, client) -> None:
        resp = await client.get("/api/v1/fixtures/nonexistent-id/fault-stats")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Fixture topology not found"

    @pytest.mark.asyncio
    async def test_existing_fixture_zero_history_honest_empty(self, client) -> None:
        """零历史 → 200 + links 空对象（无持久化表时的诚实空约定）。"""
        fid = await _create_fixture(client)
        resp = await client.get(f"/api/v1/fixtures/{fid}/fault-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["links"] == {}

    @pytest.mark.asyncio
    async def test_response_shape_and_generated_at_iso(self, client) -> None:
        """响应仅含 links/generated_at；generated_at 可解析且带时区。"""
        fid = await _create_fixture(client)
        resp = await client.get(f"/api/v1/fixtures/{fid}/fault-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"links", "generated_at"}
        generated = datetime.fromisoformat(data["generated_at"])
        assert generated.tzinfo is not None


class TestAggregateFaultEvents:
    """纯聚合函数单元测试——未来接入 fault_events 表时逻辑已锁定。"""

    def test_counts_and_last_seen_max(self) -> None:
        events = [
            {"link_id": "L1", "detected_at": "2026-08-01T10:00:00+00:00"},
            {"link_id": "L1", "detected_at": "2026-08-03T12:00:00+00:00"},
            {"link_id": "L2", "detected_at": "2026-08-02T08:00:00+00:00"},
        ]
        agg = aggregate_fault_events(events)
        assert agg["L1"]["count"] == 2
        assert agg["L1"]["last_seen"] == "2026-08-03T12:00:00+00:00"
        assert agg["L2"] == {"count": 1, "last_seen": "2026-08-02T08:00:00+00:00"}

    def test_skips_invalid_entries(self) -> None:
        events: list[object] = [
            "not-a-dict",
            {"detected_at": "2026-08-01T10:00:00+00:00"},  # 缺 link_id
            {"link_id": ""},  # 空 link_id
            {"link_id": "L9"},  # 缺 detected_at 也计数
        ]
        agg = aggregate_fault_events(events)  # type: ignore[arg-type]
        assert agg == {"L9": {"count": 1, "last_seen": None}}

    def test_empty_input(self) -> None:
        assert aggregate_fault_events([]) == {}
