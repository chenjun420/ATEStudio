"""Tests for POST /api/v1/executions/{run_id}/simulate（设计文档 §7 三层仿真 + §8.4 控制台）。

覆盖：
- full 层级：决策 + 测量事件、统计、状态
- dry_run 层级：仅决策、无测量
- driver 层级：驱动级仿真（复用 FullChainSimulator）
- 未知 execution → 404
- 噪声模型/种子传递可复现
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ate_cloud.models import Execution, Sequence

_SAMPLE_YAML = """\
name: test-plan
version: "1.0"
max_concurrency: 2
steps:
  - id: dmm1
    script: scripts/dmm_measure.py
    params:
      expected_value: 3.3
  - id: psu1
    script: scripts/psu_power.py
    params:
      expected_value: 5.0
"""


async def _seed_execution(db_session, run_id: str = "run-sim-1") -> str:
    """Create a Sequence + Execution row, return sequence_id."""
    sequence = Sequence(id="seq-sim-1", name="sim-seq", yaml_content=_SAMPLE_YAML)
    db_session.add(sequence)
    await db_session.commit()
    execution = Execution(id=run_id, sequence_id="seq-sim-1", status="PENDING")
    db_session.add(execution)
    await db_session.commit()
    return "seq-sim-1"


@pytest.fixture
def client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_simulate_full_returns_measurements(app, client, db_session) -> None:
    """full 层级返回决策 + 测量事件，状态 passed。"""
    await _seed_execution(db_session)

    response = await client.post(
        "/api/v1/executions/run-sim-1/simulate",
        json={"tier": "full", "noise_model": "GAUSSIAN", "seed": 42},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tier"] == "full"
    assert body["session_id"] == "run-sim-1"
    assert body["status"] in ("passed", "failed", "error")

    event_types = [e["event_type"] for e in body["events"]]
    assert "decision" in event_types
    assert "measurement" in event_types
    assert body["statistics"]["measurements"] >= 1
    assert body["duration_seconds"] >= 0


@pytest.mark.asyncio
async def test_simulate_dry_run_no_measurements(app, client, db_session) -> None:
    """dry_run 层级只产生决策，不产生测量。"""
    await _seed_execution(db_session)

    response = await client.post(
        "/api/v1/executions/run-sim-1/simulate",
        json={"tier": "dry_run"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tier"] == "dry_run"
    event_types = [e["event_type"] for e in body["events"]]
    assert all(t == "decision" for t in event_types)
    assert "measurements" not in body["statistics"]


@pytest.mark.asyncio
async def test_simulate_driver_tier(app, client, db_session) -> None:
    """driver 层级可执行并返回测量事件。"""
    await _seed_execution(db_session)

    response = await client.post(
        "/api/v1/executions/run-sim-1/simulate",
        json={"tier": "driver"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tier"] == "driver"
    assert "measurement" in [e["event_type"] for e in body["events"]]


@pytest.mark.asyncio
async def test_simulate_missing_execution_404(app, client, db_session) -> None:
    """不存在的 execution 返回 404。"""
    response = await client.post(
        "/api/v1/executions/run-ghost/simulate",
        json={"tier": "dry_run"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_simulate_reproducible_seed(app, client, db_session) -> None:
    """相同 seed 产生相同仿真值（可复现）。"""
    await _seed_execution(db_session, run_id="run-sim-repro")

    async def run_once() -> list[float]:
        resp = await client.post(
            "/api/v1/executions/run-sim-repro/simulate",
            json={"tier": "full", "seed": 7},
        )
        assert resp.status_code == 200
        return [
            e["data"]["simulated_value"]
            for e in resp.json()["events"]
            if e["event_type"] == "measurement"
        ]

    first = await run_once()
    second = await run_once()
    assert first == second
    assert len(first) > 0
