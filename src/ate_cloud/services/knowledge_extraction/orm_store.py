"""ORM persistence for extracted requirements/cases (task 12).

Upserts :class:`~ate_cloud.models.knowledge.TestRequirement` /
:class:`~ate_cloud.models.knowledge.TestCase` rows on natural keys so
re-running extraction creates no duplicates:

* requirements key on ``(product_code, requirement_code)``;
* cases key on ``case_code`` (global, like the task-11 ATML importer).

This module owns the SQLAlchemy queries; :mod:`service` owns orchestration.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.knowledge import SOURCE_DSL, TestCase, TestRequirement
from ate_cloud.services.atml_importer import ImportCounts
from ate_cloud.services.knowledge_extraction.dsl_extract import ExtractedPlan
from ate_cloud.services.knowledge_extraction.ids import slug


def dsl_case_code(plan_name: str, step_id: str) -> str:
    """Stable case code for a DSL step (natural key, globally unique)."""
    return f"TC-DSL-{slug(plan_name)}-{slug(step_id)}"


async def upsert_dsl_requirement(
    db: AsyncSession, product_code: str, plan: ExtractedPlan
) -> tuple[ImportCounts, str]:
    """Upsert the one requirement per DSL plan; returns (counts, row id)."""
    existing = (
        await db.execute(
            select(TestRequirement).where(
                TestRequirement.product_code == product_code,
                TestRequirement.requirement_code == plan.requirement_code,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = TestRequirement(
            id=str(uuid.uuid4()),
            product_code=product_code,
            requirement_code=plan.requirement_code,
            title=plan.title,
        )
        db.add(existing)
        counts = ImportCounts(created=1)
    else:
        existing.title = plan.title
        counts = ImportCounts(updated=1)
    existing.source = SOURCE_DSL
    existing.atml_ref = None
    existing.description = f"Test plan '{plan.plan_name}' with {len(plan.steps)} steps"
    await db.flush()
    return counts, existing.id


async def upsert_dsl_cases(
    db: AsyncSession, plan: ExtractedPlan, requirement_id: str
) -> ImportCounts:
    """Upsert one active test case per DSL step, linked to the requirement."""
    codes = [dsl_case_code(plan.plan_name, step.step_id) for step in plan.steps]
    existing = {
        c.case_code: c
        for c in (
            await db.execute(select(TestCase).where(TestCase.case_code.in_(codes)))
        ).scalars().all()
    }
    created = updated = 0
    for step in plan.steps:
        code = dsl_case_code(plan.plan_name, step.step_id)
        title = f"{plan.plan_name}: {step.title}"
        row = existing.get(code)
        if row is None:
            row = TestCase(id=str(uuid.uuid4()), case_code=code, title=title)
            db.add(row)
            created += 1
        else:
            row.title = title
            updated += 1
        row.requirement_id = requirement_id
        row.sequence_id = None
        row.step_id = step.step_id
        row.atml_ref = None
        row.status = "active"
    return ImportCounts(created, updated)


async def product_for_case(db: AsyncSession, case: TestCase) -> str | None:
    """Resolve the product code a case belongs to (via its requirement)."""
    if case.requirement_id is None:
        return None
    req = (
        await db.execute(
            select(TestRequirement).where(TestRequirement.id == case.requirement_id)
        )
    ).scalar_one_or_none()
    return req.product_code if req is not None else None


__all__ = [
    "dsl_case_code",
    "product_for_case",
    "upsert_dsl_cases",
    "upsert_dsl_requirement",
]
