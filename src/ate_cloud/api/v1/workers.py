"""Worker registry API endpoints.

Provides REST endpoints for discovering and inspecting registered
JetStreamWorker instances via the ``ate-workers`` JetStream KV bucket:

- ``GET /api/v1/workers`` — list all registered workers.
- ``GET /api/v1/workers/{worker_id}`` — get a single worker's metadata.
- ``GET /api/v1/workers/{worker_id}/health`` — get online/offline status.
- ``GET /api/v1/workers/{worker_id}/history`` — get heartbeat time-series.
- ``GET /api/v1/workers/{worker_id}/config`` — get all config entries.
- ``PUT /api/v1/workers/{worker_id}/config/{key}`` — update a config key.
- ``POST /api/v1/workers/{worker_id}/sync`` — trigger version sync.
- ``POST /api/v1/workers/{worker_id}/restart`` — trigger worker restart.
"""

import json
import os
from datetime import datetime, timezone
from typing import Annotated, Any

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
from ate_cloud.services.config_distribution import ConfigDistributionService
from ate_cloud.services.script_versioning import ScriptVersioningService
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


def _get_config_service(request: Request) -> ConfigDistributionService:
    """Dependency: get ConfigDistributionService from app state."""
    svc = getattr(request.app.state, "config_distribution", None)
    if not isinstance(svc, ConfigDistributionService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Config distribution service not available (NATS may be down)",
        )
    return svc


def _get_versioning_service(request: Request) -> ScriptVersioningService:
    """Dependency: get ScriptVersioningService from app state."""
    svc = getattr(request.app.state, "script_versioning", None)
    if not isinstance(svc, ScriptVersioningService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Script versioning service not initialized",
        )
    return svc


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


@router.post("", response_model=WorkerInfo, status_code=status.HTTP_201_CREATED)
async def register_worker(
    request: Request,
    service: Annotated[WorkerRegistryService, Depends(_get_worker_service)],
) -> WorkerInfo:
    """POST /api/v1/workers — manually register a node.

    Writes a heartbeat entry to the ``ate-workers`` KV bucket so that a
    worker appears in the registry without having sent its own heartbeat
    yet. The per-key TTL (30s) still applies — the worker must take over
    heartbeating to stay visible.

    Request body (JSON):
        - worker_id: Unique worker identifier.
        - hostname: Hostname of the machine.
        - capabilities: List of capability tags.
        - max_concurrent_tasks: Max concurrent tasks the worker accepts.

    Returns:
        WorkerInfo for the newly registered worker.
    """
    body = await request.json()
    worker_id = body.get("worker_id")
    if not worker_id or not isinstance(worker_id, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request body must contain 'worker_id' field",
        )
    hostname = body.get("hostname", "")
    capabilities = body.get("capabilities", [])
    max_concurrent_tasks = body.get("max_concurrent_tasks", 0)

    payload = json.dumps({
        "hostname": hostname,
        "capabilities": capabilities,
        "max_concurrent_tasks": max_concurrent_tasks,
        "current_tasks": 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }).encode("utf-8")

    try:
        kv = await service._get_kv()  # noqa: SLF001 — same-bucket access for registration
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"KV bucket not available: {e}",
        ) from e

    key = f"workers.{worker_id}"
    try:
        await kv.put(key, payload)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to register worker: {e}",
        ) from e

    return WorkerInfo(
        worker_id=worker_id,
        hostname=hostname,
        capabilities=capabilities,
        max_concurrent_tasks=max_concurrent_tasks,
        current_tasks=0,
        last_heartbeat=datetime.now(timezone.utc),
    )


@router.delete("/{worker_id}")
async def delete_worker(
    worker_id: str,
    service: Annotated[WorkerRegistryService, Depends(_get_worker_service)],
) -> dict[str, Any]:
    """DELETE /api/v1/workers/{worker_id} — delete a registered node.

    Removes the worker's key from the ``ate-workers`` KV bucket. The
    worker will no longer appear in the registry. If the worker is still
    running and heartbeating, it will re-register itself on the next
    heartbeat cycle.

    Returns:
        dict with status "deleted" and the worker_id.
    """
    try:
        kv = await service._get_kv()  # noqa: SLF001 — same-bucket access for deletion
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"KV bucket not available: {e}",
        ) from e

    key = f"workers.{worker_id}"
    try:
        await kv.delete(key)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to delete worker: {e}",
        ) from e

    return {"status": "deleted", "worker_id": worker_id}


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


# ---------------------------------------------------------------------------
# Config distribution endpoints
# ---------------------------------------------------------------------------


@router.get("/{worker_id}/config")
async def get_worker_config(
    worker_id: str,
    service: Annotated[ConfigDistributionService, Depends(_get_config_service)],
) -> dict[str, Any]:
    """GET /api/v1/workers/{worker_id}/config — get all config entries.

    Returns all configuration key-value pairs stored for this worker
    in the ``ate-configs`` JetStream KV bucket.
    """
    try:
        configs = await service.get_all_config(worker_id)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    return {
        "worker_id": worker_id,
        "configs": [{"key": k, "value": v} for k, v in configs.items()],
    }


@router.put("/{worker_id}/config/{key}")
async def update_worker_config(
    worker_id: str,
    key: str,
    request: Request,
    service: Annotated[ConfigDistributionService, Depends(_get_config_service)],
) -> dict[str, Any]:
    """PUT /api/v1/workers/{worker_id}/config/{key} — update a single config key.

    The request body must be JSON: ``{"value": "..."}``.
    Returns the KV revision number of the put operation.
    """
    body = await request.json()
    value = body.get("value")
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request body must contain 'value' field",
        )
    try:
        revision = await service.put_config(worker_id, key, str(value))
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    return {"revision": revision}


# ---------------------------------------------------------------------------
# Version sync + restart endpoints
# ---------------------------------------------------------------------------


@router.post("/{worker_id}/sync")
async def sync_worker(
    worker_id: str,
    service: Annotated[WorkerRegistryService, Depends(_get_worker_service)],
    versioning: Annotated[ScriptVersioningService, Depends(_get_versioning_service)],
) -> dict[str, Any]:
    """POST /api/v1/workers/{worker_id}/sync — trigger version sync.

    Tags the current script repository state and returns version info
    for all scripts. The worker is expected to pull the latest versions
    on its next heartbeat cycle.
    """
    # Verify worker exists
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

    # Collect version info for all scripts in the repo
    scripts_root = versioning.scripts_root
    synced: list[dict[str, Any]] = []
    failed: list[str] = []

    for root, _dirs, files in os.walk(scripts_root):
        # Skip .git directory
        if ".git" in root:
            continue
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, scripts_root).replace("\\", "/")
            try:
                commit_hash = versioning.get_head_commit_hash(rel_path) or ""
                last_modified = versioning.get_last_modified(rel_path)
                synced.append({
                    "script_path": rel_path,
                    "commit_hash": commit_hash,
                    "revision": 0,
                    "tagged_at": last_modified.isoformat() if last_modified else None,
                })
            except Exception:
                failed.append(rel_path)

    return {"synced": synced, "failed": failed}


@router.post("/{worker_id}/restart")
async def restart_worker(
    worker_id: str,
    request: Request,
    service: Annotated[WorkerRegistryService, Depends(_get_worker_service)],
) -> dict[str, Any]:
    """POST /api/v1/workers/{worker_id}/restart — trigger worker restart.

    Publishes a restart control message to the worker via NATS on
    ``ate.control.{worker_id}``. The worker's control subscription
    handles the restart action.
    """
    # Verify worker exists
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

    # Publish restart control message
    nc = getattr(request.app.state, "nc", None)
    if nc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NATS client not available",
        )

    subject = f"ate.control.{worker_id}"
    payload = json.dumps({"action": "restart"}).encode("utf-8")
    try:
        await nc.publish(subject, payload)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to publish restart message: {e}",
        ) from e

    return {"status": "restart signal sent", "worker_id": worker_id}
