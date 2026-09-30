"""Test-condition read + review endpoints (P5 front end).

Mounted on the SAME :data:`router` as the other knowledge reads, so the
mount-level JWT guard and the auth sentinel (protected==28/anonymous==5) are
unchanged — no new router, no new mount.

- ``GET  /knowledge/conditions``          paged, filterable by requirement,
                                          side, kind and — critically — status.
- ``GET  /knowledge/conditions/summary``  per-requirement counts by status.
                                          The review screen's work list.
- ``POST /knowledge/conditions/approve``  sign off a requirement's drafts.

Why the summary endpoint exists
-------------------------------
The review screen has to answer "what still needs a human?" before it shows
anything. Fetching every condition to count them would pull the whole product
(PA601: 289 clauses) on every page load, and the answer it produces is a dozen
numbers. The summary is the cheap question, asked first.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import HTTPException, Query, status
from sqlalchemy import func, select, update

from ate_cloud.models.knowledge import TestRequirement
from ate_cloud.models.test_conditions import STATUS_APPROVED, TestCondition
from ate_cloud.schemas.test_conditions import (
    ConditionPage,
    ConditionReviewRequest,
    ConditionReviewResult,
    TestConditionResponse,
)

# DBSession = Annotated[AsyncSession, Depends(get_db)] — the repo's own alias.
# A bare ``db: AsyncSession`` is not a valid Pydantic field, and FastAPI raises
# at import time rather than at call time, so the app would fail to boot.
from .knowledge import DBSession, router

logger = logging.getLogger(__name__)


@router.get("/conditions", response_model=ConditionPage)
async def list_conditions(
    db: DBSession,
    requirement_id: Annotated[str | None, Query(description="只取该需求下的条件")] = None,
    product_code: Annotated[str | None, Query(description="按产品过滤(经需求关联)")] = None,
    side: Annotated[str | None, Query(description="input | output")] = None,
    kind: Annotated[str | None, Query(description="条件种类")] = None,
    status_filter: Annotated[
        str | None, Query(alias="status", description="draft | approved")
    ] = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> ConditionPage:
    """GET /knowledge/conditions — paged condition list.

    ``status`` is the filter the review screen leads with: the work list is
    "what is still unapproved", and a reviewer who has to page through approved
    rows to find drafts will stop using the filter.
    """
    count_stmt = select(func.count()).select_from(TestCondition)
    list_stmt = select(TestCondition).order_by(
        TestCondition.owner_id, TestCondition.side, TestCondition.id
    )

    if requirement_id is not None:
        count_stmt = count_stmt.where(TestCondition.owner_id == requirement_id)
        list_stmt = list_stmt.where(TestCondition.owner_id == requirement_id)
    if product_code is not None:
        # Conditions hang off requirements, so a product filter has to go
        # through the owner. Using owner_id against product_code would silently
        # return nothing — a wrong answer, not an error.
        owner_ids = select(TestRequirement.id).where(
            TestRequirement.product_code == product_code
        )
        count_stmt = count_stmt.where(TestCondition.owner_id.in_(owner_ids))
        list_stmt = list_stmt.where(TestCondition.owner_id.in_(owner_ids))
    if side is not None:
        count_stmt = count_stmt.where(TestCondition.side == side)
        list_stmt = list_stmt.where(TestCondition.side == side)
    if kind is not None:
        count_stmt = count_stmt.where(TestCondition.kind == kind)
        list_stmt = list_stmt.where(TestCondition.kind == kind)
    if status_filter is not None:
        count_stmt = count_stmt.where(TestCondition.status == status_filter)
        list_stmt = list_stmt.where(TestCondition.status == status_filter)

    total = (await db.execute(count_stmt)).scalar() or 0
    rows = (await db.execute(list_stmt.offset(skip).limit(limit))).scalars().all()

    # One extra query for the denormalized display fields, rather than a join
    # that would multiply rows when a requirement has many conditions.
    req_ids = {r.owner_id for r in rows if r.owner_type == "requirement"}
    reqs: dict[str, TestRequirement] = {}
    if req_ids:
        fetched = (
            await db.execute(select(TestRequirement).where(TestRequirement.id.in_(req_ids)))
        ).scalars().all()
        reqs = {r.id: r for r in fetched}

    items: list[TestConditionResponse] = []
    for row in rows:
        item = TestConditionResponse.model_validate(row)
        req: TestRequirement | None = reqs.get(row.owner_id)
        if req is not None:
            item.requirement_code = req.requirement_code
            item.requirement_title = req.title
            item.section_path = req.section_path
        items.append(item)
    return ConditionPage(items=items, total=total)


@router.get("/conditions/summary")
async def condition_summary(
    db: DBSession,
    product_code: Annotated[str | None, Query(description="按产品过滤(经需求关联)")] = None,
) -> dict[str, object]:
    """GET /knowledge/conditions/summary — the review work list.

    Per requirement: how many conditions are approved, how many are still
    draft, and whether the requirement itself is stale (dropped from a newer
    spec revision). A stale requirement's conditions are kept for traceability
    but must not be presented as pending work — a reviewer approving conditions
    for a requirement that no longer exists is doing work that cannot ship.
    """
    owner_ids = select(TestRequirement.id)
    req_stmt = select(
        TestRequirement.id,
        TestRequirement.requirement_code,
        TestRequirement.title,
        TestRequirement.section_path,
        TestRequirement.status,
    )
    if product_code is not None:
        owner_ids = owner_ids.where(TestRequirement.product_code == product_code)
        req_stmt = req_stmt.where(TestRequirement.product_code == product_code)

    req_rows = (await db.execute(req_stmt)).all()
    if not req_rows:
        return {"items": [], "total": 0, "pending_total": 0}

    counts_stmt = (
        select(
            TestCondition.owner_id,
            TestCondition.status,
            func.count(TestCondition.id),
        )
        .where(TestCondition.owner_id.in_(owner_ids))
        .group_by(TestCondition.owner_id, TestCondition.status)
    )
    counts: dict[str, dict[str, int]] = {}
    for owner_id, st, n in (await db.execute(counts_stmt)).all():
        counts.setdefault(owner_id, {})[st] = int(n)

    items = []
    pending_total = 0
    for rid, code, title, section, req_status in req_rows:
        per = counts.get(rid, {})
        draft = per.get("draft", 0)
        approved = per.get("approved", 0)
        if req_status == "stale":
            # Kept in the payload (traceability) but not counted as work.
            draft = 0
        pending_total += draft
        items.append(
            {
                "requirement_id": rid,
                "requirement_code": code,
                "title": title,
                "section_path": section,
                "requirement_status": req_status,
                "draft": draft,
                "approved": approved,
                "total": draft + approved,
            }
        )
    return {
        "items": items,
        "total": len(items),
        "pending_total": pending_total,
    }


@router.post(
    "/conditions/approve",
    response_model=ConditionReviewResult,
    status_code=status.HTTP_200_OK,
)
async def approve_conditions(
    payload: ConditionReviewRequest,
    db: DBSession,
) -> ConditionReviewResult:
    """POST /knowledge/conditions/approve — sign off a requirement's drafts.

    Approval is scoped to one requirement and recorded with ``by``. Both matter:
    a bulk "approve everything draft" would sign conditions extracted for spec
    clauses the reviewer never opened, and an approval with no name attached
    cannot be audited — which makes it indistinguishable from no approval at
    all, the state everything starts in.

    Idempotent: re-approving an already-approved condition is a no-op counted in
    ``skipped``, not an error. A reviewer clicking twice must not get a failure.
    """
    req = (
        await db.execute(
            select(TestRequirement).where(TestRequirement.id == payload.requirement_id)
        )
    ).scalar_one_or_none()
    if req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="需求不存在"
        )
    if req.status == "stale":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该需求已在新版规格书中消失(stale), 其条件不再可批准",
        )

    stmt = (
        update(TestCondition)
        # .values() 不是可选的: 没有它, SQLAlchemy 生成的是"把所有列重置为
        # 默认值"的 UPDATE。TestCondition.status 的 Python 侧默认值恰好是
        # "draft", 于是"批准"会把整行打回初始状态 —— approve_id、value、
        # text 全部被清空, 而 rowcount 仍报 1, 看上去成功了。
        # 这种 bug 不会报错, 只在几个月后表现为"签字信息莫名其妙没了"。
        .values(status=STATUS_APPROVED)
        .where(
            TestCondition.owner_id == payload.requirement_id,
            TestCondition.owner_type == "requirement",
            TestCondition.status == "draft",
        )
        # "fetch" 显式刷新 identity map: 否则已加载进 session 的对象仍持有
        # 旧 status, 接口刚批准完再序列化同批条件时返回的仍是 draft, 评审
        # 界面显示"已批准"而刷新后变回待审, 签字像是没生效。
        .execution_options(synchronize_session="fetch")
    )
    if payload.condition_ids:
        stmt = stmt.where(TestCondition.id.in_(payload.condition_ids))
    result = await db.execute(stmt)
    approved = int(getattr(result, "rowcount", 0) or 0)

    total = (
        await db.execute(
            select(func.count())
            .select_from(TestCondition)
            .where(
                TestCondition.owner_id == payload.requirement_id,
                TestCondition.owner_type == "requirement",
                TestCondition.status == "draft",
            )
        )
    ).scalar() or 0
    await db.commit()

    skipped = int(total)
    message = (
        f"已批准 {approved} 条条件 (签字人: {payload.by})"
        if approved
        else f"没有可批准的 draft 条件 (签字人: {payload.by})"
    )
    logger.info("approve conditions req=%s by=%s n=%d", req.requirement_code, payload.by, approved)
    return ConditionReviewResult(
        requirement_id=payload.requirement_id,
        approved=approved,
        skipped=skipped,
        by=payload.by,
        message=message,
    )

