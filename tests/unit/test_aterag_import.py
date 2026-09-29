"""ATERag bundle import tests (P2).

These tests target the failure modes that do not raise: silent duplication,
partial application, silent overwrite of human edits. Each test names the
production symptom it prevents.

SQLite-backed (same as dev/CI), so they also prove the importer is
dialect-agnostic — the production backend is PostgreSQL.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ate_cloud.models import Base
from ate_cloud.models.knowledge import TestCase, TestRequirement
from ate_cloud.models.test_conditions import TestCondition
from ate_cloud.models.test_limits import TestLimit
from ate_cloud.schemas.aterag_bundle import (
    BundleModel,
    ClauseModel,
    LimitModel,
    RequirementModel,
    ScenarioModel,
    contract_hash,
)
from ate_cloud.services.aterag_importer import ATERagImporter

TODAY = date(2026, 9, 30)


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    """In-memory SQLite with the full schema created from the models."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _clause(kind: str, role: str, fp: str, status: str = "approved") -> ClauseModel:
    return ClauseModel(
        kind=kind, text=f"{kind} text", role=role, status=status, cond_fingerprint=fp
    )


def _bundle(
    *,
    title: str = "输出电流",
    inputs: list[ClauseModel] | None = None,
    outputs: list[ClauseModel] | None = None,
    limits: list[LimitModel] | None = None,
    scenarios: list[ScenarioModel] | None = None,
    removed: list[str] | None = None,
    code: str = "SR-1",
) -> BundleModel:
    return BundleModel(
        bundle_version="1.0",
        contract_hash=contract_hash(),
        product_code="PA601-TEST",
        doc_version="B",
        requirements=[
            RequirementModel(
                requirement_code=code,
                spec_requirement_id=code,
                title=title,
                input_conditions=inputs if inputs is not None else [_clause("load", "input", "i1")],
                output_conditions=(
                    outputs if outputs is not None else [_clause("output_current", "output", "o1")]
                ),
                limits=limits if limits is not None else [],
                scenarios=scenarios
                if scenarios is not None
                else [ScenarioModel(scenario_id=f"{code}#0", seq=0, name=title, rail="-54V")],
            )
        ],
        removed_requirement_codes=removed or [],
    )


# ── contract ────────────────────────────────────────────────────────────────


class TestContractGate:
    def test_local_hash_is_the_baseline(self) -> None:
        """The importer's own hash must equal the producer's recorded baseline.

        If these ever diverge, either side edited the schema without the other.
        """
        from ate_cloud.schemas.aterag_bundle import KNOWN_CONTRACT_HASH

        assert contract_hash() == KNOWN_CONTRACT_HASH

    def test_unknown_field_rejected(self) -> None:
        """A field the producer added but we don't know must not be silently dropped."""
        payload = _bundle().model_dump()
        payload["unexpected_field"] = 1
        with pytest.raises(ValueError):
            BundleModel.model_validate(payload)

    def test_dup_requirement_code_rejected(self) -> None:
        b = _bundle()
        payload = b.model_dump()
        payload["requirements"].append(payload["requirements"][0])
        with pytest.raises(ValueError):
            BundleModel.model_validate(payload)

    def test_dup_scenario_seq_rejected(self) -> None:
        """case_code derives from (requirement_code, seq) — a collision would
        merge two scenarios into one case and drop conditions.

        The rejection happens while *building* the bundle (a field_validator on
        RequirementModel), so it must be asserted around the construction, not
        around a re-validation of an already-valid object.
        """
        with pytest.raises(ValueError):
            _bundle(
                scenarios=[
                    ScenarioModel(scenario_id="a", seq=0),
                    ScenarioModel(scenario_id="b", seq=0),
                ]
            )


# ── first import ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestFirstImport:
    async def test_creates_requirement_conditions_cases(self, db: AsyncSession) -> None:
        res = await ATERagImporter().import_bundle(
            db, _bundle(), dry_run=False, today=TODAY
        )
        assert res.requirements.created == 1
        assert res.conditions.created == 2
        assert res.cases.created == 1
        assert res.cases_reset_to_draft == []

    async def test_conditions_carry_kind_and_status(self, db: AsyncSession) -> None:
        await ATERagImporter().import_bundle(db, _bundle(), dry_run=False, today=TODAY)
        rows = (await db.execute(select(TestCondition))).scalars().all()
        assert {r.kind for r in rows} == {"load", "output_current"}
        assert {r.side for r in rows} == {"input", "output"}
        assert all(r.owner_type == "requirement" for r in rows)

    async def test_draft_clause_stays_draft(self, db: AsyncSession) -> None:
        """A supplement proposal must arrive as draft, not as an approved
        criterion — otherwise a spec's 'common knowledge' premise becomes a
        signed quality standard without anyone reviewing it."""
        b = _bundle(
            outputs=[_clause("output_current", "output", "o1", status="draft")],
        )
        await ATERagImporter().import_bundle(db, b, dry_run=False, today=TODAY)
        row = (
            await db.execute(
                select(TestCondition).where(TestCondition.kind == "output_current")
            )
        ).scalar_one()
        assert row.status == "draft"

    async def test_one_sided_limit_materialised(self, db: AsyncSession) -> None:
        """Over/under-voltage criteria have only one bound; they must still
        produce a limit row rather than being dropped or padded with an
        invented bound."""
        b = _bundle(limits=[LimitModel(rail="-54V", max=55.62, unit="V")])
        await ATERagImporter().import_bundle(db, b, dry_run=False, today=TODAY)
        lim = (await db.execute(select(TestLimit))).scalar_one()
        assert lim.spec_low is None
        assert lim.spec_high == 55.62
        assert lim.limit_id.startswith("aterag-")

    async def test_three_valued_limit_keeps_typ(self, db: AsyncSession) -> None:
        b = _bundle(limits=[LimitModel(rail="-54V", min=-53.2, typ=-54.0, max=-54.8, unit="V")])
        await ATERagImporter().import_bundle(db, b, dry_run=False, today=TODAY)
        lim = (await db.execute(select(TestLimit))).scalar_one()
        assert (lim.spec_low, lim.spec_typ, lim.spec_high) == (-53.2, -54.0, -54.8)

    async def test_bare_limit_produces_no_row(self, db: AsyncSession) -> None:
        """A limit with no bounds judges nothing; a row for it would look
        authoritative in the limit table while constraining nothing."""
        b = _bundle(limits=[LimitModel(rail="-54V")])
        await ATERagImporter().import_bundle(db, b, dry_run=False, today=TODAY)
        assert (await db.execute(select(func.count()).select_from(TestLimit))).scalar_one() == 0


# ── idempotency ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestIdempotency:
    async def test_reimport_does_not_duplicate(self, db: AsyncSession) -> None:
        """Re-importing the same bundle must not double the conditions —
        duplicated conditions look fine on a dashboard but double the number
        of instrument settings a case is given."""
        imp = ATERagImporter()
        b = _bundle()
        await imp.import_bundle(db, b, dry_run=False, today=TODAY)
        res = await imp.import_bundle(db, b, dry_run=False, today=TODAY)
        assert res.requirements.created == 0
        assert res.conditions.created == 0
        assert res.cases.created == 0
        n = (await db.execute(select(func.count()).select_from(TestCondition))).scalar_one()
        assert n == 2

    async def test_removed_clause_is_deleted(self, db: AsyncSession) -> None:
        """A condition dropped upstream must disappear, not linger as a ghost."""
        imp = ATERagImporter()
        await imp.import_bundle(db, _bundle(), dry_run=False, today=TODAY)
        b2 = _bundle(outputs=[])
        await imp.import_bundle(db, b2, dry_run=False, today=TODAY)
        rows = (await db.execute(select(TestCondition))).scalars().all()
        assert {r.kind for r in rows} == {"load"}

    async def test_changed_clause_updates_in_place(self, db: AsyncSession) -> None:
        imp = ATERagImporter()
        await imp.import_bundle(db, _bundle(), dry_run=False, today=TODAY)
        b2 = _bundle(
            outputs=[
                ClauseModel(
                    kind="output_current",
                    text="changed",
                    role="output",
                    status="approved",
                    cond_fingerprint="o1",
                )
            ]
        )
        res = await imp.import_bundle(db, b2, dry_run=False, today=TODAY)
        assert res.conditions.updated >= 1
        row = (
            await db.execute(
                select(TestCondition).where(TestCondition.cond_fingerprint == "o1")
            )
        ).scalar_one()
        assert row.text == "changed"
        assert (await db.execute(select(func.count()).select_from(TestCondition))).scalar_one() == 2


# ── draft reset policy ──────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDraftReset:
    async def _approved_case(self, db: AsyncSession, bundle: BundleModel) -> str:
        await ATERagImporter().import_bundle(db, bundle, dry_run=False, today=TODAY)
        code = f"{bundle.requirements[0].requirement_code}-S000"
        row = (await db.execute(select(TestCase).where(TestCase.case_code == code))).scalar_one()
        row.status = "active"
        await db.commit()
        return code

    async def test_criteria_change_resets_case_to_draft(self, db: AsyncSession) -> None:
        """An approved case whose limits changed must go back to draft: the
        engineer approved a different criterion."""
        b1 = _bundle(limits=[LimitModel(rail="-54V", max=11.1, unit="A")])
        code = await self._approved_case(db, b1)
        b2 = _bundle(limits=[LimitModel(rail="-54V", max=9.9, unit="A")])
        res = await ATERagImporter().import_bundle(db, b2, dry_run=False, today=TODAY)
        assert code in res.cases_reset_to_draft
        row = (await db.execute(select(TestCase).where(TestCase.case_code == code))).scalar_one()
        assert row.status == "draft"

    async def test_unchanged_criteria_keep_approved_case(self, db: AsyncSession) -> None:
        """Re-importing identical data must not throw away approvals — that
        would make the status meaningless and train people to ignore it."""
        b = _bundle(limits=[LimitModel(rail="-54V", max=11.1, unit="A")])
        code = await self._approved_case(db, b)
        res = await ATERagImporter().import_bundle(db, b, dry_run=False, today=TODAY)
        assert res.cases_reset_to_draft == []
        row = (await db.execute(select(TestCase).where(TestCase.case_code == code))).scalar_one()
        assert row.status == "active"


# ── manual edit conflicts ───────────────────────────────────────────────────


@pytest.mark.asyncio
class TestConflicts:
    async def test_manual_requirement_edit_survives_without_confirmation(
        self, db: AsyncSession
    ) -> None:
        """A requirement typed in by hand must not be clobbered by an import
        that was not explicitly told to overwrite."""
        imp = ATERagImporter()
        await imp.import_bundle(db, _bundle(), dry_run=False, today=TODAY)
        row = (await db.execute(select(TestRequirement))).scalar_one()
        row.source = "manual"
        row.title = "人工改过的标题"
        await db.commit()

        res = await imp.import_bundle(db, _bundle(), dry_run=False, confirm_overwrite=False)
        assert any(c.field == "title" for c in res.conflicts)
        after = (await db.execute(select(TestRequirement))).scalar_one()
        assert after.title == "人工改过的标题"

    async def test_confirmed_overwrite_applies(self, db: AsyncSession) -> None:
        imp = ATERagImporter()
        await imp.import_bundle(db, _bundle(), dry_run=False, today=TODAY)
        row = (await db.execute(select(TestRequirement))).scalar_one()
        row.source = "manual"
        row.title = "人工改过的标题"
        await db.commit()
        res = await imp.import_bundle(db, _bundle(), dry_run=False, confirm_overwrite=True)
        after = (await db.execute(select(TestRequirement))).scalar_one()
        assert after.title == "输出电流"
        assert not any(c.field == "title" for c in res.conflicts)


# ── dry run ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDryRun:
    async def test_dry_run_writes_nothing(self, db: AsyncSession) -> None:
        res = await ATERagImporter().import_bundle(db, _bundle(), dry_run=True, today=TODAY)
        assert res.dry_run is True
        assert res.changed is True  # the plan is non-empty…
        assert (await db.execute(select(func.count()).select_from(TestRequirement))).scalar_one() == 0
        assert (await db.execute(select(func.count()).select_from(TestCondition))).scalar_one() == 0
        assert (await db.execute(select(func.count()).select_from(TestCase))).scalar_one() == 0


# ── spec revision (stale) ───────────────────────────────────────────────────


@pytest.mark.asyncio
class TestStale:
    async def test_removed_requirement_marked_stale_not_deleted(
        self, db: AsyncSession
    ) -> None:
        """A requirement missing from a newer spec revision is kept: deleting
        it would break the traceability chain back to results already measured
        against it."""
        imp = ATERagImporter()
        await imp.import_bundle(db, _bundle(code="SR-OLD"), dry_run=False, today=TODAY)
        b2 = _bundle(code="SR-NEW", removed=["SR-OLD"])
        res = await imp.import_bundle(db, b2, dry_run=False, today=TODAY)
        assert res.requirements_staled == ["SR-OLD"]
        rows = (await db.execute(select(TestRequirement))).scalars().all()
        codes = {r.requirement_code: r.status for r in rows}
        assert codes["SR-OLD"] == "stale"
        assert codes["SR-NEW"] == "active"

    async def test_stale_requirement_reactivated_when_it_returns(
        self, db: AsyncSession
    ) -> None:
        """A requirement that comes back in a later revision must stop being
        marked stale — otherwise it stays invisible forever."""
        imp = ATERagImporter()
        await imp.import_bundle(db, _bundle(code="SR-A"), dry_run=False, today=TODAY)
        await imp.import_bundle(
            db, _bundle(code="SR-B", removed=["SR-A"]), dry_run=False, today=TODAY
        )
        await imp.import_bundle(db, _bundle(code="SR-A"), dry_run=False, today=TODAY)
        row = (
            await db.execute(
                select(TestRequirement).where(TestRequirement.requirement_code == "SR-A")
            )
        ).scalar_one()
        assert row.status == "active"

    async def test_conditions_of_stale_requirement_survive(
        self, db: AsyncSession
    ) -> None:
        """Marking stale must not cascade into deleting conditions — the
        traceability back to past measurements depends on them.

        Both requirements keep their own 2 conditions (SR-A is stale, SR-B is
        active), so 4 in total.
        """
        imp = ATERagImporter()
        await imp.import_bundle(db, _bundle(code="SR-A"), dry_run=False, today=TODAY)
        await imp.import_bundle(
            db, _bundle(code="SR-B", removed=["SR-A"]), dry_run=False, today=TODAY
        )
        rows = (await db.execute(select(TestCondition))).scalars().all()
        owners = {r.owner_id for r in rows}
        stale = (
            await db.execute(select(TestRequirement).where(TestRequirement.requirement_code == "SR-A"))
        ).scalar_one()
        assert stale.id in owners, "stale 需求的条件不得被级联删除"


# ── integrity guards ────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestIntegrity:
    async def test_clause_without_fingerprint_is_reported_not_imported(
        self, db: AsyncSession
    ) -> None:
        """A condition we cannot fingerprint cannot be imported idempotently;
        it must be reported rather than silently creating a duplicate later."""
        b = _bundle(outputs=[_clause("output_current", "output", "")])
        res = await ATERagImporter().import_bundle(db, b, dry_run=False, today=TODAY)
        assert any(u["reason"].startswith("缺 cond_fingerprint") for u in res.unmapped)
        assert res.conditions.created == 1  # only the input clause

    async def test_provenance_recorded(self, db: AsyncSession) -> None:
        await ATERagImporter().import_bundle(db, _bundle(), dry_run=False, today=TODAY)
        req = (await db.execute(select(TestRequirement))).scalar_one()
        case = (await db.execute(select(TestCase))).scalar_one()
        assert req.source == "aterag"
        assert case.created_by == "aterag:bundle"

    async def test_requirement_id_is_stable_uuid(self, db: AsyncSession) -> None:
        """Re-import must reuse the same requirement row, not create a second
        one under a new id — cases point at requirement_id."""
        imp = ATERagImporter()
        await imp.import_bundle(db, _bundle(), dry_run=False, today=TODAY)
        first = (await db.execute(select(TestRequirement))).scalar_one().id
        await imp.import_bundle(db, _bundle(), dry_run=False, today=TODAY)
        rows = (await db.execute(select(TestRequirement))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == first
        assert uuid.UUID(rows[0].id)
