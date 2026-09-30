"""Condition read + review endpoint tests (P5 front end).

These back the review screen, so the assertions are about what the screen must
*not* let happen: approving conditions for a requirement that no longer exists,
approving on someone's behalf without recording who, and presenting a stale
requirement's conditions as work still to do.

The stale-requirement cases matter more than they look. A requirement that
vanished from a newer spec revision keeps its conditions for traceability, and
it would be easy — and wrong — to present those as reviewable: a reviewer would
spend time on conditions that can never ship, and the "approved" mark would sit
on a requirement the tester does not use.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ate_cloud.models import Base
from ate_cloud.models.knowledge import TestRequirement
from ate_cloud.models.test_conditions import TestCondition
from ate_cloud.schemas.aterag_bundle import (
    BundleModel,
    ClauseModel,
    RequirementModel,
    ScenarioModel,
    contract_hash,
)
from ate_cloud.schemas.test_conditions import ConditionReviewRequest
from ate_cloud.services.aterag_importer import ATERagImporter

PRODUCT = "PA601-TEST"


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _bundle(reqs: list[RequirementModel], *, removed: list[str] | None = None) -> BundleModel:
    return BundleModel(
        bundle_version="1.0",
        contract_hash=contract_hash(),
        product_code=PRODUCT,
        doc_version="B",
        requirements=reqs,
        removed_requirement_codes=removed or [],
    )


def _req(code: str, outputs: list[ClauseModel], inputs: list[ClauseModel] | None = None):
    return RequirementModel(
        requirement_code=code,
        title=code,
        section_path="4.3.1",
        input_conditions=inputs or [],
        output_conditions=outputs,
        scenarios=[ScenarioModel(scenario_id=f"{code}#s0", seq=0)],
    )


async def _import(db: AsyncSession, *reqs: RequirementModel, removed=None) -> None:
    await ATERagImporter().import_bundle(
        db, _bundle(list(reqs), removed=removed), dry_run=False, today=date(2026, 9, 30)
    )


def _approved(kind: str, fp: str) -> ClauseModel:
    return ClauseModel(kind=kind, text=kind, role="output", status="approved", cond_fingerprint=fp)


def _draft(kind: str, fp: str) -> ClauseModel:
    return ClauseModel(
        kind=kind, text=kind, role="output", status="draft", cond_fingerprint=fp
    )


async def _get_requirement(db: AsyncSession, code: str) -> TestRequirement:
    from sqlalchemy import select

    return (
        await db.execute(
            select(TestRequirement).where(TestRequirement.requirement_code == code)
        )
    ).scalar_one()


# ── listing ─────────────────────────────────────────────────────────────────


class TestListConditions:
    @pytest.mark.asyncio
    async def test_lists_and_denormalizes_requirement_fields(
        self, db: AsyncSession
    ) -> None:
        """The table must render without a second request per row."""

        from ate_cloud.api.v1.knowledge_conditions import list_conditions

        await _import(db, _req("SR-1", [_approved("output_voltage", "a")]))
        req = await _get_requirement(db, "SR-1")

        page = await list_conditions(
            db, requirement_id=req.id, product_code=None, side=None, kind=None,
            status_filter=None, skip=0, limit=100,
        )
        assert page.total == 1
        item = page.items[0]
        assert item.requirement_code == "SR-1"
        assert item.section_path == "4.3.1"

    @pytest.mark.asyncio
    async def test_status_filter_is_the_work_list(self, db: AsyncSession) -> None:
        """The review screen leads with "what is unapproved"."""

        from ate_cloud.api.v1.knowledge_conditions import list_conditions

        await _import(
            db,
            _req("SR-1", [_approved("output_voltage", "a"), _draft("signal_state", "b")]),
        )
        req = await _get_requirement(db, "SR-1")
        page = await list_conditions(
            db, requirement_id=req.id, product_code=None, side=None, kind=None,
            status_filter="draft", skip=0, limit=100,
        )
        assert [c.kind for c in page.items] == ["signal_state"]

    @pytest.mark.asyncio
    async def test_product_filter_goes_through_the_owner(self, db: AsyncSession) -> None:
        """Conditions hang off requirements, so filtering by product must
        resolve the owner. Comparing product_code against owner_id would return
        an empty page — a wrong answer, not an error."""

        from ate_cloud.api.v1.knowledge_conditions import list_conditions

        await _import(db, _req("SR-1", [_approved("output_voltage", "a")]))
        hit = await list_conditions(
            db, requirement_id=None, product_code=PRODUCT, side=None, kind=None,
            status_filter=None, skip=0, limit=100,
        )
        miss = await list_conditions(
            db, requirement_id=None, product_code="OTHER", side=None, kind=None,
            status_filter=None, skip=0, limit=100,
        )
        assert hit.total == 1
        assert miss.total == 0


# ── summary ─────────────────────────────────────────────────────────────────


class TestSummary:
    @pytest.mark.asyncio
    async def test_counts_drafts_per_requirement(self, db: AsyncSession) -> None:
        from ate_cloud.api.v1.knowledge_conditions import condition_summary

        await _import(
            db,
            _req("SR-1", [_approved("output_voltage", "a"), _draft("signal_state", "b")]),
        )
        out = await condition_summary(db, product_code=PRODUCT)
        assert out["pending_total"] == 1
        row = out["items"][0]
        assert row["draft"] == 1
        assert row["approved"] == 1

    @pytest.mark.asyncio
    async def test_stale_requirement_is_not_counted_as_work(self, db: AsyncSession) -> None:
        """Its conditions are kept for traceability, but presenting them as
        pending work sends a reviewer to sign off on something that can never
        ship — and the "approved" mark would sit on an unused requirement."""
        from ate_cloud.api.v1.knowledge_conditions import condition_summary

        await _import(db, _req("SR-OLD", [_draft("signal_state", "a")]))
        await _import(db, _req("SR-NEW", [_draft("signal_state", "b")]), removed=["SR-OLD"])

        out = await condition_summary(db, product_code=PRODUCT)
        rows = {r["requirement_code"]: r for r in out["items"]}
        assert rows["SR-OLD"]["requirement_status"] == "stale"
        assert rows["SR-OLD"]["draft"] == 0
        assert rows["SR-NEW"]["draft"] == 1
        assert out["pending_total"] == 1


# ── approval ────────────────────────────────────────────────────────────────


class TestApprove:
    @pytest.mark.asyncio
    async def test_approves_drafts_and_records_who(self, db: AsyncSession) -> None:
        from sqlalchemy import select

        from ate_cloud.api.v1.knowledge_conditions import approve_conditions

        await _import(
            db, _req("SR-1", [_approved("output_voltage", "a"), _draft("signal_state", "b")])
        )
        req = await _get_requirement(db, "SR-1")
        out = await approve_conditions(
            ConditionReviewRequest(requirement_id=req.id, by="张工"), db
        )
        assert out.approved == 1
        assert out.by == "张工"
        rows = (
            await db.execute(
                select(TestCondition).where(TestCondition.owner_id == req.id)
            )
        ).scalars().all()
        assert {c.status for c in rows} == {"approved"}

    @pytest.mark.asyncio
    async def test_never_touches_another_requirement(self, db: AsyncSession) -> None:
        """Approving "everything draft" would sign clauses the reviewer never
        opened."""
        from sqlalchemy import select

        from ate_cloud.api.v1.knowledge_conditions import approve_conditions

        await _import(
            db,
            _req("SR-1", [_draft("signal_state", "a")]),
            _req("SR-2", [_draft("signal_state", "b")]),
        )
        req1 = await _get_requirement(db, "SR-1")
        await approve_conditions(ConditionReviewRequest(requirement_id=req1.id, by="张工"), db)
        req2 = await _get_requirement(db, "SR-2")
        statuses = (
            await db.execute(
                select(TestCondition.status).where(TestCondition.owner_id == req2.id)
            )
        ).scalars().all()
        assert statuses == ["draft"]

    @pytest.mark.asyncio
    async def test_repeat_approval_is_a_no_op_not_an_error(self, db: AsyncSession) -> None:
        """A reviewer clicking twice must not see a failure."""
        from ate_cloud.api.v1.knowledge_conditions import approve_conditions

        await _import(db, _req("SR-1", [_draft("signal_state", "a")]))
        req = await _get_requirement(db, "SR-1")
        first = await approve_conditions(
            ConditionReviewRequest(requirement_id=req.id, by="张工"), db
        )
        second = await approve_conditions(
            ConditionReviewRequest(requirement_id=req.id, by="张工"), db
        )
        assert first.approved == 1
        assert second.approved == 0

    @pytest.mark.asyncio
    async def test_cannot_approve_a_stale_requirement(self, db: AsyncSession) -> None:
        from fastapi import HTTPException
        from sqlalchemy import select

        from ate_cloud.api.v1.knowledge_conditions import approve_conditions

        await _import(db, _req("SR-OLD", [_draft("signal_state", "a")]))
        await _import(db, _req("SR-NEW", []), removed=["SR-OLD"])
        old = await _get_requirement(db, "SR-OLD")
        with pytest.raises(HTTPException) as exc:
            await approve_conditions(
                ConditionReviewRequest(requirement_id=old.id, by="张工"), db
            )
        assert exc.value.status_code == 409
        rows = (
            await db.execute(select(TestCondition).where(TestCondition.owner_id == old.id))
        ).scalars().all()
        assert {c.status for c in rows} == {"draft"}

    @pytest.mark.asyncio
    async def test_unknown_requirement_is_404(self, db: AsyncSession) -> None:
        from fastapi import HTTPException

        from ate_cloud.api.v1.knowledge_conditions import approve_conditions

        with pytest.raises(HTTPException) as exc:
            await approve_conditions(
                ConditionReviewRequest(requirement_id=str(uuid.uuid4()), by="张工"), db
            )
        assert exc.value.status_code == 404

    def test_blank_signature_rejected(self) -> None:
        """An approval with no name cannot be audited, which makes it
        indistinguishable from no approval at all — the state everything
        starts in."""
        with pytest.raises(ValueError):
            ConditionReviewRequest(requirement_id="x", by="   ")

    @pytest.mark.asyncio
    async def test_approval_preserves_the_rest_of_the_row(self, db: AsyncSession) -> None:
        """Regression: ``update()`` without ``.values()`` resets every column to
        its default. ``status``'s default is "draft", so approving silently
        wiped the condition's value/text while still reporting one row updated.

        Asserted on the payload rather than the status, because the payload is
        what the reset destroyed — and what a production test would read.
        """
        from sqlalchemy import select

        from ate_cloud.api.v1.knowledge_conditions import approve_conditions

        clause = ClauseModel(
            kind="output_voltage",
            text="额定输出电压 54V ±0.5V",
            role="output",
            value={"min": 53.5, "max": 54.5, "unit": "V"},
            status="draft",
            cond_fingerprint="a",
            method_ref="en300132_steady_state",
        )
        await _import(db, _req("SR-1", [clause]))
        req = await _get_requirement(db, "SR-1")
        await approve_conditions(ConditionReviewRequest(requirement_id=req.id, by="张工"), db)
        row = (
            await db.execute(select(TestCondition).where(TestCondition.owner_id == req.id))
        ).scalar_one()
        assert row.status == "approved"
        assert row.value == {"min": 53.5, "max": 54.5, "unit": "V"}
        assert row.text == "额定输出电压 54V ±0.5V"
        assert row.method_ref == "en300132_steady_state"
        assert row.cond_fingerprint == "a"

    @pytest.mark.asyncio
    async def test_partial_approval_narrows_to_named_ids(self, db: AsyncSession) -> None:
        """A reviewer who accepts one clause and rejects another must be able to
        say so, rather than approving the lot."""
        from sqlalchemy import select

        from ate_cloud.api.v1.knowledge_conditions import approve_conditions

        await _import(
            db, _req("SR-1", [_draft("signal_state", "a"), _draft("output_voltage", "b")])
        )
        req = await _get_requirement(db, "SR-1")
        rows = (
            await db.execute(
                select(TestCondition).where(
                    TestCondition.owner_id == req.id, TestCondition.kind == "signal_state"
                )
            )
        ).scalars().all()
        out = await approve_conditions(
            ConditionReviewRequest(
                requirement_id=req.id, by="张工", condition_ids=[rows[0].id]
            ),
            db,
        )
        assert out.approved == 1
        statuses = dict(
            (
                await db.execute(
                    select(TestCondition.kind, TestCondition.status).where(
                        TestCondition.owner_id == req.id
                    )
                )
            ).all()
        )
        assert statuses == {"signal_state": "approved", "output_voltage": "draft"}


# ── what the reviewer needs on screen ────────────────────────────────────────


class TestReviewScreenHasWhatItNeeds:
    """The review screen's job is to answer "can this clause ship?".

    Both of these were silently dropped on the floor at import time, and the
    failure mode is not a missing column — it is a reviewer signing a form
    they cannot check:

    * the spec clause. Signing means confirming the clause says what the
      condition claims, and a distilled fragment can invert the original's
      meaning. Without the text on screen that check is impossible.
    * ``flags`` / ``assessment``. ``annotation_draft`` marks clauses a person
      wrote rather than the rules cut. Those are exactly the criteria the red
      line is about, and with the signal dropped they render like any other
      row.
    """

    async def test_extraction_signals_survive_the_import(self, db: AsyncSession) -> None:
        req = _req("SR-SIG-1", [_draft("output_voltage", "fp-sig-1")])
        req.flags = ["annotation_draft"]
        req.assessment = {"sufficiency": "needs_review"}
        await _import(db, req)

        row = await _get_requirement(db, "SR-SIG-1")
        assert row.flags is not None, "flags 没落盘 -> 界面标不出人工注记的条件"
        assert "annotation_draft" in row.flags
        assert row.assessment is not None
        assert "needs_review" in row.assessment

    async def test_signals_are_replaced_on_reimport(self, db: AsyncSession) -> None:
        """A re-import must refresh them, not leave the previous verdict.

        The spec changed and extraction now succeeds where it used to need a
        human. Keeping the stale ``annotation_draft`` would keep marking
        ordinary rule-cut clauses as hand-written, and the reviewer would chase
        an annotation that no longer exists.
        """
        first = _req("SR-SIG-2", [_draft("output_voltage", "fp-sig-2")])
        first.flags = ["annotation_draft"]
        first.assessment = {"sufficiency": "needs_review"}
        await _import(db, first)

        second = _req("SR-SIG-2", [_draft("output_voltage", "fp-sig-2b")])
        second.flags = []
        second.assessment = {"sufficiency": "sufficient"}
        await _import(db, second)

        row = await _get_requirement(db, "SR-SIG-2")
        assert row.flags is None, "旧的 annotation_draft 没被清掉"
        assert row.assessment is not None
        assert "sufficient" in row.assessment
        assert "needs_review" not in row.assessment

    async def test_signals_serialise_deterministically(self, db: AsyncSession) -> None:
        """Key order must not make a clean replay look like a change.

        The importer's contract is that re-importing an unchanged bundle is a
        no-op. ``json.dumps`` without ``sort_keys`` would emit a different
        string for the same mapping whenever insertion order differed, which
        is invisible in the row counts and shows up as phantom churn.
        """
        req = _req("SR-SIG-3", [_draft("output_voltage", "fp-sig-3")])
        req.assessment = {"b": "2", "a": "1"}
        await _import(db, req)
        row = await _get_requirement(db, "SR-SIG-3")
        assert row.assessment is not None
        assert row.assessment.index('"a"') < row.assessment.index('"b"')

    async def test_spec_clause_reaches_the_condition_response(self, db: AsyncSession) -> None:
        from ate_cloud.api.v1.knowledge_conditions import list_conditions

        req = _req("SR-SIG-4", [_draft("output_voltage", "fp-sig-4")])
        req.description = "在 -25℃ 低温下限启动时, 开机输出延时应不大于 12s"
        req.notes = "启动过程中允许跌落"
        await _import(db, req)
        row = await _get_requirement(db, "SR-SIG-4")

        page = await list_conditions(
            db, requirement_id=row.id, product_code=None, side=None, kind=None,
            status_filter=None, skip=0, limit=100,
        )
        item = page.items[0]
        assert item.requirement_description == req.description
        assert item.requirement_notes == req.notes

    async def test_missing_signals_degrade_to_empty_not_error(self, db: AsyncSession) -> None:
        """NULL is normal for rows written before the columns existed.

        A review screen that 500s on an older requirement is worse than one
        that shows it without a badge.
        """
        from ate_cloud.api.v1.knowledge_conditions import list_conditions

        req = _req("SR-SIG-5", [_draft("output_voltage", "fp-sig-5")])
        await _import(db, req)
        row = await _get_requirement(db, "SR-SIG-5")
        row.flags = "not json at all"
        row.assessment = "also not json"
        db.add(row)
        await db.commit()

        page = await list_conditions(
            db, requirement_id=row.id, product_code=None, side=None, kind=None,
            status_filter=None, skip=0, limit=100,
        )
        assert page.items[0].requirement_flags == []
        assert page.items[0].requirement_assessment == {}
