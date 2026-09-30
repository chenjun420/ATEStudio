"""厂区 / 工位 / 工位故障案例 的 CRUD。

三处不肯将就的行为
------------------
1. **有工位的厂区删不掉**, 返回 409 而不是级联删掉一条产线的故障史。孤儿工位在
   上下文选择器里永远看不见, 那比「请先清空产线」是更糟的失败。
2. **同厂区工位编码重复** 返回 409 并说明后果。重复会把一个工位的故障史劈成两
   半, 而「该工位无记录」与「该工位没出过故障」在界面上完全一样。
3. **案例写不进去向量库时不拒绝写入**。案例本身是真记录, 关系表才是真源; 让检索
   侧的问题变成写侧的数据丢失是本末倒置。响应里如实说明有几条没索引上。

术语
----
全局「节点」改称「工位」;「阶段 ↔ 工位」对应, 一个测试阶段就是一台工位上的一次
执行。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.auth.dependencies import require_scopes
from ate_cloud.db import get_db
from ate_cloud.models.station import Plant, Station, StationFaultCase
from ate_cloud.models.user import User
from ate_cloud.schemas.station import (
    FaultCaseCreate,
    FaultCaseListResponse,
    FaultCaseResponse,
    FaultCaseUpdate,
    PlantCreate,
    PlantListResponse,
    PlantResponse,
    ReindexResponse,
    StationCreate,
    StationListResponse,
    StationResponse,
    StationUpdate,
)
from ate_cloud.services.fault_case_index import FaultCaseIndexer

logger = logging.getLogger(__name__)

plants_router = APIRouter(prefix="/plants", tags=["stations"])
stations_router = APIRouter(prefix="/stations", tags=["stations"])
fault_cases_router = APIRouter(prefix="/fault-cases", tags=["fault-cases"])

DBSession = Annotated[AsyncSession, Depends(get_db)]
ReadGuard = Annotated[User, Depends(require_scopes("read"))]
WriteGuard = Annotated[User, Depends(require_scopes("write"))]


def _indexer(request: Request) -> FaultCaseIndexer:
    """Build an indexer from whatever the lifespan managed to start.

    Both dependencies are optional by design elsewhere (failure indexing degrades
    to a logged skip), so this must cope with either being absent rather than
    assume the happy path.
    """
    state = request.app.state
    return FaultCaseIndexer(
        qdrant_client=getattr(state, "qdrant_client", None),
        embedding_service=getattr(state, "embedding_service", None),
    )


# ── 厂区 ──────────────────────────────────────────────────────────────────


@plants_router.get("", response_model=PlantListResponse)
async def list_plants(db: DBSession, _user: ReadGuard) -> PlantListResponse:
    """GET /api/v1/plants — 上下文选择器的顶层选项。

    Includes ``station_count`` because the UI must be able to say why a plant
    cannot be deleted *before* the operator tries.
    """
    rows = (
        (
            await db.execute(
                select(Plant, func.count(Station.id))
                .outerjoin(Station, Station.plant_id == Plant.id)
                .group_by(Plant.id)
                .order_by(Plant.code)
            )
        )
        .tuples()
        .all()
    )
    items = [
        PlantResponse(
            **{
                **{c: getattr(p, c) for c in ("id", "code", "name", "parent_id", "is_active")},
                "station_count": n,
                "created_at": p.created_at,
                "updated_at": p.updated_at,
            }
        )
        for p, n in rows
    ]
    return PlantListResponse(total=len(items), items=items)


@plants_router.post("", response_model=PlantResponse, status_code=status.HTTP_201_CREATED)
async def create_plant(data: PlantCreate, db: DBSession, _user: WriteGuard) -> PlantResponse:
    """POST /api/v1/plants"""
    if data.parent_id and not await db.get(Plant, data.parent_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"上级厂区 {data.parent_id} 不存在")

    plant = Plant(code=data.code, name=data.name, parent_id=data.parent_id)
    db.add(plant)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"厂区编码 {data.code} 已存在"
        ) from exc
    await db.refresh(plant)
    return PlantResponse(station_count=0, **{c: getattr(plant, c) for c in
                        ("id", "code", "name", "parent_id", "is_active",
                         "created_at", "updated_at")})


@plants_router.delete("/{plant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plant(plant_id: str, db: DBSession, _user: WriteGuard) -> None:
    """DELETE /api/v1/plants/{id} — 仅当厂区为空。

    The refusal is the point. Cascading here would delete a line's stations and
    their fault history as a side effect of reorganising a grouping, and the
    database's own NOT NULL on ``stations.plant_id`` would block the quiet
    alternative anyway.
    """
    plant = await db.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="厂区不存在")

    count = (await db.execute(
        select(func.count()).select_from(Station).where(Station.plant_id == plant_id)
    )).scalar_one()
    if count:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"厂区 {plant.code} 下还有 {count} 个工位, 不能删除。"
                "删除厂区不应连带删掉一条产线的故障历史; 请先移走或删除这些工位。"
            ),
        )
    await db.delete(plant)
    await db.commit()


# ── 工位 ──────────────────────────────────────────────────────────────────


async def _load_station_row(db: AsyncSession, station_id: str) -> Any:
    row = (
        await db.execute(
            select(Station, Plant, func.count(StationFaultCase.id))
            .join(Plant, Plant.id == Station.plant_id)
            .outerjoin(StationFaultCase, StationFaultCase.station_id == Station.id)
            .where(Station.id == station_id)
            .group_by(Station.id, Plant.id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="工位不存在")
    return row


def _station_response(station: Station, plant: Plant | None, case_count: int) -> StationResponse:
    return StationResponse(
        **{
            **{c: getattr(station, c) for c in
               ("id", "plant_id", "code", "name", "attributes", "is_active",
                "created_at", "updated_at")},
            "plant_code": plant.code if plant else None,
            "plant_name": plant.name if plant else None,
            "fault_case_count": case_count,
        }
    )


@stations_router.get("", response_model=StationListResponse)
async def list_stations(
    db: DBSession,
    _user: ReadGuard,
    plant_id: Annotated[str | None, Query(description="按厂区过滤")] = None,
    include_inactive: Annotated[bool, Query(description="包含已停用工位")] = False,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> StationListResponse:
    """GET /api/v1/stations — 工位管理列表 / 上下文选择器的第二级。"""
    stmt = (
        select(Station, Plant, func.count(StationFaultCase.id))
        .join(Plant, Plant.id == Station.plant_id)
        .outerjoin(StationFaultCase, StationFaultCase.station_id == Station.id)
        .group_by(Station.id, Plant.id)
        .order_by(Plant.code, Station.code)
    )
    if plant_id:
        stmt = stmt.where(Station.plant_id == plant_id)
    if not include_inactive:
        stmt = stmt.where(Station.is_active.is_(True))

    total = (
        await db.execute(
            select(func.count()).select_from(stmt.subquery())
        )
    ).scalar_one()
    rows = (await db.execute(stmt.offset(skip).limit(limit))).all()
    return StationListResponse(
        total=total, items=[_station_response(s, p, n) for s, p, n in rows]
    )


@stations_router.post("", response_model=StationResponse, status_code=status.HTTP_201_CREATED)
async def create_station(
    data: StationCreate, db: DBSession, _user: WriteGuard
) -> StationResponse:
    """POST /api/v1/stations"""
    if not await db.get(Plant, data.plant_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"厂区 {data.plant_id} 不存在")

    station = Station(
        plant_id=data.plant_id, code=data.code, name=data.name, attributes=data.attributes
    )
    db.add(station)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"厂区 {data.plant_id} 下已有编码为 {data.code} 的工位。"
                "重复编码会把该工位的故障历史劈成两半, 使「无记录」与「未出过故障」"
                "无法区分。"
            ),
        ) from exc
    await db.refresh(station)
    plant = await db.get(Plant, data.plant_id)
    return _station_response(station, plant, 0)


@stations_router.get("/{station_id}", response_model=StationResponse)
async def get_station(station_id: str, db: DBSession, _user: ReadGuard) -> StationResponse:
    """GET /api/v1/stations/{id}"""
    station, plant, n = await _load_station_row(db, station_id)
    return _station_response(station, plant, n)


@stations_router.patch("/{station_id}", response_model=StationResponse)
async def update_station(
    station_id: str, data: StationUpdate, db: DBSession, _user: WriteGuard
) -> StationResponse:
    """PATCH /api/v1/stations/{id}

    ``code`` and ``plant_id`` are deliberately not updatable. Both are part of
    what identifies a station in reports and in history; making them mutable
    would mean the same physical station appears under two identities.
    """
    station = await db.get(Station, station_id)
    if station is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="工位不存在")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(station, field, value)
    await db.commit()
    await db.refresh(station)
    station, plant, n = await _load_station_row(db, station_id)
    return _station_response(station, plant, n)


@stations_router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_station(
    station_id: str, db: DBSession, request: Request, _user: WriteGuard
) -> None:
    """DELETE /api/v1/stations/{id} — 连同其故障案例一起删除。

    The opposite call to plant deletion, and the right one: a fault case has no
    meaning without the station it happened on, whereas a plant's stations are
    still real hardware somewhere.
    """
    station = await db.get(Station, station_id)
    if station is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="工位不存在")

    case_ids = (
        await db.execute(
            select(StationFaultCase.id).where(StationFaultCase.station_id == station_id)
        )
    ).scalars().all()

    indexer = _indexer(request)
    await db.delete(station)
    await db.commit()
    for case_id in case_ids:
        await indexer.remove_case(case_id)


# ── 工位故障案例 ──────────────────────────────────────────────────────────


async def _load_case(db: AsyncSession, case_id: str) -> Any:
    row = (
        await db.execute(
            select(StationFaultCase, Station, Plant)
            .join(Station, Station.id == StationFaultCase.station_id)
            .join(Plant, Plant.id == Station.plant_id)
            .where(StationFaultCase.id == case_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="故障案例不存在")
    return row


def _case_response(case: StationFaultCase, station: Station, plant: Plant) -> FaultCaseResponse:
    return FaultCaseResponse(
        **{
            **{c: getattr(case, c) for c in
               ("id", "station_id", "product_code", "symptom", "cause", "effect", "fix",
                "fix_verified", "severity", "occurrence", "detection", "rpn",
                "source_diagnosis_id", "occurred_at", "created_at", "updated_at")},
            "station_code": station.code,
            "station_name": station.name,
            "plant_id": plant.id,
        }
    )


@fault_cases_router.get("", response_model=FaultCaseListResponse)
async def list_fault_cases(
    db: DBSession,
    _user: ReadGuard,
    station_id: Annotated[str | None, Query(description="按工位过滤")] = None,
    plant_id: Annotated[str | None, Query(description="按厂区过滤")] = None,
    product_code: Annotated[str | None, Query()] = None,
    verified_only: Annotated[
        bool, Query(description="只要已验证的措施 —— 建议层引用措施时用这个")
    ] = False,
    order_by: Annotated[str, Query(description="rpn 或 occurred_at", pattern="^(rpn|occurred_at)$")] = "occurred_at",
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> FaultCaseListResponse:
    """GET /api/v1/fault-cases — 工位故障案例库。

    ``verified_only`` exists because the suggestion layer must only quote
    remedies someone confirmed. Making that a query parameter rather than a
    convention means a future caller cannot forget it by accident.
    """
    # Filters are collected once and applied to both queries. Deriving the count
    # from ``stmt.subquery()`` and then appending WHERE clauses that reference
    # the outer tables produces a cartesian product between the subquery and the
    # main FROM — so the count silently disagrees with the rows returned, which
    # is how a paginated list ends up claiming more pages than exist.
    filters = []
    if station_id:
        filters.append(StationFaultCase.station_id == station_id)
    if plant_id:
        filters.append(Station.plant_id == plant_id)
    if product_code:
        filters.append(StationFaultCase.product_code == product_code)
    if verified_only:
        filters.append(StationFaultCase.fix_verified.is_(True))

    stmt = (
        select(StationFaultCase, Station, Plant)
        .join(Station, Station.id == StationFaultCase.station_id)
        .join(Plant, Plant.id == Station.plant_id)
        .where(*filters)
    )
    count_stmt = (
        select(func.count())
        .select_from(StationFaultCase)
        .join(Station, Station.id == StationFaultCase.station_id)
        .join(Plant, Plant.id == Station.plant_id)
        .where(*filters)
    )

    order = (
        StationFaultCase.rpn.desc().nullslast()
        if order_by == "rpn"
        else StationFaultCase.occurred_at.desc()
    )
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(stmt.order_by(order).offset(skip).limit(limit))).all()
    return FaultCaseListResponse(
        total=total, items=[_case_response(c, s, p) for c, s, p in rows]
    )


@fault_cases_router.post(
    "", response_model=FaultCaseResponse, status_code=status.HTTP_201_CREATED
)
async def create_fault_case(
    data: FaultCaseCreate, db: DBSession, request: Request, _user: WriteGuard
) -> FaultCaseResponse:
    """POST /api/v1/fault-cases

    A case that cannot be indexed is still created — see the module docstring.
    """
    station = await db.get(Station, data.station_id)
    if station is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"工位 {data.station_id} 不存在")

    case = StationFaultCase(
        station_id=data.station_id,
        product_code=data.product_code,
        symptom=data.symptom,
        cause=data.cause,
        effect=data.effect,
        fix=data.fix,
        # Default false in the schema too; set explicitly so that the intent is
        # visible at the point where it matters.
        fix_verified=data.fix_verified,
        severity=data.severity,
        occurrence=data.occurrence,
        detection=data.detection,
        rpn=data.rpn,
        occurred_at=data.occurred_at,
    )
    db.add(case)
    await db.commit()
    await db.refresh(case)

    plant = await db.get(Plant, station.plant_id)
    if plant is None:
        # The foreign key makes this unreachable, so reaching it means the data
        # is already inconsistent. Failing loudly beats returning a case whose
        # plant_id is null and whose payload filters would silently match
        # everything.
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"工位 {station.id} 的厂区 {station.plant_id} 不存在 —— 数据不一致",
        )
    indexed = await _indexer(request).index_case(case, plant_id=station.plant_id)
    if not indexed:
        logger.warning(
            "Fault case %s created but not indexed; retrieval will not see it "
            "until POST /api/v1/fault-cases/reindex",
            case.id,
        )
    return _case_response(case, station, plant)


@fault_cases_router.get("/{case_id}", response_model=FaultCaseResponse)
async def get_fault_case(case_id: str, db: DBSession, _user: ReadGuard) -> FaultCaseResponse:
    """GET /api/v1/fault-cases/{id}"""
    case, station, plant = await _load_case(db, case_id)
    return _case_response(case, station, plant)


@fault_cases_router.patch("/{case_id}", response_model=FaultCaseResponse)
async def update_fault_case(
    case_id: str, data: FaultCaseUpdate, db: DBSession, request: Request, _user: WriteGuard
) -> FaultCaseResponse:
    """PATCH /api/v1/fault-cases/{id}

    Re-indexes on every change: ``symptom`` and ``cause`` are what the vector
    encodes, so editing the fix alone still needs the payload refreshed for
    ``fix_verified`` to stay filterable.
    """
    case = await db.get(StationFaultCase, case_id)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="故障案例不存在")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(case, field, value)
    await db.commit()
    await db.refresh(case)

    case, station, plant = await _load_case(db, case_id)
    await _indexer(request).index_case(case, plant_id=plant.id)
    return _case_response(case, station, plant)


@fault_cases_router.delete("/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fault_case(
    case_id: str, db: DBSession, request: Request, _user: WriteGuard
) -> None:
    """DELETE /api/v1/fault-cases/{id} — 同时移除其向量。

    Deleting the row but leaving the vector would let retrieval quote a case
    that no longer exists, which is the worst of both: the operator sees a
    confident answer citing history that has been deleted.
    """
    case = await db.get(StationFaultCase, case_id)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="故障案例不存在")
    await db.delete(case)
    await db.commit()
    await _indexer(request).remove_case(case_id)


@fault_cases_router.post("/reindex", response_model=ReindexResponse)
async def reindex_fault_cases(
    db: DBSession, request: Request, _user: WriteGuard
) -> ReindexResponse:
    """POST /api/v1/fault-cases/reindex — 全量重建向量索引。

    Exists because the first cases were entered before indexing did, and because
    a model or width change invalidates everything already indexed. Both cases
    are silent if you do not look: the relational table looks complete and the
    retrievable set stays empty.
    """
    rows = (
        await db.execute(
            select(StationFaultCase, Station.plant_id).join(
                Station, Station.id == StationFaultCase.station_id
            )
        )
    ).all()
    indexed, skipped = await _indexer(request).reindex_all(
        [(c, p) for c, p in rows]
    )
    detail = (
        "全部案例已重新索引"
        if not skipped
        else f"{skipped} 条未索引 —— 缺 embedding 服务或向量库不可用"
    )
    return ReindexResponse(indexed=indexed, skipped=skipped, detail=detail)
