"""Tests for fixture topology CRUD + validate + duplicate + versions + export API.

设计文档 §9.2 工装拓扑 API。

覆盖：
- POST /api/v1/fixtures 创建（201 / 无效 topology_data 422 / 重复 409）
- GET 列表（items+total，product_model 过滤）
- GET /{id} 详情（200 / 404）
- PUT /{id} 更新（版本自动递增 / 显式 version / 404 / 409）
- DELETE /{id}（204 / 404）
- POST /{id}/validate（合法/非法拓扑）
- POST /{id}/duplicate（副本 version 重置）
- GET /{id}/versions 版本历史
- POST /{id}/export JSON/YAML
- /templates 设备模板创建/列表
"""

from __future__ import annotations

import pytest

from shared.fixture_topology import ChannelType


def _valid_topo_dict(name: str = "PSU 产测工装") -> dict[str, object]:
    """合法拓扑：PSU--夹具--DUT，接地完整。"""
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
                "terminals": [
                    {"id": "T1", "type": "voltage", "direction": "bidirectional"},
                    {"id": "TGND", "type": "voltage", "direction": "bidirectional"},
                ],
                "relays": [
                    {"id": "R1", "type": "spdt", "control_signal": "GPIO1"},
                ],
                "actuators": [
                    {"id": "A1", "type": "cylinder"},
                ],
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
                "max_current": 2.0,
            },
            {
                "id": "L2",
                "from": {"entity_type": "fixture_terminal",
                         "entity_id": "FIX1", "port_id": "T1"},
                "to": {"entity_type": "dut_testpoint",
                       "entity_id": "DUT1", "port_id": "TP1"},
                "signal_type": "power",
            },
            {
                "id": "LGND",
                "from": {"entity_type": "instrument_channel",
                         "entity_id": "PSU_MAIN", "port_id": "CH1"},
                "to": {"entity_type": "fixture_terminal",
                       "entity_id": "FIX1", "port_id": "TGND"},
                "signal_type": "ground",
            },
        ],
        "routes": [
            {"id": "RT1", "name": "电源路径", "links": ["L1", "L2"], "relays": ["R1"]},
        ],
    }


class TestCreate:
    @pytest.mark.asyncio
    async def test_create(self, client):
        resp = await client.post("/api/v1/fixtures", json={
            "name": "产测工装",
            "product_model": "comm_module_v2",
            "topology_data": _valid_topo_dict(),
            "tags": ["psu", "v2"],
        })
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["name"] == "产测工装"
        assert data["version"] == "1.0"
        assert data["product_model"] == "comm_module_v2"
        assert data["tags"] == ["psu", "v2"]
        assert "id" in data

    @pytest.mark.asyncio
    async def test_create_invalid_topology_data(self, client):
        bad = _valid_topo_dict()
        bad["instruments"] = [{"id": "X", "type": "bogus_type"}]  # 无效仪器类型
        resp = await client.post("/api/v1/fixtures", json={
            "name": "坏拓扑",
            "topology_data": bad,
        })
        assert resp.status_code == 422, resp.text

    @pytest.mark.asyncio
    async def test_create_missing_topology_data(self, client):
        resp = await client.post("/api/v1/fixtures", json={"name": "无数据"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_duplicate_name_version(self, client):
        payload = {"name": "重名", "topology_data": _valid_topo_dict()}
        assert (await client.post("/api/v1/fixtures", json=payload)).status_code == 201
        resp = await client.post("/api/v1/fixtures", json=payload)
        assert resp.status_code == 409, resp.text


class TestListGet:
    @pytest.mark.asyncio
    async def test_list(self, client):
        await client.post("/api/v1/fixtures", json={
            "name": "A", "topology_data": _valid_topo_dict(),
        })
        await client.post("/api/v1/fixtures", json={
            "name": "B", "product_model": "other",
            "topology_data": _valid_topo_dict(),
        })
        resp = await client.get("/api/v1/fixtures")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    @pytest.mark.asyncio
    async def test_list_filter_product_model(self, client):
        await client.post("/api/v1/fixtures", json={
            "name": "A", "product_model": "comm_module_v2",
            "topology_data": _valid_topo_dict(),
        })
        await client.post("/api/v1/fixtures", json={
            "name": "B", "product_model": "other",
            "topology_data": _valid_topo_dict(),
        })
        resp = await client.get("/api/v1/fixtures?product_model=comm_module_v2")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "A"

    @pytest.mark.asyncio
    async def test_get_detail(self, client):
        created = (await client.post("/api/v1/fixtures", json={
            "name": "详情", "topology_data": _valid_topo_dict(),
        })).json()
        resp = await client.get(f"/api/v1/fixtures/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "详情"
        assert resp.json()["topology_data"]["instruments"][0]["id"] == "PSU_MAIN"

    @pytest.mark.asyncio
    async def test_get_not_found(self, client):
        resp = await client.get("/api/v1/fixtures/no-such-id")
        assert resp.status_code == 404


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_auto_version_bump(self, client):
        created = (await client.post("/api/v1/fixtures", json={
            "name": "版本", "topology_data": _valid_topo_dict(),
        })).json()
        assert created["version"] == "1.0"

        resp = await client.put(f"/api/v1/fixtures/{created['id']}", json={
            "description": "第二轮",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["version"] == "1.1"
        assert resp.json()["description"] == "第二轮"

    @pytest.mark.asyncio
    async def test_update_explicit_version(self, client):
        created = (await client.post("/api/v1/fixtures", json={
            "name": "显式版本", "topology_data": _valid_topo_dict(),
        })).json()
        resp = await client.put(f"/api/v1/fixtures/{created['id']}", json={
            "version": "2.0",
        })
        assert resp.json()["version"] == "2.0"

    @pytest.mark.asyncio
    async def test_update_not_found(self, client):
        resp = await client.put("/api/v1/fixtures/nope", json={"name": "X"})
        assert resp.status_code == 404


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete(self, client):
        created = (await client.post("/api/v1/fixtures", json={
            "name": "删除", "topology_data": _valid_topo_dict(),
        })).json()
        resp = await client.delete(f"/api/v1/fixtures/{created['id']}")
        assert resp.status_code == 204
        assert (await client.get(f"/api/v1/fixtures/{created['id']}")).status_code == 404

    @pytest.mark.asyncio
    async def test_delete_not_found(self, client):
        resp = await client.delete("/api/v1/fixtures/nope")
        assert resp.status_code == 404


class TestValidate:
    @pytest.mark.asyncio
    async def test_validate_valid(self, client):
        created = (await client.post("/api/v1/fixtures", json={
            "name": "合法", "topology_data": _valid_topo_dict(),
        })).json()
        resp = await client.post(f"/api/v1/fixtures/{created['id']}/validate")
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["errors"] == []

    @pytest.mark.asyncio
    async def test_validate_invalid(self, client):
        bad = _valid_topo_dict()
        # 电源通道方向改为输入 -> 方向检查 error
        bad["instruments"][0]["channels"][0]["direction"] = "input"
        created = (await client.post("/api/v1/fixtures", json={
            "name": "非法", "topology_data": bad,
        })).json()
        resp = await client.post(f"/api/v1/fixtures/{created['id']}/validate")
        data = resp.json()
        assert data["valid"] is False
        assert any(e["code"] == "direction" for e in data["errors"])
        assert "summary" in data

    @pytest.mark.asyncio
    async def test_validate_not_found(self, client):
        resp = await client.post("/api/v1/fixtures/nope/validate")
        assert resp.status_code == 404


class TestDuplicate:
    @pytest.mark.asyncio
    async def test_duplicate(self, client):
        created = (await client.post("/api/v1/fixtures", json={
            "name": "原件", "topology_data": _valid_topo_dict(),
        })).json()
        resp = await client.post(f"/api/v1/fixtures/{created['id']}/duplicate")
        assert resp.status_code == 201, resp.text
        dup = resp.json()
        assert dup["id"] != created["id"]
        assert dup["name"] == "原件（副本）"
        assert dup["version"] == "1.0"

    @pytest.mark.asyncio
    async def test_duplicate_not_found(self, client):
        resp = await client.post("/api/v1/fixtures/nope/duplicate")
        assert resp.status_code == 404


class TestVersions:
    @pytest.mark.asyncio
    async def test_versions_history(self, client):
        created = (await client.post("/api/v1/fixtures", json={
            "name": "历史", "topology_data": _valid_topo_dict(),
        })).json()
        await client.put(f"/api/v1/fixtures/{created['id']}", json={"description": "v2"})
        await client.put(f"/api/v1/fixtures/{created['id']}", json={"description": "v3"})

        resp = await client.get(f"/api/v1/fixtures/{created['id']}/versions")
        assert resp.status_code == 200
        versions = resp.json()
        # 创建 + 2 次更新 = 3 个版本快照
        assert len(versions) == 3
        assert sorted(v["version"] for v in versions) == ["1.0", "1.1", "1.2"]
        # 拓扑主记录版本为最新
        detail = (await client.get(f"/api/v1/fixtures/{created['id']}")).json()
        assert detail["version"] == "1.2"


class TestExport:
    @pytest.mark.asyncio
    async def test_export_json(self, client):
        created = (await client.post("/api/v1/fixtures", json={
            "name": "导出", "topology_data": _valid_topo_dict(),
        })).json()
        resp = await client.post(f"/api/v1/fixtures/{created['id']}/export", json={})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["format"] == "json"
        assert '"PSU_MAIN"' in data["content"]

    @pytest.mark.asyncio
    async def test_export_yaml(self, client):
        created = (await client.post("/api/v1/fixtures", json={
            "name": "导出YAML", "topology_data": _valid_topo_dict(),
        })).json()
        resp = await client.post(
            f"/api/v1/fixtures/{created['id']}/export",
            params={"format": "yaml"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["format"] == "yaml"
        assert "PSU_MAIN" in data["content"]

    @pytest.mark.asyncio
    async def test_export_version_snapshot(self, client):
        created = (await client.post("/api/v1/fixtures", json={
            "name": "快照导出", "topology_data": _valid_topo_dict(),
        })).json()
        resp = await client.post(
            f"/api/v1/fixtures/{created['id']}/export",
            params={"version": "1.0"},
        )
        assert resp.status_code == 200
        assert resp.json()["format"] == "json"


class TestDeviceTemplates:
    @pytest.mark.asyncio
    async def test_create_template(self, client):
        resp = await client.post("/api/v1/fixtures/templates", json={
            "category": "instrument",
            "type": "psu",
            "model": "Chroma 62012P",
            "manufacturer": "Chroma",
            "spec_data": {"channels": ["CH1", "CH2"]},
            "icon": "psu",
        })
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["category"] == "instrument"
        assert data["spec_data"]["channels"] == ["CH1", "CH2"]

    @pytest.mark.asyncio
    async def test_list_templates_filter(self, client):
        await client.post("/api/v1/fixtures/templates", json={
            "category": "instrument", "type": "psu", "model": "M1",
        })
        await client.post("/api/v1/fixtures/templates", json={
            "category": "fixture", "type": "jig", "model": "M2",
        })
        resp = await client.get("/api/v1/fixtures/templates?category=instrument")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["category"] == "instrument"
