"""ATERag bundle import API (P2).

Two-phase by design:

1. ``POST /api/v1/imports/aterag?dry_run=true`` — plan only. Returns what would
   change plus any conflicts against manual edits. Nothing is written.
2. ``POST /api/v1/imports/aterag?dry_run=false&confirm_overwrite=true`` —
   apply. ``confirm_overwrite`` is required to overwrite manual edits; without it
   those fields are left alone and reported as conflicts.

Auth: JWT with the ``aterag:import`` scope (operator roles cannot ingest).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.auth.dependencies import get_current_user, require_scopes
from ate_cloud.db import get_db
from ate_cloud.models.test_conditions import STATUS_APPROVED, TestCondition
from ate_cloud.schemas.aterag_bundle import (
    BundleModel,
    ContractMismatchError,
    assert_contract,
    contract_mismatch_report,
)
from ate_cloud.services.aterag_importer import ATERagImporter
from ate_cloud.services.aterag_script_emit import plan_gaps
from ate_cloud.services.flow_planner import plan_flow

router = APIRouter(prefix="/imports", tags=["imports"])

#: The repo's own alias for an injected session. A bare ``db: AsyncSession``
#: parameter is not a valid dependency — FastAPI raises at import time, so the
#: whole app would fail to boot rather than this route failing.
DBSession = Annotated[AsyncSession, Depends(get_db)]


def get_importer() -> ATERagImporter:
    """Importer factory (overridable in tests via dependency_overrides)."""
    return ATERagImporter()


async def _apply_review_state(db: DBSession, bundle: BundleModel) -> BundleModel:
    """Return a copy of ``bundle`` with signed conditions marked approved.

    See :func:`plan_aterag_bundle` for why. Mutating the caller's bundle in
    place would be a trap: the same object is later handed to the importer, and
    an in-place upgrade would make a dry-run look like it had persisted a
    signature that nobody made.

    Fingerprint, not id: the bundle has no database ids, and a fingerprint is
    the identity that survives a re-import, which is exactly the property
    needed to decide "is this the same condition the human signed?".
    """
    wanted: set[str] = set()
    for model in bundle.requirements:
        for clause in (*model.input_conditions, *model.output_conditions):
            if clause.status == "draft" and clause.cond_fingerprint:
                wanted.add(clause.cond_fingerprint)
    if not wanted:
        return bundle

    rows = (
        await db.execute(
            select(TestCondition.cond_fingerprint).where(
                TestCondition.cond_fingerprint.in_(wanted),
                TestCondition.status == STATUS_APPROVED,
            )
        )
    ).scalars()
    approved = {fp for fp in rows if fp}
    if not approved:
        return bundle

    for model in bundle.requirements:
        for clause in (*model.input_conditions, *model.output_conditions):
            if clause.status == "draft" and clause.cond_fingerprint in approved:
                clause.status = "approved"
    return bundle


@router.post("/aterag/plan")
async def plan_aterag_bundle(
    bundle: BundleModel,
    db: DBSession,
    _: Annotated[Any, Depends(require_scopes("aterag:import"))] = None,
) -> dict[str, Any]:
    """Plan the test sequence for a bundle. Writes nothing.

    Separate from the import because planning is a *review* question — "what
    would this test, and what is missing?" — asked before anyone commits. The
    wizard shows the answer next to the sign-off decision, so an approver can
    see that a requirement has no executable steps while they are still in the
    room to act on it.

    Planning happens server-side rather than in the browser on purpose: the
    planner needs the binding table, and shipping that table to the client to
    reproduce the sequence there would create two implementations that can
    disagree about the sequence that runs on the line.

    The bundle's approval status is overlaid with the database's
    ------------------------------------------------------------
    ATERag emits every industry-method proposal as ``draft`` and nothing ever
    rewrites the exported file, so a bundle pasted into the wizard always says
    draft — even after a human has signed the condition in this system. Planning
    straight from the bundle therefore reported the same 82 pending conditions
    forever, and the wizard's own warning ("评审签字后重新规划即可纳入") was a
    promise it could not keep: the wizard re-plans the same text from step 1, so
    a signature changed the database and nothing else.

    The overlay makes the signature mean something operationally. It is
    deliberately one-directional and evidence-based:

    * only ``draft`` may become ``approved``; a bundle that says approved is
      never downgraded, and the planner's own red line still refuses anything
      that is not approved;
    * only against a database row that exists, matches on ``cond_fingerprint``,
      and itself reads ``approved``;
    * anything uncertain — no row, mismatched fingerprint, unreadable value —
      stays ``draft``, because the cost of wrongly excluding a condition is a
      coverage gap someone will notice, while the cost of wrongly including one
      is an unauthenticated criterion on the line.

    The red line itself stays in :func:`plan_flow`, which remains the single
    place that decides what may be planned and needs no database to test.
    """
    assert_contract(bundle, strict=True)

    plan = plan_flow(await _apply_review_state(db, bundle))
    gaps = plan_gaps(plan)
    return {
        "product_code": plan.product_code,
        "segments": len(plan.segments),
        "steps": plan.step_count,
        "settle_s": round(plan.total_settle_s, 1),
        "batch_setups": plan.batch_setups,
        "unmapped": plan.unmapped,
        "gaps": [g.to_dict() for g in gaps],
        "pending": plan.pending,
        "warnings": plan.warnings,
        "plan_yaml": plan.to_yaml(),
    }


@router.post("/aterag")
async def import_aterag_bundle(
    bundle: BundleModel,
    dry_run: Annotated[bool, Query(description="只计算变更计划, 不写库")] = True,
    confirm_overwrite: Annotated[bool, Query(description="允许覆盖人工修改的字段; 否则只报冲突")] = False,
    db: Annotated[AsyncSession, Depends(get_db)] = None,  # type: ignore[assignment]
    _: Annotated[Any, Depends(require_scopes("aterag:import"))] = None,
    importer: Annotated[ATERagImporter, Depends(get_importer)] = None,  # type: ignore[assignment]
    _user: Annotated[Any, Depends(get_current_user)] = None,
) -> dict[str, Any]:
    """Import an ATERag bundle.

    Returns the same shape for both phases so the UI can render a preview and a
    result with one component. ``dry_run=true`` is the default precisely because
    a wrong import is expensive to unwind: nothing is written until the caller
    explicitly asks.
    """
    try:
        assert_contract(bundle, strict=True)
    except ContractMismatchError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=contract_mismatch_report(bundle.contract_hash),
        ) from e

    assert db is not None and importer is not None
    try:
        result = await importer.import_bundle(db, bundle, dry_run=dry_run, confirm_overwrite=confirm_overwrite)
    except Exception as e:  # noqa: BLE001
        # One transaction covers the import; a failure must not leave
        # requirements without their conditions.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"导入失败, 已回滚: {e}",
        ) from e
    return result.to_dict()
