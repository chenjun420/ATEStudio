"""T41 (v41-gap-analysis #41) — GET /api/v1/executions/{run_id}/simulation-report.

Covers the consolidated end-of-run report endpoint composing three
already-computed sources (no backend recomputation beyond assembly):

- coverage — ``SimulationCoverage.report()`` (T14) over the materialized
  plan's compiled DAG + the run's recorded step events; degrades to an
  unavailable section when the sequence can't be materialized/compiled or
  no recording exists
- contention — ``ResourceContentionAnalyzer.analyze()`` (T13) fed the
  ``lock_wait``/``lock_acquire``/``lock_release`` events from the run's
  JSONL recording, when present; empty section otherwise
- faults — the execution's recorded fault records
  (``Execution.result["faults"]``), normalized for display

Recordings follow the T10 finalize convention
``<recordings_dir>/<run_id>.jsonl`` (env ``ATE_RECORDINGS_DIR``).
"""

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.config import settings
from ate_cloud.models.execution import Execution
from ate_cloud.models.sequence import Sequence
from ate_platform.simulation.recording import RecordingInterceptor

# Flat two-step plan: compiled ids == source ids (no loop expansion).
_YAML = """\
name: report-plan
version: "1.0"
steps:
  - id: step-1
    script: scripts/a.py
  - id: step-2
    script: scripts/b.py
"""


async def _insert_plan(
    db_session: AsyncSession,
    run_id: str,
    *,
    sequence_id: str = "seq-rep",
    status: str = "COMPLETED",
    result: dict | None = None,
) -> None:
    """Insert a Sequence + Execution row so materialize/run checks pass."""
    db_session.add(
        Sequence(id=sequence_id, name=f"seq-{sequence_id}", yaml_content=_YAML)
    )
    db_session.add(
        Execution(id=run_id, sequence_id=sequence_id, status=status, result=result)
    )
    await db_session.flush()


def _write_recording(
    tmp_path: Path,
    run_id: str,
    *,
    with_steps: bool = True,
    with_locks: bool = True,
    deadlock: bool = False,
) -> None:
    """Write a deterministic JSONL recording for ``run_id``.

    Uses an injected synthetic clock so lock-event ``t`` values are strictly
    increasing regardless of platform timer granularity.
    """
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 0.01
        return state["t"]

    rec = RecordingInterceptor(
        tmp_path / f"{run_id}.jsonl", execution_id=run_id, clock=clock
    )
    if with_steps:
        rec.record_step_started("step-1")
        rec.record_step_completed("step-1")
        rec.record_step_started("step-2")
        rec.record_step_completed("step-2")
    if deadlock:
        # A→B→A wait-for cycle: A holds R1 wants R2, B holds R2 wants R1.
        rec.record("lock_acquire", resource="R1", owner="uut-a")
        rec.record("lock_acquire", resource="R2", owner="uut-b")
        rec.record("lock_wait", resource="R2", owner="uut-a")
        rec.record("lock_wait", resource="R1", owner="uut-b")
    elif with_locks:
        # Benign contention: uut-b waits once for R1 held by uut-a.
        rec.record("lock_acquire", resource="R1", owner="uut-a")
        rec.record("lock_release", resource="R1", owner="uut-a")
        rec.record("lock_wait", resource="R1", owner="uut-b")
        rec.record("lock_acquire", resource="R1", owner="uut-b")
        rec.record("lock_release", resource="R1", owner="uut-b")
    rec.finalize()


@pytest.fixture
def recordings_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point settings.recordings_dir at a per-test temp directory."""
    monkeypatch.setattr(settings, "recordings_dir", str(tmp_path))
    return tmp_path


class TestSimulationReportEndpoint:
    async def test_report_full_shape_happy(
        self,
        db_session: AsyncSession,
        client: AsyncClient,
        recordings_dir: Path,
    ) -> None:
        """Populated run → all three sections available with expected shapes."""
        await _insert_plan(
            db_session,
            "run-happy",
            result={"faults": [{"fault_id": "f-1", "fault_type": "open_circuit"}]},
        )
        _write_recording(recordings_dir, "run-happy")

        response = await client.get("/api/v1/executions/run-happy/simulation-report")

        assert response.status_code == 200
        body = response.json()
        assert body["run_id"] == "run-happy"
        assert body["run_status"] == "COMPLETED"
        assert isinstance(body["generated_at"], str) and body["generated_at"]
        assert body["warnings"] == []

        cov = body["coverage"]
        assert cov["available"] is True and cov["reason"] is None
        assert cov["report"]["plan"]["total_steps"] == 2
        assert cov["report"]["summary"]["step_percent"] == 100.0

        cont = body["contention"]
        assert cont["available"] is True and cont["reason"] is None
        assert cont["report"]["resources"]["R1"]["contention_count"] == 1
        assert cont["report"]["gantt"]

        faults = body["faults"]
        assert faults["total"] == 1
        assert faults["records"][0]["type"] == "open_circuit"

    async def test_report_unknown_run_404(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Unknown run_id → 404."""
        response = await client.get(
            "/api/v1/executions/run-ghost/simulation-report"
        )
        assert response.status_code == 404
        assert "run-ghost" in response.json()["detail"]

    async def test_report_requires_auth(self, client: AsyncClient) -> None:
        """Garbage bearer token → 401 (mount-level JWT enforcement)."""
        response = await client.get(
            "/api/v1/executions/run-a/simulation-report",
            headers={"Authorization": "Bearer not.a.real.token"},
        )
        assert response.status_code == 401

    async def test_report_missing_recording_degrades_sections(
        self,
        db_session: AsyncSession,
        client: AsyncClient,
        recordings_dir: Path,
    ) -> None:
        """No JSONL on disk → coverage+contention unavailable, faults intact."""
        await _insert_plan(db_session, "run-norec")

        response = await client.get(
            "/api/v1/executions/run-norec/simulation-report"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["coverage"]["available"] is False
        assert body["coverage"]["report"] is None
        assert body["contention"]["available"] is False
        assert body["contention"]["report"] is None
        assert len(body["warnings"]) == 2
        assert body["faults"] == {"records": [], "total": 0}

    async def test_report_recording_without_lock_events_degrades_contention_only(
        self,
        db_session: AsyncSession,
        client: AsyncClient,
        recordings_dir: Path,
    ) -> None:
        """Steps-only recording → coverage available, contention degraded."""
        await _insert_plan(db_session, "run-steps")
        _write_recording(recordings_dir, "run-steps", with_locks=False)

        response = await client.get(
            "/api/v1/executions/run-steps/simulation-report"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["coverage"]["available"] is True
        assert body["coverage"]["report"]["summary"]["step_percent"] == 100.0
        assert body["contention"]["available"] is False
        assert "lock" in body["contention"]["reason"]
        assert len(body["warnings"]) == 1

    async def test_report_unmaterializable_sequence_degrades_coverage(
        self,
        db_session: AsyncSession,
        client: AsyncClient,
        recordings_dir: Path,
    ) -> None:
        """Execution pointing at a missing sequence → coverage degraded,
        contention still computed from the recording."""
        db_session.add(
            Execution(id="run-orphan", sequence_id="seq-missing", status="COMPLETED")
        )
        await db_session.flush()
        _write_recording(recordings_dir, "run-orphan")

        response = await client.get(
            "/api/v1/executions/run-orphan/simulation-report"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["coverage"]["available"] is False
        assert body["coverage"]["reason"]
        assert body["contention"]["available"] is True

    async def test_report_contention_deadlock_alerts(
        self,
        db_session: AsyncSession,
        client: AsyncClient,
        recordings_dir: Path,
    ) -> None:
        """Wait-for cycle in lock events → deadlocks surfaced for alert UI."""
        await _insert_plan(db_session, "run-dead")
        _write_recording(recordings_dir, "run-dead", deadlock=True)

        response = await client.get(
            "/api/v1/executions/run-dead/simulation-report"
        )

        assert response.status_code == 200
        report = response.json()["contention"]["report"]
        assert len(report["deadlocks"]) >= 1
        cycle = report["deadlocks"][0]
        assert set(cycle["cycle_owners"]) == {"uut-a", "uut-b"}
        assert set(cycle["involved_resources"]) == {"R1", "R2"}

    async def test_report_fault_records_normalized_and_empty_default(
        self,
        db_session: AsyncSession,
        client: AsyncClient,
        recordings_dir: Path,
    ) -> None:
        """result=None → empty fault section; alias fields get canonical keys."""
        await _insert_plan(db_session, "run-faults-none", result=None, sequence_id="seq-fn")
        await _insert_plan(
            db_session,
            "run-faults",
            result={
                "faults": [
                    {
                        "id": "fx-9",
                        "fault_type": "short_circuit",
                        "level": "critical",
                        "link_id": "L3",
                    },
                    "malformed-entry",
                ]
            },
            sequence_id="seq-f2",
        )

        none_resp = await client.get(
            "/api/v1/executions/run-faults-none/simulation-report"
        )
        assert none_resp.status_code == 200
        assert none_resp.json()["faults"] == {"records": [], "total": 0}

        resp = await client.get(
            "/api/v1/executions/run-faults/simulation-report"
        )
        assert resp.status_code == 200
        records = resp.json()["faults"]["records"]
        assert resp.json()["faults"]["total"] == 2
        first = records[0]
        assert first["fault_id"] == "fx-9"
        assert first["type"] == "short_circuit"
        assert first["severity"] == "critical"
        assert first["target"] == "L3"
        # Non-mapping entries degrade to a detail record instead of crashing.
        assert records[1]["detail"] == "malformed-entry"

    async def test_report_aborted_run_partial_report(
        self,
        db_session: AsyncSession,
        client: AsyncClient,
        recordings_dir: Path,
    ) -> None:
        """Aborted run still renders a partial report with warnings banner."""
        await _insert_plan(db_session, "run-abort", status="ABORTED")
        _write_recording(recordings_dir, "run-abort", with_locks=False)

        response = await client.get(
            "/api/v1/executions/run-abort/simulation-report"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["run_status"] == "ABORTED"
        assert body["coverage"]["available"] is True
        assert body["contention"]["available"] is False
        assert body["warnings"]
