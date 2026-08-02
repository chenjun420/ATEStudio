"""Dashboard API endpoints — production overview aggregation.

Provides:
- ``GET /api/v1/dashboard/summary`` — active worker count, today's
  execution total, pass rate, and fault count in a single response.
- ``GET /api/v1/dashboard/stations`` — per-station (worker) status list
  with online/offline, capabilities, and current task load.
- ``GET /api/v1/dashboard/faults`` — fault rate trend (hourly buckets
  for the last 24h) and Top-5 fault Pareto (by error category).
- ``GET /api/v1/dashboard/executions`` — today's execution count broken
  down by status (PENDING / RUNNING / COMPLETED / FAILED / ABORTED).

Data sources:
- Execution records from the SQLAlchemy DB (executions table).
- Active workers from the JetStream ``ate-workers`` KV bucket via
  :class:`~ate_cloud.services.worker_registry.WorkerRegistryService`.
- Fault records from Qdrant ``ate_failures`` collection (if available).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.execution import Execution
from ate_cloud.services.worker_registry import WorkerRegistryService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]

# How many hours of fault data to include in the trend.
_FAULT_TREND_HOURS = 24
# Top-N faults for the Pareto chart.
_TOP_FAULTS_LIMIT = 5


def _get_worker_service(request: Request) -> WorkerRegistryService | None:
    """Dependency: extract WorkerRegistryService from app state.

    Returns ``None`` if NATS is not connected — callers handle the
    graceful-degradation path.
    """
    nc = getattr(request.app.state, "nc", None)
    if nc is None:
        return None
    return WorkerRegistryService(nc)


def _get_failure_indexer(request: Request) -> Any:
    """Dependency: extract FailureIndexer from app state.

    Returns ``None`` if Qdrant/failure indexer is not initialized
    (graceful degradation — dashboard works without Qdrant).
    """
    return getattr(request.app.state, "failure_indexer", None)


def _utc_now() -> datetime:
    """Return current UTC time (timezone-aware)."""
    return datetime.now(UTC)


def _today_start() -> datetime:
    """Return the start of today (00:00 UTC) as a timezone-aware datetime."""
    now = _utc_now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _query_qdrant_top_faults(
    indexer: Any,
    limit: int,
) -> list[dict[str, Any]]:
    """Query Qdrant for top-N fault categories via scroll.

    Uses ``scroll()`` to iterate all failure points (no vector search
    needed — we just want frequency counts by category). Returns a list
    of ``{"category": str, "count": int}`` sorted descending.

    Returns an empty list if Qdrant is unavailable.
    """
    if indexer is None:
        return []
    try:
        qdrant_client = indexer._qdrant_client  # noqa: SLF001
        collection_name = indexer._collection_name  # noqa: SLF001


        # Scroll through all points (batch of 100 at a time)
        all_payloads: list[dict[str, Any]] = []
        offset: str | int | None = None
        while True:
            results, offset = qdrant_client.scroll(
                collection_name=collection_name,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in results:
                if point.payload:
                    all_payloads.append(point.payload)
            if offset is None:
                break

        # Count by failed_step_name (or failed_step_id as fallback)
        category_counts: Counter[str] = Counter()
        for payload in all_payloads:
            category = (
                payload.get("failed_step_name")
                or payload.get("failed_step_id")
                or payload.get("error_message")
                or "Unknown"
            )
            category_counts[str(category)] += 1

        top = category_counts.most_common(limit)
        return [{"category": cat, "count": cnt} for cat, cnt in top]
    except Exception as e:
        logger.warning("Qdrant fault query failed: %s", e)
        return []


def _query_qdrant_fault_trend(
    indexer: Any,
    hours: int,
) -> list[dict[str, Any]]:
    """Query Qdrant for hourly fault counts over the last ``hours`` hours.

    Returns a list of ``{"hour": str, "count": int}`` entries. Each
    ``hour`` is an ISO 8601 string marking the start of the bucket.
    Buckets with zero faults are included.
    """
    if indexer is None:
        return []
    try:
        qdrant_client = indexer._qdrant_client  # noqa: SLF001
        collection_name = indexer._collection_name  # noqa: SLF001

        # Scroll all points and bucket by timestamp
        all_timestamps: list[str | None] = []
        offset: str | int | None = None
        while True:
            results, offset = qdrant_client.scroll(
                collection_name=collection_name,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in results:
                if point.payload:
                    all_timestamps.append(point.payload.get("timestamp"))
            if offset is None:
                break

        # Build hourly buckets
        now = _utc_now()
        start = now - timedelta(hours=hours)
        buckets: dict[str, int] = {}
        for h in range(hours + 1):
            bucket_time = start + timedelta(hours=h)
            bucket_time = bucket_time.replace(minute=0, second=0, microsecond=0)
            buckets[bucket_time.isoformat()] = 0

        for ts_str in all_timestamps:
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if ts < start:
                    continue
                bucket_time = ts.replace(minute=0, second=0, microsecond=0)
                key = bucket_time.isoformat()
                if key in buckets:
                    buckets[key] += 1
            except (ValueError, TypeError):
                continue

        return [{"hour": k, "count": v} for k, v in buckets.items()]
    except Exception as e:
        logger.warning("Qdrant fault trend query failed: %s", e)
        return []


# ─── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/summary")
async def get_summary(
    request: Request,
    db: DBSession,
) -> dict[str, Any]:
    """GET /api/v1/dashboard/summary — aggregated dashboard overview.

    Returns a single response with:
    - ``active_workers``: count of online workers (from KV bucket).
    - ``total_executions_today``: executions created today.
    - ``completed_today``: executions completed today.
    - ``failed_today``: executions failed today.
    - ``pass_rate``: completed / (completed + failed) as a percentage.
    - ``total_faults``: fault records in Qdrant (if available).

    Worker count degrades to 0 if NATS is unavailable. Fault count
    degrades to 0 if Qdrant is unavailable.
    """
    today_start = _today_start()

    # Active worker count (graceful degradation)
    active_workers = 0
    worker_service = _get_worker_service(request)
    if worker_service is not None:
        try:
            workers = await worker_service.list_workers()
            active_workers = len(workers)
        except Exception as e:
            logger.warning("Worker list failed: %s", e)

    # Execution aggregation from DB
    result = await db.execute(
        select(func.count()).select_from(Execution).where(
            Execution.created_at >= today_start
        )
    )
    total_today = result.scalar() or 0

    result = await db.execute(
        select(func.count()).select_from(Execution).where(
            Execution.created_at >= today_start,
            Execution.status == "COMPLETED",
        )
    )
    completed_today = result.scalar() or 0

    result = await db.execute(
        select(func.count()).select_from(Execution).where(
            Execution.created_at >= today_start,
            Execution.status == "FAILED",
        )
    )
    failed_today = result.scalar() or 0

    # Pass rate: completed / (completed + failed)
    total_terminal = completed_today + failed_today
    pass_rate = round((completed_today / total_terminal) * 100, 1) if total_terminal > 0 else 0.0

    # Total faults from Qdrant (graceful degradation)
    total_faults = 0
    indexer = _get_failure_indexer(request)
    if indexer is not None:
        try:
            qdrant_client = indexer._qdrant_client  # noqa: SLF001
            collection_name = indexer._collection_name  # noqa: SLF001
            info = qdrant_client.get_collection(collection_name)
            total_faults = info.points_count or 0
        except Exception as e:
            logger.warning("Qdrant fault count failed: %s", e)

    return {
        "active_workers": active_workers,
        "total_executions_today": total_today,
        "completed_today": completed_today,
        "failed_today": failed_today,
        "pass_rate": pass_rate,
        "total_faults": total_faults,
    }


@router.get("/stations")
async def get_stations(
    request: Request,
) -> dict[str, Any]:
    """GET /api/v1/dashboard/stations — per-station worker status.

    Returns a list of station objects with:
    - ``worker_id``: unique worker identifier.
    - ``hostname``: worker hostname.
    - ``status``: ``online`` (key exists in KV).
    - ``capabilities``: list of capability strings.
    - ``current_tasks``: number of tasks currently assigned.
    - ``max_concurrent_tasks``: maximum concurrent task capacity.

    Returns an empty list if NATS is unavailable.
    """
    worker_service = _get_worker_service(request)
    if worker_service is None:
        return {"stations": [], "total": 0}

    try:
        workers = await worker_service.list_workers()
    except Exception as e:
        logger.warning("Worker list for stations failed: %s", e)
        return {"stations": [], "total": 0}

    stations = [
        {
            "worker_id": w.worker_id,
            "hostname": w.hostname,
            "status": "online",
            "capabilities": w.capabilities,
            "current_tasks": w.current_tasks,
            "max_concurrent_tasks": w.max_concurrent_tasks,
        }
        for w in workers
    ]
    return {"stations": stations, "total": len(stations)}


@router.get("/faults")
async def get_faults(
    request: Request,
) -> dict[str, Any]:
    """GET /api/v1/dashboard/faults — fault trend + Top-5 Pareto.

    Returns:
    - ``trend``: list of ``{"hour": str, "count": int}`` for the last
      24 hours (hourly buckets).
    - ``top_faults``: list of ``{"category": str, "count": int}`` for
      the top 5 most frequent fault categories.

    Both degrade to empty lists if Qdrant is unavailable.
    """
    indexer = _get_failure_indexer(request)

    trend = _query_qdrant_fault_trend(indexer, _FAULT_TREND_HOURS)
    top_faults = _query_qdrant_top_faults(indexer, _TOP_FAULTS_LIMIT)

    return {
        "trend": trend,
        "top_faults": top_faults,
    }


@router.get("/executions")
async def get_executions(
    db: DBSession,
) -> dict[str, Any]:
    """GET /api/v1/dashboard/executions — today's execution breakdown.

    Returns execution counts grouped by status for today:
    - ``total``: total executions created today.
    - ``by_status``: dict mapping status → count.
    - ``recent``: list of the 10 most recent executions (id, status,
      sequence_id, started_at, completed_at).
    """
    today_start = _today_start()

    # Count by status
    result = await db.execute(
        select(Execution.status, func.count()).where(
            Execution.created_at >= today_start
        ).group_by(Execution.status)
    )
    status_rows = result.all()
    by_status: dict[str, int] = {row[0]: row[1] for row in status_rows}
    total = sum(by_status.values())

    # Recent 10 executions
    result = await db.execute(
        select(Execution).where(
            Execution.created_at >= today_start
        ).order_by(Execution.created_at.desc()).limit(10)
    )
    recent_executions = result.scalars().all()
    recent = [
        {
            "id": str(e.id),
            "status": e.status,
            "sequence_id": e.sequence_id,
            "started_at": e.started_at.isoformat() if e.started_at else None,
            "completed_at": e.completed_at.isoformat() if e.completed_at else None,
        }
        for e in recent_executions
    ]

    return {
        "total": total,
        "by_status": by_status,
        "recent": recent,
    }
