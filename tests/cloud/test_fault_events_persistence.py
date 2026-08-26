"""Tests for FaultEvent persistence (RH-1, v41-remaining-hardening #1).

设计文档 §8.3 历史故障热力图持久化落地：

覆盖：
- 写入路径：POST /executions/{run_id}/fault-injection（source=link）与
  POST /executions/{run_id}/manual-fault（source=manual）在 NATS 发布之后
  落一行 fault_events；NATS 不可达仍落库（API 容忍 outage 的对称语义）
- DB 写失败不阻断主流程：commit 抛异常 → 请求仍 200 + warning 日志
- GET /fixtures/{fixture_id}/fault-stats 改为读表聚合 count/last_seen
  （按工装拓扑声明的 link_id 归因）；表空仍诚实空 {links: {}}
- 404（未知 fixture / 未知 run）/ 401（匿名）
- Alembic 迁移可升级：临时 SQLite 上 alembic upgrade head 建
  fault_events 表 + link_id 与 (fixture_id, created_at) 索引
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.api.v1.fixtures import aggregate_fault_events
from ate_cloud.config import settings
from ate_cloud.models.execution import Execution


async def _insert_execution(
    db_session: AsyncSession,
    run_id: str,
    status: str = "RUNNING",
) -> Execution:
    """Insert an Execution record for testing."""
    execution = Execution(
        id=run_id,
        sequence_id="seq-test",
        status=status,
    )
    db_session.add(execution)
    await db_session.flush()
    return execution


def _make_mock_nc() -> MagicMock:
    """Build a mock NATS client with async publish."""
    mock_nc = MagicMock()
    mock_nc.publish = AsyncMock()
    return mock_nc


def _valid_topo_dict(name: str = "rh1-fixture") -> dict[str, object]:
    """合法双链路拓扑（PSU--夹具--DUT，L1/L2），与 test_fixtures.py 同构。"""
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
            {
                "id": "L2",
                "from": {"entity_type": "fixture_terminal",
                         "entity_id": "FIX1", "port_id": "T1"},
                "to": {"entity_type": "dut_testpoint",
                       "entity_id": "DUT1", "port_id": "TP1"},
                "signal_type": "power",
            },
        ],
        "routes": [],
    }


async def _create_fixture(client, name: str = "RH1热力图工装") -> str:
    """创建一个合法工装拓扑，返回其 id。"""
    resp = await client.post(
        "/api/v1/fixtures",
        json={"name": name, "topology_data": _valid_topo_dict()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _post_inject(client, run_id: str, body: dict):
    """POST the fault-injection endpoint with get_nats patched."""
    mock_nc = _make_mock_nc()
    with patch("ate_cloud.main.get_nats", return_value=mock_nc):
        response = await client.post(
            f"/api/v1/executions/{run_id}/fault-injection", json=body
        )
    return response, mock_nc


async def _post_manual(client, run_id: str, body: dict):
    """POST the manual-fault endpoint with get_nats patched."""
    mock_nc = _make_mock_nc()
    with patch("ate_cloud.main.get_nats", return_value=mock_nc):
        response = await client.post(
            f"/api/v1/executions/{run_id}/manual-fault", json=body
        )
    return response, mock_nc


class TestWriteOnInject:
    """T44 fault-injection 落库（source='link'）。"""

    @pytest.mark.asyncio
    async def test_inject_persists_fault_event_row(self, db_session, client) -> None:
        """Successful injection writes one fault_events row after publish."""
        from ate_cloud.models.fault_event import FaultEvent

        await _insert_execution(db_session, "run-fe-inj")
        resp, mock_nc = await _post_inject(
            client,
            "run-fe-inj",
            {"link_id": "L1", "fault_type": "open_circuit"},
        )
        assert resp.status_code == 200
        mock_nc.publish.assert_awaited_once()

        rows = (await db_session.execute(select(FaultEvent))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.run_id == "run-fe-inj"
        assert row.link_id == "L1"
        assert row.fault_type == "open_circuit"
        assert row.source == "link"
        assert row.detail["fault_id"] == "link-L1-open_circuit"

    @pytest.mark.asyncio
    async def test_inject_persists_even_when_nats_down(self, db_session, client) -> None:
        """NATS outage is non-fatal AND the event is still persisted."""
        from ate_cloud.models.fault_event import FaultEvent

        await _insert_execution(db_session, "run-fe-natsdown")
        with patch(
            "ate_cloud.main.get_nats", side_effect=RuntimeError("no nats")
        ):
            resp = await client.post(
                "/api/v1/executions/run-fe-natsdown/fault-injection",
                json={"link_id": "L2", "fault_type": "noise"},
            )
        assert resp.status_code == 200

        rows = (
            await db_session.execute(
                select(FaultEvent).where(FaultEvent.run_id == "run-fe-natsdown")
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].link_id == "L2"

    @pytest.mark.asyncio
    async def test_inject_unknown_run_404_writes_nothing(self, db_session, client) -> None:
        """Unknown run → 404 and no fault_events row."""
        from ate_cloud.models.fault_event import FaultEvent

        resp, _ = await _post_inject(
            client,
            "run-fe-missing",
            {"link_id": "L1", "fault_type": "open_circuit"},
        )
        assert resp.status_code == 404
        rows = (await db_session.execute(select(FaultEvent))).scalars().all()
        assert rows == []


class TestWriteOnManualFault:
    """T38 manual-fault 落库（source='manual'）。"""

    @pytest.mark.asyncio
    async def test_manual_link_scope_persists_row(self, db_session, client) -> None:
        """scope=link stores target as link_id with source='manual'."""
        from ate_cloud.models.fault_event import FaultEvent

        await _insert_execution(db_session, "run-fe-manual")
        resp, _ = await _post_manual(
            client,
            "run-fe-manual",
            {"scope": "link", "target_id": "L1", "fault_type": "short_circuit"},
        )
        assert resp.status_code == 200

        rows = (await db_session.execute(select(FaultEvent))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.source == "manual"
        assert row.link_id == "L1"
        assert row.fault_type == "short_circuit"
        assert row.run_id == "run-fe-manual"

    @pytest.mark.asyncio
    async def test_manual_non_link_scope_stores_empty_link_id(
        self, db_session, client,
    ) -> None:
        """Non-link scopes persist the event but never fabricate a link id."""
        from ate_cloud.models.fault_event import FaultEvent

        await _insert_execution(db_session, "run-fe-inst")
        resp, _ = await _post_manual(
            client,
            "run-fe-inst",
            {
                "scope": "instrument",
                "target_id": "dmm-1",
                "fault_type": "over_voltage",
            },
        )
        assert resp.status_code == 200

        rows = (await db_session.execute(select(FaultEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].link_id == ""  # not a link — heatmap must skip it
        assert rows[0].detail["target_id"] == "dmm-1"
        assert rows[0].detail["layer"] == "instrument"


class TestNonBlockingDbFailure:
    """DB 写失败绝不阻断注入主流程（log warning + 200）。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/fault-injection", {"link_id": "L1", "fault_type": "open_circuit"}),
            (
                "/manual-fault",
                {"scope": "link", "target_id": "L1", "fault_type": "noise"},
            ),
        ],
    )
    async def test_db_failure_returns_200_with_warning(
        self,
        db_session,
        client,
        caplog: pytest.LogCaptureFixture,
        path: str,
        body: dict,
    ) -> None:
        """commit raising must not fail the request; a warning is logged."""
        run_id = f"run-fe-dbfail-{path.strip('/')}"
        await _insert_execution(db_session, run_id)

        async def _boom() -> None:
            raise RuntimeError("db down")

        monkey_target = type(db_session)
        original_commit = monkey_target.commit
        monkey_target.commit = _boom  # type: ignore[method-assign]
        try:
            with caplog.at_level(logging.WARNING, logger="ate_cloud.api.v1.executions"):
                if path == "/fault-injection":
                    resp, _ = await _post_inject(client, run_id, body)
                else:
                    resp, _ = await _post_manual(client, run_id, body)
        finally:
            monkey_target.commit = original_commit  # type: ignore[method-assign]

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert any("fault event" in rec.message.lower() for rec in caplog.records)


class TestFaultStatsAggregation:
    """fault-stats 改为读表聚合；表空保持诚实空契约。"""

    @pytest.mark.asyncio
    async def test_stats_counts_and_last_seen_from_table(
        self, db_session, client,
    ) -> None:
        """Inject L1×2 + L2×1 → stats {L1:{count:2}, L2:{count:1}} + last_seen."""
        from ate_cloud.models.fault_event import FaultEvent

        fid = await _create_fixture(client)
        await _insert_execution(db_session, "run-fe-stats")

        for link_id, fault_type in (
            ("L1", "open_circuit"),
            ("L1", "short_circuit"),
            ("L2", "noise"),
        ):
            resp, _ = await _post_inject(
                client, "run-fe-stats", {"link_id": link_id, "fault_type": fault_type}
            )
            assert resp.status_code == 200

        resp = await client.get(f"/api/v1/fixtures/{fid}/fault-stats")
        assert resp.status_code == 200
        links = resp.json()["links"]

        assert links["L1"]["count"] == 2
        assert links["L2"]["count"] == 1

        # last_seen == max(created_at) of that link's rows (ISO string compare).
        l1_rows = (
            await db_session.execute(
                select(FaultEvent).where(FaultEvent.link_id == "L1")
            )
        ).scalars().all()
        expected_last_seen = max(r.created_at.isoformat() for r in l1_rows)
        assert links["L1"]["last_seen"] == expected_last_seen

    @pytest.mark.asyncio
    async def test_stats_honest_empty_when_table_empty(self, client) -> None:
        """No events → 200 + links == {} (honest-empty contract preserved)."""
        fid = await _create_fixture(client, "RH1空表工装")
        resp = await client.get(f"/api/v1/fixtures/{fid}/fault-stats")
        assert resp.status_code == 200
        assert resp.json()["links"] == {}

    @pytest.mark.asyncio
    async def test_stats_response_shape_unchanged(self, client) -> None:
        """Response keeps exactly {links, generated_at} keys."""
        fid = await _create_fixture(client, "RH1形状工装")
        resp = await client.get(f"/api/v1/fixtures/{fid}/fault-stats")
        data = resp.json()
        assert set(data.keys()) == {"links", "generated_at"}

    @pytest.mark.asyncio
    async def test_stats_unknown_fixture_404(self, client) -> None:
        resp = await client.get("/api/v1/fixtures/no-such-fixture/fault-stats")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Fixture topology not found"

    @pytest.mark.asyncio
    async def test_stats_anonymous_401(
        self, client, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Anonymous request (dev_mode off) rejected with 401."""
        monkeypatch.setattr(settings, "dev_mode", False)
        resp = await client.get("/api/v1/fixtures/some-id/fault-stats")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_aggregate_contract_survives_swap(self) -> None:
        """aggregate_fault_events 纯函数契约不被持久化交换破坏。"""
        events = [
            {"link_id": "L1", "detected_at": "2026-08-01T10:00:00+00:00"},
            {"link_id": "L1", "detected_at": "2026-08-03T12:00:00+00:00"},
            {"link_id": "L2", "detected_at": "2026-08-02T08:00:00+00:00"},
        ]
        agg = aggregate_fault_events(events)
        assert agg["L1"]["count"] == 2
        assert agg["L1"]["last_seen"] == "2026-08-03T12:00:00+00:00"
        assert agg["L2"] == {"count": 1, "last_seen": "2026-08-02T08:00:00+00:00"}


class TestFaultEventDetailPayload:
    """detail JSON 列往返。"""

    @pytest.mark.asyncio
    async def test_detail_params_roundtrip(self, db_session, client) -> None:
        """params ride along in the detail JSON column."""
        from ate_cloud.models.fault_event import FaultEvent

        await _insert_execution(db_session, "run-fe-detail")
        resp, _ = await _post_inject(
            client,
            "run-fe-detail",
            {"link_id": "L1", "fault_type": "contact_resistance",
             "params": {"ohms": 4.2}},
        )
        assert resp.status_code == 200

        row = (await db_session.execute(select(FaultEvent))).scalars().one()
        assert row.detail["params"] == {"ohms": 4.2}
        assert row.created_at is not None


class TestMigrationUpgradeHead:
    """Alembic 迁移可升级：临时库 upgrade head 建表 + 索引。"""

    def test_upgrade_head_creates_fault_events_table_and_indexes(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import sqlite3

        db_file = tmp_path / "rh1_mig.db"
        url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

        # env.py imports settings from src.ate_cloud.config (a distinct module
        # object from ate_cloud.config) — swap that view's settings wholesale
        # (pydantic v2 forbids instance setattr of non-field attrs).
        import sys
        from types import SimpleNamespace

        root = str(Path(__file__).resolve().parents[2])
        if root not in sys.path:
            sys.path.insert(0, root)
        import src.ate_cloud.config as src_cfg  # noqa: E402 — needs sys.path first

        monkeypatch.setattr(
            src_cfg, "settings", SimpleNamespace(get_database_url=lambda: url)
        )

        # env.py calls fileConfig(alembic.ini), which GLOBALLY disables
        # existing loggers (fileConfig default) and breaks caplog assertions
        # in subsequently-run test files. No-op it for this migration run.
        import logging.config

        from alembic.config import Config

        from alembic import command

        monkeypatch.setattr(logging.config, "fileConfig", lambda *a, **k: None)
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "head")

        conn = sqlite3.connect(db_file)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "fault_events" in tables

            indexes = {
                r[1] for r in conn.execute("PRAGMA index_list('fault_events')")
            }
            assert "ix_fault_events_link_id" in indexes
            assert "ix_fault_events_fixture_id_created_at" in indexes
        finally:
            conn.close()
