"""Knowledge READ endpoints (frontend tasks 25 graph-browse / 26 traceability).

The GET routes are registered on the SAME :data:`router` object as the
task-12 extraction trigger (``api/v1/knowledge.py``) — this module is imported
for its registration side effect at the bottom of that module — so the
mount-level JWT guard and the auth sentinel (protected==27/anonymous==5) are
unchanged: no new router, no new mount.

- ``GET /knowledge/requirements``   paged TestRequirement list.
- ``GET /knowledge/cases``          paged TestCase list joined to requirement
  + DSL sequence_id/step_id mapping.
- ``GET /knowledge/traceability``   requirement → cases → DSL-step tree.
- (a ``GET /knowledge/graph`` {nodes, edges} endpoint used to live here and
  honest 503 when the graph backend is absent/down.

ORM reads follow the fmea.py paged-list pattern ({items,total}); the graph
 was removed with the knowledge-graph subsystem.)
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.knowledge import TestCase, TestRequirement
from ate_cloud.schemas.knowledge import (
    CasePage,
    CaseResponse,
    RequirementPage,
    TestRequirementResponse,
    TraceabilityCase,
    TraceabilityRequirement,
    TraceabilityTree,
)

logger = logging.getLogger(__name__)

# The router and the session alias used to live in ``knowledge.py``, which was the
# extraction-trigger module and went away with the knowledge-graph subsystem.
# This module is now the sole owner of the ``/knowledge`` prefix. Keeping a
# sibling module hold the router by import was indirection with no remaining
# reason: there is no second module to share it with.
router = APIRouter(prefix="/knowledge", tags=["knowledge"])

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


@router.get("/requirements", response_model=RequirementPage)
async def list_requirements(
    db: DBSession,
    product_code: Annotated[str | None, Query(description="Filter by product_code")] = None,
    source: Annotated[str | None, Query(description="Filter by source (dsl|atml|manual)")] = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> RequirementPage:
    """GET /knowledge/requirements — paged TestRequirement list."""
    count_stmt = select(func.count()).select_from(TestRequirement)
    list_stmt = select(TestRequirement).order_by(TestRequirement.created_at.desc())
    if product_code is not None:
        count_stmt = count_stmt.where(TestRequirement.product_code == product_code)
        list_stmt = list_stmt.where(TestRequirement.product_code == product_code)
    if source is not None:
        count_stmt = count_stmt.where(TestRequirement.source == source)
        list_stmt = list_stmt.where(TestRequirement.source == source)

    total = (await db.execute(count_stmt)).scalar() or 0
    rows = (await db.execute(list_stmt.offset(skip).limit(limit))).scalars().all()
    return RequirementPage(
        items=[TestRequirementResponse.model_validate(r) for r in rows], total=total
    )


@router.get("/cases", response_model=CasePage)
async def list_cases(
    db: DBSession,
    requirement_id: Annotated[str | None, Query(description="Filter by requirement_id")] = None,
    product_code: Annotated[str | None, Query(description="Filter via requirement product")] = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> CasePage:
    """GET /knowledge/cases — paged TestCase list joined to its requirement.

    Each row carries the requirement link plus the denormalized
    product_code/requirement_code and the DSL sequence_id/step_id mapping so
    the matrix renders requirement→case→DSL step in one response.
    """
    count_stmt = select(func.count()).select_from(TestCase)
    list_stmt = (
        select(TestCase, TestRequirement)
        .outerjoin(TestRequirement, TestCase.requirement_id == TestRequirement.id)
        .order_by(TestCase.created_at.desc())
    )
    if requirement_id is not None:
        count_stmt = count_stmt.where(TestCase.requirement_id == requirement_id)
        list_stmt = list_stmt.where(TestCase.requirement_id == requirement_id)
    if product_code is not None:
        # product lives on the requirement (inner join here excludes orphans).
        count_stmt = count_stmt.join(
            TestRequirement, TestCase.requirement_id == TestRequirement.id
        ).where(TestRequirement.product_code == product_code)
        list_stmt = list_stmt.where(TestRequirement.product_code == product_code)

    total = (await db.execute(count_stmt)).scalar() or 0
    rows = (await db.execute(list_stmt.offset(skip).limit(limit))).all()
    items: list[CaseResponse] = []
    for case, requirement in rows:
        item = CaseResponse.model_validate(case)
        if requirement is not None:
            item.product_code = requirement.product_code
            item.requirement_code = requirement.requirement_code
        items.append(item)
    return CasePage(items=items, total=total)


@router.get("/traceability", response_model=TraceabilityTree)
async def get_traceability(
    db: DBSession,
    product_code: Annotated[str | None, Query(description="Filter requirements by product")] = None,
) -> TraceabilityTree:
    """GET /knowledge/traceability — requirement → cases → DSL-step tree.

    Cases without a requirement land in ``unlinked_cases`` so matrix gaps
    (cases ingested before their requirement) stay visible.
    """
    req_stmt = select(TestRequirement).order_by(TestRequirement.requirement_code)
    if product_code is not None:
        req_stmt = req_stmt.where(TestRequirement.product_code == product_code)
    requirements = (await db.execute(req_stmt)).scalars().all()

    case_stmt = select(TestCase).order_by(TestCase.case_code)
    if product_code is not None:
        case_stmt = case_stmt.join(
            TestRequirement, TestCase.requirement_id == TestRequirement.id
        ).where(TestRequirement.product_code == product_code)
    cases = (await db.execute(case_stmt)).scalars().all()

    by_requirement: dict[str, list[TestCase]] = {}
    unlinked: list[TraceabilityCase] = []
    for case in cases:
        if case.requirement_id is None:
            unlinked.append(TraceabilityCase.model_validate(case))
        else:
            by_requirement.setdefault(case.requirement_id, []).append(case)

    tree_requirements = [
        TraceabilityRequirement(
            id=req.id,
            requirement_code=req.requirement_code,
            title=req.title,
            source=req.source,  # type: ignore[arg-type]
            cases=[
                TraceabilityCase.model_validate(c)
                for c in by_requirement.get(req.id, [])
            ],
        )
        for req in requirements
    ]
    return TraceabilityTree(
        product_code=product_code,
        requirements=tree_requirements,
        unlinked_cases=unlinked,
    )


# ``GET /knowledge/graph`` was removed with the knowledge-graph subsystem. It
# returned an honest 503 on every deployment, because the backend it needed was
# never provisioned — a permanently red endpoint that told an operator nothing
# except that a feature they had never been given did not work.


# P5: condition read/review endpoints register on the same router, imported
# for the side effect. Last on purpose — FastAPI matches routes in registration
# order, and keeping the additions after the existing ones means a future
# "/conditions/{id}" style path cannot shadow an existing route by accident.
from . import knowledge_conditions as _knowledge_conditions  # noqa: E402,F401

__all__: list[str] = []
