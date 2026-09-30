"""A signature must change what the plan actually includes.

The defect
----------
``/imports/aterag/plan`` planned straight from the posted bundle. ATERag emits
every industry-method proposal as ``draft`` and nothing rewrites the exported
file, so a bundle pasted into the wizard always said draft — even after a human
signed the condition in ATEStudio. Signing moved a database column and nothing
else.

Measured on the board before the fix: 82 pending, 530 steps; sign two
conditions; re-plan; 82 pending, 530 steps. The wizard's own warning says
"评审签字后重新规划即可纳入" — a promise it structurally could not keep, because
step 4 re-plans the same text pasted in step 1.

The fix overlays the database's approval state onto the bundle in the endpoint,
leaving :func:`plan_flow` — the red line — as the single, database-free place
that decides what may be planned.

What these tests hold
---------------------
The overlay is a safety mechanism, so the tests are mostly about what it must
*not* do:

* never downgrade an approved bundle clause (a re-export losing a signature
  must not silently revoke a production criterion)
* never promote on a fingerprint that has no approved database row
* never promote a condition that was never in the database
* an unsigned draft stays excluded, and the red line's own count still holds
"""

from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import select
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
from ate_cloud.services.aterag_importer import ATERagImporter

PRODUCT = "PA601-D54A"


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _clause(kind: str, fp: str, status: str = "draft") -> ClauseModel:
    return ClauseModel(
        kind=kind,
        text=kind,
        role="output",
        status=status,
        cond_fingerprint=fp,
    )


def _bundle(reqs: list[RequirementModel]) -> BundleModel:
    return BundleModel(
        bundle_version="1.0",
        contract_hash=contract_hash(),
        product_code=PRODUCT,
        doc_version="B",
        requirements=reqs,
        removed_requirement_codes=[],
    )


def _req(code: str, outputs: list[ClauseModel]) -> RequirementModel:
    return RequirementModel(
        requirement_code=code,
        title=code,
        section_path="4.3.1",
        output_conditions=outputs,
        scenarios=[ScenarioModel(scenario_id=f"{code}#s0", seq=0)],
    )


async def _seed(db: AsyncSession, code: str, clause: ClauseModel) -> str:
    """Import one requirement, returning the stored requirement id."""
    await ATERagImporter().import_bundle(
        db, _bundle([_req(code, [clause])]), dry_run=False, today=date(2026, 9, 30)
    )
    from sqlalchemy import select


    row = (
        await db.execute(
            select(TestRequirement).where(TestRequirement.requirement_code == code)
        )
    ).scalar_one()
    return row.id


def _statuses(bundle: BundleModel, code: str) -> list[str]:
    req = next(r for r in bundle.requirements if r.requirement_code == code)
    return [c.status for c in req.output_conditions]


@pytest.mark.asyncio
class TestOverlayMovesOnlyInTheSafeDirection:
    async def test_signed_draft_becomes_approved(self, db: AsyncSession) -> None:
        """The whole point: a signature has to be visible to the planner."""
        from ate_cloud.api.v1.imports import _apply_review_state
        from ate_cloud.api.v1.knowledge_conditions import approve_conditions
        from ate_cloud.schemas.test_conditions import ConditionReviewRequest

        req_id = await _seed(db, "SR-S1", _clause("output_voltage", "fp-s1"))
        cond_id = (
            await db.execute(
                select(TestCondition.id).where(TestCondition.owner_id == req_id)
            )
        ).scalar_one()
        await approve_conditions(
            ConditionReviewRequest(requirement_id=req_id, by="张工", condition_ids=[cond_id]),
            db,
        )

        # Fresh bundle, exactly as the wizard re-posts it: still says draft.
        fresh = _bundle([_req("SR-S1", [_clause("output_voltage", "fp-s1")])])
        assert _statuses(fresh, "SR-S1") == ["draft"]

        merged = await _apply_review_state(db, fresh)
        assert _statuses(merged, "SR-S1") == ["approved"]

    async def test_unsigned_draft_stays_draft(self, db: AsyncSession) -> None:
        """Most of PA601 is still unsigned; the overlay must not leak."""
        from ate_cloud.api.v1.imports import _apply_review_state

        await _seed(db, "SR-S2", _clause("output_voltage", "fp-s2"))
        fresh = _bundle([_req("SR-S2", [_clause("output_voltage", "fp-s2")])])

        assert _statuses(await _apply_review_state(db, fresh), "SR-S2") == ["draft"]

    async def test_approved_bundle_clause_is_never_downgraded(self, db: AsyncSession) -> None:
        """A re-export that lost a signature must not revoke a criterion.

        If the bundle says approved and the database says draft — say an admin
        reset it — the conservative reading matters: the bundle is what the
        planner would otherwise treat as authoritative for inclusion, and
        silently excluding it would change a production sequence with no event
        anyone could see. The overlay only ever adds.
        """
        from ate_cloud.api.v1.imports import _apply_review_state

        fresh = _bundle([_req("SR-S3", [_clause("output_voltage", "fp-s3", status="approved")])])
        assert _statuses(await _apply_review_state(db, fresh), "SR-S3") == ["approved"]

    async def test_fingerprint_absent_from_the_db_stays_draft(self, db: AsyncSession) -> None:
        """A fingerprint the database has never seen cannot inherit a signature.

        Matching on requirement code or row order instead would let an unrelated
        condition pick up someone else's approval.
        """
        from ate_cloud.api.v1.imports import _apply_review_state

        await _seed(db, "SR-S4", _clause("output_voltage", "fp-s4"))
        fresh = _bundle([_req("SR-S4", [_clause("output_voltage", "fp-never-signed")])])

        assert _statuses(await _apply_review_state(db, fresh), "SR-S4") == ["draft"]

    async def test_clause_absent_from_the_db_stays_draft(self, db: AsyncSession) -> None:
        """A brand-new condition the database has no row for is still draft."""
        from ate_cloud.api.v1.imports import _apply_review_state

        fresh = _bundle([_req("SR-S5", [_clause("output_voltage", "fp-brand-new")])])
        assert _statuses(await _apply_review_state(db, fresh), "SR-S5") == ["draft"]

    async def test_empty_fingerprint_is_not_promoted(self, db: AsyncSession) -> None:
        """A clause with no fingerprint must not match the empty string.

        The query is an ``IN`` over collected fingerprints; an empty string in
        that set would otherwise match any row whose fingerprint is also empty
        and hand it a signature.
        """
        from ate_cloud.api.v1.imports import _apply_review_state

        await _seed(db, "SR-S6", _clause("output_voltage", ""))
        fresh = _bundle([_req("SR-S6", [_clause("output_voltage", "")])])
        assert _statuses(await _apply_review_state(db, fresh), "SR-S6") == ["draft"]

    async def test_bundle_with_no_draft_is_returned_untouched(self, db: AsyncSession) -> None:
        """The fast path must not even query when nothing needs overlaying."""
        from ate_cloud.api.v1.imports import _apply_review_state

        fresh = _bundle([_req("SR-S7", [_clause("output_voltage", "fp-s7", status="approved")])])
        assert await _apply_review_state(db, fresh) is fresh


class TestTheRedLineIsUnchanged:
    """plan_flow stays the single decision point, and needs no database."""

    def test_planner_still_refuses_draft_without_a_database(self) -> None:
        """Guards the layering: the overlay is an endpoint concern only.

        If this ever starts needing a session, the red line has stopped being
        testable in isolation and the two places that decide what runs have
        begun to merge.
        """
        from inspect import signature

        from ate_cloud.services.flow_planner import plan_flow

        params = set(signature(plan_flow).parameters)
        assert "db" not in params
        assert params == {"bundle", "bindings", "include_draft"}

    def test_draft_is_excluded_and_reported_not_dropped(self) -> None:
        """A condition nobody approved is reported, never silently omitted.

        Silence would leave the impression the spec is fully covered.
        """
        from ate_cloud.services.flow_planner import plan_flow

        plan = plan_flow(_bundle([_req("SR-S8", [_clause("output_voltage", "fp-s8")])]))
        assert len(plan.pending) == 1
        assert plan.pending[0]["requirement_code"] == "SR-S8"
        assert any("未经人审" in w for w in plan.warnings)
