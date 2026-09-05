"""Tests for task 10 knowledge-domain persistence models.

Covers the deterministic (non-LLM) relational layer for the ontology-driven
domain: TestRequirement -> TestCase (1-N), FMEA with server-computed RPN and
1-10 rating enforcement, and persisted Diagnosis linked to an execution run
(task 15 feedback link). Also an Alembic migration smoke test
(upgrade head -> downgrade -1 -> upgrade head on a temp SQLite file).
"""

from __future__ import annotations

import logging.config
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.execution import Execution

# ── TestRequirement / TestCase ──────────────────────────────────────────────


async def test_create_requirement_with_cases_persists_1_to_n(
    db_session: AsyncSession,
) -> None:
    """Given a requirement and two cases referencing it, the cases persist
    with requirement_id linkage (requirement 1-N test cases)."""
    from ate_cloud.models.knowledge import TestCase, TestRequirement

    req = TestRequirement(
        id="req-1",
        product_code="comm_module_v2",
        requirement_code="REQ-PSU-001",
        title="PSU output voltage within 5V +/-1%",
        source="dsl",
    )
    db_session.add(req)
    case_a = TestCase(
        id="case-1",
        requirement_id="req-1",
        case_code="TC-VOLT-001",
        title="Measure CH1 voltage under load",
        sequence_id="seq-1",
        step_id="step_measure_v",
    )
    case_b = TestCase(
        id="case-2",
        requirement_id=None,  # ingestion ordering: case before requirement link
        case_code="TC-VOLT-002",
        title="Measure CH1 ripple",
        status="draft",
    )
    db_session.add_all([case_a, case_b])
    await db_session.flush()

    cases = (
        (await db_session.execute(select(TestCase).order_by(TestCase.id)))
        .scalars()
        .all()
    )
    assert len(cases) == 2
    linked = [c for c in cases if c.requirement_id == "req-1"]
    assert len(linked) == 1
    assert linked[0].case_code == "TC-VOLT-001"
    assert linked[0].sequence_id == "seq-1"
    unlinked = [c for c in cases if c.requirement_id is None]
    assert len(unlinked) == 1

    stored_req = (
        await db_session.execute(
            select(TestRequirement).where(TestRequirement.id == "req-1")
        )
    ).scalar_one()
    assert stored_req.product_code == "comm_module_v2"
    assert stored_req.source == "dsl"


# ── FMEA RPN ────────────────────────────────────────────────────────────────


async def test_fmea_rpn_is_computed_server_side(db_session: AsyncSession) -> None:
    """Given S=7/O=4/D=3, rpn is derived (84) at flush — never accepted from
    the client, and recomputed when a rating changes."""
    from ate_cloud.models.knowledge import FMEA

    fmea = FMEA(
        id="fmea-1",
        component_code="PSU_MAIN",
        fault_code="over_voltage",
        failure_mode="Output over-voltage",
        severity=7,
        occurrence=4,
        detection=3,
        rpn=999,  # client-supplied value MUST be ignored
    )
    db_session.add(fmea)
    await db_session.flush()
    assert fmea.rpn == 84

    fmea.severity = 10
    await db_session.flush()
    assert fmea.rpn == 120


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("severity", 0),
        ("severity", 11),
        ("occurrence", 0),
        ("occurrence", 42),
        ("detection", -1),
        ("detection", 100),
    ],
)
async def test_fmea_rejects_out_of_range_ratings(
    db_session: AsyncSession, field: str, bad_value: int
) -> None:
    """Given an S/O/D rating outside 1-10, construction raises ValueError
    (validation error, not a raw DB constraint crash)."""
    from ate_cloud.models.knowledge import FMEA

    kwargs = {
        "id": "fmea-bad",
        "component_code": "PSU_MAIN",
        "failure_mode": "x",
        "severity": 5,
        "occurrence": 5,
        "detection": 5,
        field: bad_value,
    }
    with pytest.raises(ValueError, match="1.*10|rating"):
        FMEA(**kwargs)


async def test_fmea_db_check_constraint_blocks_bypass(db_session: AsyncSession) -> None:
    """The 1-10 range is also enforced by a DB CHECK constraint: a raw Core
    insert bypassing the Python validator fails at the database."""
    from sqlalchemy import text

    from ate_cloud.models.knowledge import FMEA

    db_session.add(
        FMEA(
            id="fmea-ok",
            component_code="C",
            failure_mode="m",
            severity=1,
            occurrence=1,
            detection=1,
        )
    )
    await db_session.flush()

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO fmeas (id, component_code, failure_mode, "
                "severity, occurrence, detection, rpn) "
                "VALUES ('fmea-raw', 'C', 'm', 11, 1, 1, 0)"
            )
        )


# ── Diagnosis ───────────────────────────────────────────────────────────────


async def test_diagnosis_persisted_and_linked_to_run(db_session: AsyncSession) -> None:
    """Given an execution run and a diagnosis, the diagnosis persists with the
    run link and accepts later operator feedback (helpful bool)."""
    from ate_cloud.models.knowledge import Diagnosis

    db_session.add(Execution(id="run-1", sequence_id="seq-1", status="COMPLETED"))
    diagnosis = Diagnosis(
        id="diag-1",
        run_id="run-1",
        symptom="CH1 voltage reads 5.6V, OVP tripped",
        conclusion="Likely feedback resistor drift on PSU_MAIN",
        context_summary="3 historical cases matched (cosine 0.81/0.77/0.74)",
        llm_model="gpt-4o-mini",
    )
    db_session.add(diagnosis)
    await db_session.flush()
    assert diagnosis.helpful is None  # no feedback yet

    diagnosis.helpful = True
    diagnosis.feedback_note = "Confirmed: replaced R12"
    await db_session.flush()

    stored = (
        await db_session.execute(
            select(Diagnosis).where(Diagnosis.run_id == "run-1")
        )
    ).scalar_one()
    assert stored.symptom.startswith("CH1 voltage")
    assert stored.llm_model == "gpt-4o-mini"
    assert stored.helpful is True
    assert stored.feedback_note == "Confirmed: replaced R12"


# ── Pydantic schemas (boundary validation) ──────────────────────────────────


def test_fmea_create_schema_rejects_out_of_range_ratings() -> None:
    """The request schema is the parse boundary: ratings are constrained to
    1-10 and rpn is not an accepted input field."""
    from pydantic import ValidationError

    from ate_cloud.schemas.knowledge import FMEACreate

    ok = FMEACreate(
        component_code="PSU_MAIN",
        fault_code="over_voltage",
        failure_mode="OVP",
        severity=10,
        occurrence=1,
        detection=1,
    )
    assert not hasattr(ok, "rpn")  # rpn is server-computed, never a client input

    with pytest.raises(ValidationError):
        FMEACreate(
            component_code="PSU_MAIN",
            failure_mode="OVP",
            severity=11,
            occurrence=1,
            detection=1,
        )


# ── Alembic migration smoke ─────────────────────────────────────────────────


def test_migration_upgrade_downgrade_cycle_on_temp_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """upgrade head creates the four tables; downgrade -1 drops them;
    upgrade head again recreates them — on a temp SQLite file."""
    db_file = tmp_path / "task10_mig.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    import src.ate_cloud.config as src_cfg  # noqa: E402 — needs sys.path first

    monkeypatch.setattr(
        src_cfg, "settings", SimpleNamespace(get_database_url=lambda: url)
    )
    # fileConfig globally disables existing loggers; no-op for this run.
    monkeypatch.setattr(logging.config, "fileConfig", lambda *a, **k: None)

    from alembic.config import Config

    from alembic import command

    cfg = Config(str(Path(root) / "alembic.ini"))

    def _tables() -> set[str]:
        conn = sqlite3.connect(db_file)
        try:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()

    command.upgrade(cfg, "head")
    created = _tables()
    assert {"test_requirements", "test_cases", "fmeas", "diagnoses"} <= created

    command.downgrade(cfg, "-1")
    after_downgrade = _tables()
    assert "fmeas" not in after_downgrade
    assert "test_cases" not in after_downgrade
    assert "test_requirements" not in after_downgrade
    assert "diagnoses" not in after_downgrade

    command.upgrade(cfg, "head")
    assert {"test_requirements", "test_cases", "fmeas", "diagnoses"} <= _tables()
