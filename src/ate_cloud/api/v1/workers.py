"""Worker registry API endpoints.

Provides REST endpoints for discovering and inspecting registered
JetStreamWorker instances via the ``ate-workers`` JetStream KV bucket:

- ``GET /api/v1/workers`` — list all registered workers.
- ``GET /api/v1/workers/{worker_id}`` — get a single worker's metadata.
- ``GET /api/v1/workers/{worker_id}/health`` — get online/offline status.
- ``GET /api/v1/workers/{worker_id}/history`` — get heartbeat time-series.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.worker_heartbeat import WorkerHeartbeat
from ate_cloud.schemas.worker import (
    WorkerHealthResponse,
    WorkerHeartbeatResponse,
    WorkerInfo,
    WorkerListResponse,
)
from ate_cloud.services.worker_registry import WorkerRegistryService

router = APIRouter(prefix="/workers", tags=["workers"])

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


def _get_worker_service(request: Request) -> WorkerRegistryService:
    """Dependency: extract or create WorkerRegistryService from app state.

    The NATS client is expected on ``app.state.nc`` (set by the lifespan).
    """
    nc = getattr(request.app.state, "nc", None)
    if nc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NATS client not available",
        )
    return WorkerRegistryService(nc)


@router.get("", response_model=WorkerListResponse)
async def list_workers(
    service: Annotated[WorkerRegistryService, Depends(_get_worker_service)],
) -> WorkerListResponse:
    """GET /api/v1/workers — list all registered workers.

    Reads all keys from the ``ate-workers`` KV bucket and decodes each
    worker's JSON metadata. Returns an empty list if the bucket does
    not exist or has no keys.
    """
    try:
        workers = await service.list_workers()
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    return WorkerListResponse(workers=workers, total=len(workers))


@router.get("/{worker_id}", response_model=WorkerInfo)
async def get_worker(
    worker_id: str,
    service: Annotated[WorkerRegistryService, Depends(_get_worker_service)],
) -> WorkerInfo:
    """GET /api/v1/workers/{worker_id} — get a single worker's metadata.

    Raises:
        HTTPException: 404 if the worker is not registered (key missing
            or bucket does not exist).
    """
    try:
        worker = await service.get_worker(worker_id)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worker '{worker_id}' not found",
        )
    return worker


@router.get("/{worker_id}/health", response_model=WorkerHealthResponse)
async def get_worker_health(
    worker_id: str,
    service: Annotated[WorkerRegistryService, Depends(_get_worker_service)],
) -> WorkerHealthResponse:
    """GET /api/v1/workers/{worker_id}/health — get worker health status.

    Returns ``online`` if the worker's KV key exists (heartbeated within
    the 30s TTL), ``offline`` otherwise. Never raises 404 — missing
    workers are reported as ``offline``.
    """
    try:
        health = await service.get_worker_health(worker_id)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    return health


@router.get("/{worker_id}/history")
async def get_worker_history(
    worker_id: str,
    db: DBSession,
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, object]:
    """GET /api/v1/workers/{worker_id}/history — get heartbeat time-series.

    Returns historical heartbeat records persisted by the
    :class:`~ate_cloud.services.health_monitor.HealthMonitorService`,
    ordered by ``recorded_at`` descending (most recent first).

    Args:
        worker_id: Unique worker identifier.
        db: Database session.
        limit: Maximum number of records to return (default 100, max 1000).

    Returns:
        dict: Dictionary with ``items`` list and ``total`` count.
    """
    result = await db.execute(
        select(WorkerHeartbeat)
        .where(WorkerHeartbeat.worker_id == worker_id)
        .order_by(WorkerHeartbeat.recorded_at.desc())
        .limit(limit)
    )
    records = result.scalars().all()
    return {
        "items": [WorkerHeartbeatResponse.model_validate(r) for r in records],
        "total": len(records),
    }
