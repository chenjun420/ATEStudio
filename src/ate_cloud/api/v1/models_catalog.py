"""型号聚合。

为什么不建 product 表
--------------------
型号不是 ATEStudio 里被管理的对象, 而是导入的产物 —— 规格书进来才产生型号。
所以这里没有 ``products`` / ``models`` 表, 而是**按 ``test_requirements`` 聚合**:
型号存在, 当且仅当它有需求。

为什么现在做
------------
P2 的上下文选择器要选型号, 而型号此前没有任何读取端点 ——
``product_configs`` 只有产品类型, 而且那张表记的是产测配置, 不是型号目录。
P4 计划补 ``GET /api/v1/models``, 那是给导入向导用的。这里提前做掉, 因为选择器
绕不过去; 顺带把它做成聚合视图, 型号总览那一页将来读同一个契约。

产品类型这一级来自 ATERag, 不在本库
--------------------------------
``test_requirements`` 只记 ``product_code``(型号), ``product_configs`` 只记
``product_type``, 两者之间**在本库里没有任何键**。映射在 ATERag:
``src/aterag/registry.py`` 里 ``ProductEntry{domain, ...}`` 按 ``model_id`` 建索引,
经它的 MCP 工具 ``list_models`` 暴露成 ``{型号: 产品类型}``。那个工具**已经**在
``aterag_agent.ALLOWED_TOOLS`` 里。

所以本端点按 D2/D6 走只读 MCP 取这一级(``services/aterag_catalog.py``), 而**不**是
去读 ATERag 的 YAML 或它的库 —— 那会造出一条没有鉴权也没有版本约束的私线。

取不到时诚实降级
--------------
ATERag 不可达时, ``product_type`` 为 ``null`` 且响应带 ``catalog_source``
与 ``catalog_warning``, 前端据此把产品类型那一级显示为「不可用」并说明原因。
不静默假装成单级 —— 规格要的是两级, 少一级必须让人看出是少了, 而不是像设计对了。

(本文件的初版把这件事写成了「映射要等 P4 的 bundle 契约才会有」。那句话是错的:
映射早就在 ATERag 侧且已经通过 MCP 暴露, 缺的只是这一侧的接线。)

关于 ``draft_conditions``
------------------------
那是**待签数**: 条件里 status 为 draft 的条数, 界面上叫「待签」。

这不是把 draft 当判据 —— 红线是「未批准的条件不得作为产测判据」, 这里正好相反:
数出还有多少条**没有**被签字。把它算成「已就绪」才是违规。签字本身走 approve
端点, 必填 ``by``; 本端点只读。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.auth.dependencies import require_scopes
from ate_cloud.db import get_db
from ate_cloud.models.knowledge import TestCase, TestRequirement
from ate_cloud.models.test_conditions import (
    OWNER_REQUIREMENT,
    STATUS_DRAFT,
    TestCondition,
)
from ate_cloud.models.user import User
from ate_cloud.services.aterag_agent import AgentUnavailableError
from ate_cloud.services.aterag_catalog import fetch_model_catalog

logger = logging.getLogger(__name__)

DBSession = Annotated[AsyncSession, Depends(get_db)]
ReadGuard = Annotated[User, Depends(require_scopes("read"))]

router = APIRouter(prefix="/models", tags=["models"])


@router.get("")
async def list_models(db: DBSession, _user: ReadGuard) -> dict[str, Any]:
    """GET /api/v1/models — 型号清单及其需求/用例/条件计数。

    条件计数用条件聚合一次算出, 而不是每个型号发一条 count 查询 —— 型号多了以后
    那是 N+1。

    ``draft_conditions`` 与 ``approved_conditions`` 都返回: 只给一个数字时, 界面
    无法回答「这个型号还有没有东西没签字」。
    """
    cond_counts = (
        select(
            TestCondition.owner_id.label("owner_id"),
            func.count()
            .filter(TestCondition.status == STATUS_DRAFT)
            .label("draft_conditions"),
            func.count()
            .filter(TestCondition.status != STATUS_DRAFT)
            .label("approved_conditions"),
        )
        .where(TestCondition.owner_type == OWNER_REQUIREMENT)
        .group_by(TestCondition.owner_id)
        .subquery()
    )

    req_counts = (
        select(
            TestRequirement.product_code.label("product_code"),
            func.count().label("requirement_count"),
        )
        .group_by(TestRequirement.product_code)
        .subquery()
    )

    case_counts = (
        select(
            TestRequirement.product_code.label("product_code"),
            func.count(TestCase.id).label("case_count"),
        )
        .select_from(TestRequirement)
        .join(TestCase, TestCase.requirement_id == TestRequirement.id)
        .group_by(TestRequirement.product_code)
        .subquery()
    )

    cond_totals = (
        select(
            TestRequirement.product_code.label("product_code"),
            func.coalesce(func.sum(cond_counts.c.draft_conditions), 0).label("draft_conditions"),
            func.coalesce(func.sum(cond_counts.c.approved_conditions), 0).label(
                "approved_conditions"
            ),
        )
        .select_from(TestRequirement)
        .join(cond_counts, cond_counts.c.owner_id == TestRequirement.id)
        .group_by(TestRequirement.product_code)
        .subquery()
    )

    stmt = (
        select(
            req_counts.c.product_code,
            req_counts.c.requirement_count,
            func.coalesce(case_counts.c.case_count, 0).label("case_count"),
            func.coalesce(cond_totals.c.draft_conditions, 0).label("draft_conditions"),
            func.coalesce(cond_totals.c.approved_conditions, 0).label("approved_conditions"),
        )
        .select_from(req_counts)
        .outerjoin(case_counts, case_counts.c.product_code == req_counts.c.product_code)
        .outerjoin(cond_totals, cond_totals.c.product_code == req_counts.c.product_code)
        .order_by(req_counts.c.product_code)
    )

    result = await db.execute(stmt)
    rows = result.all()

    # The product_type level comes from ATERag over read-only MCP. Fetched
    # before the loop because a failure there degrades every row at once, and
    # reporting one degraded response is more useful than N.
    catalog: dict[str, str] = {}
    catalog_source = "aterag-mcp"
    catalog_warning: str | None = None
    try:
        catalog = await fetch_model_catalog()
    except AgentUnavailableError as exc:
        # Not swallowed silently: the response carries the warning, and the
        # selector renders the level as unavailable *with a reason*. Returning
        # nulls and no explanation is how a missing level becomes invisible.
        logger.warning("ATERag 型号目录不可用, product_type 置空: %s", exc)
        catalog_source = "unavailable"
        catalog_warning = str(exc)

    items = [
        {
            "product_code": row.product_code,
            # None when ATERag has no opinion on this model — which is a
            # legitimate state (the requirement arrived without a registered
            # product), distinct from "ATERag could not be asked".
            "product_type": catalog.get(row.product_code),
            "requirement_count": int(row.requirement_count),
            "case_count": int(row.case_count),
            "draft_conditions": int(row.draft_conditions),
            "approved_conditions": int(row.approved_conditions),
        }
        for row in rows
    ]

    known_types = sorted({t for t in catalog.values() if t})
    return {
        "total": len(items),
        "items": items,
        # The product types ATERag knows about, so the selector's first level has
        # a source even when no local requirement happens to match one.
        "product_types": known_types,
        "catalog_source": catalog_source,
        "catalog_warning": catalog_warning,
    }
