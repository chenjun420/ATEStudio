"""T37 (v41-gap-analysis #37) — GET /api/v1/executions/{run_id}/diff tests.

Covers the ExecutionDiff.compare() passthrough endpoint added to the
executions router:

- happy path: diff shape passthrough (match/meta/steps/measurements/timing/
  resources/variables) with a perturbed candidate run flagging violations
- identical runs → match=true, every section empty
- 404 unknown run (candidate side), 404 unknown baseline
- 404 when either recording JSONL is missing on disk
- auth-401 with a garbage bearer token (mount-level JWT, T17)

Recordings follow the T10 RecordingInterceptor finalize convention:
``<recordings_dir>/<run_id>.jsonl`` where ``recordings_dir`` comes from
``settings.recordings_dir`` (env ``ATE_RECORDINGS_DIR``).
"""

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.config import settings
from ate_cloud.models.execution import Execution
from ate_platform.simulation.recording import RecordingInterceptor


async def _insert_execution(db_session: AsyncSession, run_id: str) -> None:
    """Insert a minimal Execution row so run-existence checks pass."""
    db_session.add(Execution(id=run_id, sequence_id="seq-1", status="COMPLETED"))
    await db_session.flush()


def _write_recording(
    tmp_path: Path,
    run_id: str,
    *,
    fail_s2: bool = False,
    voltage: float = 3.3,
    tick: float = 0.01,
) -> None:
    """Write a small deterministic JSONL recording for ``run_id``.

    Uses an injected synthetic clock (``tick`` seconds per event) so timing
    spans are reproducible regardless of platform timer granularity.
    """
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += tick
        return state["t"]

    rec = RecordingInterceptor(
        tmp_path / f"{run_id}.jsonl", execution_id=run_id, clock=clock
    )
    rec.record_step_started("s1")
    rec.record_step_completed("s1")
    rec.record_instrument_call("DMM1", "read", result=voltage)
    rec.record_step_started("s2")
    if fail_s2:
        rec.record_step_failed("s2", error="open circuit")
    else:
        rec.record_step_completed("s2")
    rec.finalize()


@pytest.fixture
def recordings_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point settings.recordings_dir at a per-test temp directory."""
    monkeypatch.setattr(settings, "recordings_dir", str(tmp_path))
    return tmp_path


class TestExecutionDiffEndpoint:
    async def test_diff_happy_shape_passthrough(
        self,
        db_session: AsyncSession,
        client: AsyncClient,
        recordings_dir: Path,
    ) -> None:
        """Perturbed candidate flags step-status + measurement violations."""
        await _insert_execution(db_session, "run-base")
        await _insert_execution(db_session, "run-cand")
        _write_recording(recordings_dir, "run-base", fail_s2=False, voltage=3.3)
        # Different tick → different total span → timing.total must surface.
        _write_recording(recordings_dir, "run-cand", fail_s2=True, voltage=4.9, tick=0.02)

        response = await client.get(
            "/api/v1/executions/run-cand/diff?baseline=run-base"
        )

        assert response.status_code == 200
        body = response.json()
        # Passthrough of the T12 summary schema + run identity envelope.
        assert body["run_id"] == "run-cand"
        assert body["baseline"] == "run-base"
        assert body["match"] is False
        assert set(body["meta"]) == {"events_a", "events_b"}
        assert body["meta"]["events_a"] == body["meta"]["events_b"]
        assert {"steps", "measurements", "timing", "resources", "variables"} <= set(body)
        assert body["steps"]["status_changed"] == [
            {"step_id": "s2", "a": "passed", "b": "failed"}
        ]
        meas = body["measurements"][0]
        assert meas["key"] == "DMM1.read#0"
        assert meas["a"] == 3.3 and meas["b"] == 4.9
        assert body["timing"]["total"] is not None

    async def test_diff_identical_runs_all_match(
        self,
        db_session: AsyncSession,
        client: AsyncClient,
        recordings_dir: Path,
    ) -> None:
        """Identical event streams render an all-green (match=true) summary."""
        await _insert_execution(db_session, "run-a")
        await _insert_execution(db_session, "run-b")
        _write_recording(recordings_dir, "run-a")
        _write_recording(recordings_dir, "run-b")

        response = await client.get("/api/v1/executions/run-b/diff?baseline=run-a")

        assert response.status_code == 200
        body = response.json()
        assert body["match"] is True
        assert body["steps"] == {"added": [], "removed": [], "status_changed": []}
        assert body["measurements"] == []
        assert body["timing"]["total"] is None
        assert body["timing"]["steps"] == []
        assert body["resources"] == []
        assert body["variables"]["changed"] == []

    async def test_diff_unknown_run_404(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Unknown candidate run_id → 404."""
        await _insert_execution(db_session, "run-base")

        response = await client.get(
            "/api/v1/executions/run-ghost/diff?baseline=run-base"
        )

        assert response.status_code == 404
        assert "run-ghost" in response.json()["detail"]

    async def test_diff_unknown_baseline_404(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Unknown baseline run_id → 404."""
        await _insert_execution(db_session, "run-cand")

        response = await client.get(
            "/api/v1/executions/run-cand/diff?baseline=run-ghost"
        )

        assert response.status_code == 404
        assert "run-ghost" in response.json()["detail"]

    async def test_diff_missing_recording_404(
        self,
        db_session: AsyncSession,
        client: AsyncClient,
        recordings_dir: Path,
    ) -> None:
        """Both runs exist in DB but no JSONL on disk → 404."""
        await _insert_execution(db_session, "run-x")
        await _insert_execution(db_session, "run-y")

        response = await client.get("/api/v1/executions/run-y/diff?baseline=run-x")

        assert response.status_code == 404
        assert "Recording not found" in response.json()["detail"]

    async def test_diff_requires_auth(self, client: AsyncClient) -> None:
        """Garbage bearer token → 401 (mount-level JWT enforcement)."""
        response = await client.get(
            "/api/v1/executions/run-a/diff?baseline=run-b",
            headers={"Authorization": "Bearer not.a.real.token"},
        )
        assert response.status_code == 401

    async def test_diff_self_compare_matches(
        self,
        db_session: AsyncSession,
        client: AsyncClient,
        recordings_dir: Path,
    ) -> None:
        """Comparing a run against itself yields match=true."""
        await _insert_execution(db_session, "run-self")
        _write_recording(recordings_dir, "run-self")

        response = await client.get(
            "/api/v1/executions/run-self/diff?baseline=run-self"
        )

        assert response.status_code == 200
        assert response.json()["match"] is True
