"""Fixtures (fixture topology) API endpoints.

设计文档 §9.2 工装拓扑 API 明细：

    GET    /api/v1/fixtures                      # 工装列表
    POST   /api/v1/fixtures                      # 创建工装配置
    GET    /api/v1/fixtures/{id}                 # 工装详情
    PUT    /api/v1/fixtures/{id}                 # 更新
    DELETE /api/v1/fixtures/{id}                 # 删除
    POST   /api/v1/fixtures/{id}/validate        # 校验拓扑合法性（§8.3.5 8 类检查）
    POST   /api/v1/fixtures/{id}/duplicate       # 复制
    GET    /api/v1/fixtures/{id}/versions        # 版本历史
    POST   /api/v1/fixtures/{id}/export          # 导出 JSON/YAML

    GET    /api/v1/fixtures/templates            # 设备模板列表
    POST   /api/v1/fixtures/templates            # 创建设备模板

版本策略：每次保存（创建/更新）将 topology_data 快照写入 fixture_versions；
更新未显式指定 version 时自动递增（x.y -> x.y+1）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.fixture_topology import (
    FixtureDeviceTemplate,
    FixtureTopology,
    FixtureVersion,
)
from shared.fixture_topology import (
    FixtureTopology as SharedFixtureTopology,
    TopologyValidator,
)
from ate_cloud.schemas.fixture_topology import (
    FixtureDeviceTemplateCreate,
    FixtureDeviceTemplateResponse,
    FixtureTopologyCreate,
    FixtureTopologyResponse,
    FixtureTopologyUpdate,
    FixtureVersionResponse,
)
from shared.fixture_topology import TopologyValidator

router = APIRouter(prefix="/fixtures", tags=["fixtures"])

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


def _bump_version(version: str) -> str:
    """版本自动递增：x.y -> x.y+1（解析失败时追加 .1）。"""
    try:
        major_str, minor_str = version.split(".", 1)
        return f"{int(major_str)}.{int(minor_str) + 1}"
    except (ValueError, TypeError):
        return f"{version}.1"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("")
async def list_fixture_topologies(
    db: DBSession,
    skip: int = 0,
    limit: int = Query(100, le=500),
    product_model: str | None = None,
) -> dict[str, object]:
    """List fixture topologies with optional product_model filter.

    Args:
        skip: Records to skip.
        limit: Max records to return.
        product_model: Optional filter by target product model.
        db: Database session.

    Returns:
        dict: 'items' list and 'total' count.
    """
    stmt = select(func.count()).select_from(FixtureTopology)
    if product_model:
        stmt = stmt.where(FixtureTopology.product_model == product_model)
    total = (await db.execute(stmt)).scalar()

    query = select(FixtureTopology).order_by(FixtureTopology.updated_at.desc())
    if product_model:
        query = query.where(FixtureTopology.product_model == product_model)
    result = await db.execute(query.offset(skip).limit(limit))
    items = result.scalars().all()

    return {
        "items": [FixtureTopologyResponse.model_validate(t) for t in items],
        "total": total,
    }


# ---------------------------------------------------------------------------
# 设备模板库（静态路径须先于 /{fixture_id} 定义，避免被动态路由抢占）
# ---------------------------------------------------------------------------


@router.get("/templates", response_model=list[FixtureDeviceTemplateResponse])
async def list_device_templates(
    db: DBSession,
    category: str | None = None,
) -> list[FixtureDeviceTemplateResponse]:
    """List device templates (optional category filter).

    Args:
        db: Database session.
        category: Optional category filter (instrument / fixture / dut).

    Returns:
        List of FixtureDeviceTemplateResponse.
    """
    query = select(FixtureDeviceTemplate).order_by(FixtureDeviceTemplate.category)
    if category:
        query = query.where(FixtureDeviceTemplate.category == category)
    result = await db.execute(query)
    templates = result.scalars().all()
    return [FixtureDeviceTemplateResponse.model_validate(t) for t in templates]


@router.post("/templates", response_model=FixtureDeviceTemplateResponse, status_code=status.HTTP_201_CREATED)
async def create_device_template(
    data: FixtureDeviceTemplateCreate,
    db: DBSession,
) -> FixtureDeviceTemplateResponse:
    """Create a device template.

    Args:
        data: Template creation data.
        db: Database session.

    Returns:
        FixtureDeviceTemplateResponse.
    """
    template = FixtureDeviceTemplate(
        id=str(uuid.uuid4()),
        category=data.category,
        type=data.type,
        model=data.model,
        manufacturer=data.manufacturer,
        spec_data=data.spec_data,
        icon=data.icon,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return FixtureDeviceTemplateResponse.model_validate(template)


@router.get("/{fixture_id}", response_model=FixtureTopologyResponse)
async def get_fixture_topology(
    fixture_id: str,
    db: DBSession,
) -> FixtureTopologyResponse:
    """Get a fixture topology by id.

    Args:
        fixture_id: Fixture topology UUID.
        db: Database session.

    Returns:
        FixtureTopologyResponse.

    Raises:
        HTTPException: 404 if not found.
    """
    result = await db.execute(
        select(FixtureTopology).where(FixtureTopology.id == fixture_id)
    )
    topology = result.scalar_one_or_none()
    if not topology:
        raise HTTPException(status_code=404, detail="Fixture topology not found")
    return FixtureTopologyResponse.model_validate(topology)


@router.post("", response_model=FixtureTopologyResponse, status_code=status.HTTP_201_CREATED)
async def create_fixture_topology(
    data: FixtureTopologyCreate,
    db: DBSession,
) -> FixtureTopologyResponse:
    """Create a new fixture topology and record version 1.0.

    Args:
        data: Fixture topology creation data (topology_data validated).
        db: Database session.

    Returns:
        FixtureTopologyResponse.

    Raises:
        HTTPException: 409 if (name, version) already exists.
    """
    topology = FixtureTopology(
        id=str(uuid.uuid4()),
        name=data.name,
        version=data.version,
        description=data.description,
        product_model=data.product_model,
        topology_data=data.topology_data,
        created_by=data.created_by,
        tags=data.tags,
    )
    db.add(topology)
    try:
        await db.commit()
        await db.refresh(topology)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Fixture topology '{data.name}' version '{data.version}' already exists",
        ) from None

    # 记录版本快照
    await _record_version(db, topology, change_log="初始版本")

    return FixtureTopologyResponse.model_validate(topology)


@router.put("/{fixture_id}", response_model=FixtureTopologyResponse)
async def update_fixture_topology(
    fixture_id: str,
    data: FixtureTopologyUpdate,
    db: DBSession,
) -> FixtureTopologyResponse:
    """Update a fixture topology (auto version bump unless specified).

    Args:
        fixture_id: Fixture topology UUID.
        data: Partial update data.
        db: Database session.

    Returns:
        FixtureTopologyResponse.

    Raises:
        HTTPException: 404 if not found; 409 on (name, version) conflict.
    """
    result = await db.execute(
        select(FixtureTopology).where(FixtureTopology.id == fixture_id)
    )
    topology = result.scalar_one_or_none()
    if not topology:
        raise HTTPException(status_code=404, detail="Fixture topology not found")

    update_data = data.model_dump(exclude_unset=True)
    explicit_version = "version" in update_data
    for key, value in update_data.items():
        setattr(topology, key, value)
    if not explicit_version:
        topology.version = _bump_version(topology.version)

    try:
        await db.commit()
        await db.refresh(topology)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Fixture topology '{topology.name}' version '{topology.version}' already exists",
        ) from None

    # 记录版本快照
    await _record_version(
        db,
        topology,
        change_log=update_data.get("description") or "更新拓扑",
    )

    return FixtureTopologyResponse.model_validate(topology)


@router.delete("/{fixture_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fixture_topology(
    fixture_id: str,
    db: DBSession,
) -> None:
    """Delete a fixture topology and its version history.

    Args:
        fixture_id: Fixture topology UUID.
        db: Database session.

    Raises:
        HTTPException: 404 if not found.
    """
    result = await db.execute(
        select(FixtureTopology).where(FixtureTopology.id == fixture_id)
    )
    topology = result.scalar_one_or_none()
    if not topology:
        raise HTTPException(status_code=404, detail="Fixture topology not found")

    # 级联删除版本历史
    versions = await db.execute(
        select(FixtureVersion).where(FixtureVersion.topology_id == fixture_id)
    )
    for version in versions.scalars().all():
        await db.delete(version)

    await db.delete(topology)
    await db.commit()


# ---------------------------------------------------------------------------
# validate / duplicate / versions / export
# ---------------------------------------------------------------------------


@router.post("/{fixture_id}/validate")
async def validate_fixture_topology(
    fixture_id: str,
    db: DBSession,
    strictness: Literal["error", "warning"] = "error",
) -> dict[str, object]:
    """Validate topology legality (8 类接线校验, §8.3.5).

    Args:
        fixture_id: Fixture topology UUID.
        db: Database session.
        strictness: 'error'（默认）冲突类检查为 error；'warning' 降级。

    Returns:
        Validation result dict (valid/errors/warnings/summary).

    Raises:
        HTTPException: 404 if not found.
    """
    result = await db.execute(
        select(FixtureTopology).where(FixtureTopology.id == fixture_id)
    )
    topology = result.scalar_one_or_none()
    if not topology:
        raise HTTPException(status_code=404, detail="Fixture topology not found")

    validator = TopologyValidator(strictness=strictness)
    shared = SharedFixtureTopology.model_validate(topology.topology_data)
    validation = validator.validate(shared)
    return validation.as_dict()


@router.post("/{fixture_id}/duplicate", response_model=FixtureTopologyResponse, status_code=status.HTTP_201_CREATED)
async def duplicate_fixture_topology(
    fixture_id: str,
    db: DBSession,
) -> FixtureTopologyResponse:
    """Duplicate a fixture topology as a new record (version reset to 1.0).

    Args:
        fixture_id: Source fixture topology UUID.
        db: Database session.

    Returns:
        The duplicated FixtureTopologyResponse.

    Raises:
        HTTPException: 404 if source not found.
    """
    result = await db.execute(
        select(FixtureTopology).where(FixtureTopology.id == fixture_id)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Fixture topology not found")

    dup = FixtureTopology(
        id=str(uuid.uuid4()),
        name=f"{source.name}（副本）",
        version="1.0",
        description=source.description,
        product_model=source.product_model,
        topology_data=source.topology_data,
        created_by=source.created_by,
        tags=list(source.tags or []),
    )
    db.add(dup)
    try:
        await db.commit()
        await db.refresh(dup)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Duplicate name/version conflict"
        ) from None

    await _record_version(db, dup, change_log="复制创建")

    return FixtureTopologyResponse.model_validate(dup)


@router.get("/{fixture_id}/versions", response_model=list[FixtureVersionResponse])
async def list_fixture_versions(
    fixture_id: str,
    db: DBSession,
) -> list[FixtureVersionResponse]:
    """List version history for a fixture topology.

    Args:
        fixture_id: Fixture topology UUID.
        db: Database session.

    Returns:
        List of FixtureVersionResponse ordered by creation (newest first).
    """
    result = await db.execute(
        select(FixtureVersion)
        .where(FixtureVersion.topology_id == fixture_id)
        .order_by(FixtureVersion.created_at.desc())
    )
    versions = result.scalars().all()
    return [FixtureVersionResponse.model_validate(v) for v in versions]


@router.post("/{fixture_id}/export")
async def export_fixture_topology(
    fixture_id: str,
    db: DBSession,
    format: Literal["json", "yaml"] = "json",
    version: str | None = None,
) -> dict[str, object]:
    """Export a fixture topology as JSON or YAML (design doc §9.2).

    Args:
        fixture_id: Fixture topology UUID.
        db: Database session.
        format: Export format ('json' default, or 'yaml').
        version: Optional version string to export a historical snapshot.

    Returns:
        dict with 'format' and 'content' (str payload).

    Raises:
        HTTPException: 404 if not found; 400 on invalid format.
    """
    if format not in ("json", "yaml"):
        raise HTTPException(status_code=400, detail="Unsupported export format")

    if version is not None:
        result = await db.execute(
            select(FixtureVersion).where(
                FixtureVersion.topology_id == fixture_id,
                FixtureVersion.version == version,
            )
        )
        v = result.scalar_one_or_none()
        if not v:
            raise HTTPException(
                status_code=404,
                detail=f"Version '{version}' not found for this topology",
            )
        payload = v.topology_data
    else:
        result = await db.execute(
            select(FixtureTopology).where(FixtureTopology.id == fixture_id)
        )
        topology = result.scalar_one_or_none()
        if not topology:
            raise HTTPException(status_code=404, detail="Fixture topology not found")
        payload = topology.topology_data

    if format == "yaml":
        from shared.fixture_topology import serialize_fixture_topology

        shared = SharedFixtureTopology.model_validate(payload)
        content = serialize_fixture_topology(shared)
    else:
        import json

        content = json.dumps(payload, ensure_ascii=False, indent=2)

    return {"format": format, "content": content}


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


async def _record_version(
    db: AsyncSession,
    topology: FixtureTopology,
    change_log: str,
) -> None:
    """将当前 topology_data 快照写入版本历史。"""
    db.add(
        FixtureVersion(
            id=str(uuid.uuid4()),
            topology_id=topology.id,
            version=topology.version,
            change_log=change_log,
            topology_data=topology.topology_data,
        )
    )
    await db.commit()
