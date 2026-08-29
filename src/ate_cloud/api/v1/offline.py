"""Offline status/reconcile/cache API endpoints (T24 v41-gap-analysis, doc §10.5).

Exposes the offline-autonomy layer's live state so the UI can render the
offline badge and operators can trigger reconciliation WITHOUT station
downtime:

- ``GET  /api/v1/offline/status``        - badge snapshot: connection state,
  pending-upload count, cache health (size / oldest record age / capacity %).
- ``POST /api/v1/offline/reconcile``     - manual Reconciler trigger (202).
- ``GET  /api/v1/offline/cache/items``   - cached sequence/topology entries.
- ``GET  /api/v1/offline/status/stream`` - SSE stream emitting ``offline_status``
  events on the existing SSEBridge (isolated stream queue, topology-stream
  precedent) so the badge updates push-style.

Consumes edge components purely through their PUBLIC ctor seams (no
offline/* internals modified): HeartbeatMonitor, CapacityGuard, UploadQueue,
OfflineCacheStore, Reconciler. The composition root is
:class:`OfflineStatusService`; deployments wire one instance onto
``app.state.offline_status_service`` (503 when absent - honest "not
configured" rather than fabricated data).

Privacy: responses never contain raw filesystem paths - quarantine views
drop the payload-path detail, cache listings expose logical ids only.

Auth: mounted via ``_PROTECTED_ROUTERS`` (T17 central JWT enforcement).
SSE note: native EventSource cannot send Authorization headers; browser
clients must use a token-carrying fetch-stream (same migration tracked for
the executions SSE endpoints in the T17/T24 notes).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse, ServerSentEvent  # type: ignore[import-untyped]

from ate_cloud.nats.sse_bridge import SSEBridge
from ate_platform.offline import (
    DEFAULT_SOFT_SIZE_BYTES,
    STATE_ONLINE,
    CapacityGuard,
    HeartbeatMonitor,
    OfflineCacheStore,
    Reconciler,
    ReconcileReport,
    UploadQueue,
)

router = APIRouter(prefix="/offline", tags=["offline"])

#: Ticket-authenticated mount for the SSE stream (RH-3). Mounted in
#: router.py WITHOUT the mount-level JWT guard and WITH a mount-level
#: ``require_sse_user`` dependency (see executions.sse_router note).
sse_router = APIRouter(prefix="/offline", tags=["offline"])

#: Pseudo run-id for the dedicated offline stream queue. Double-underscore
#: prefix can never collide with a real execution run id.
OFFLINE_STREAM_RUN_ID = "__offline__"

#: Stream name inside the SSEBridge isolated stream-queue namespace.
OFFLINE_STREAM_NAME = "status"

#: SSE ``event:`` line emitted on this stream (UI addEventListener target).
OFFLINE_STATUS_EVENT = "offline_status"

#: Keep-alive interval for the SSE loop (matches executions.py convention).
_SSE_HEARTBEAT_INTERVAL = 15.0


# ---------------------------------------------------------------------------
# Service seam: composes offline/* public APIs (ctor injection only)
# ---------------------------------------------------------------------------


class OfflineStatusError(Exception):
    """Raised when a requested capability is not wired into the service."""


class OfflineStatusService:
    """Read-model over the offline autonomy components (doc §10.5).

    All collaborators are injected via the constructor - this class owns no
    storage and never mutates offline/* module state except through the
    explicit :meth:`reconcile` trigger.
    """

    def __init__(
        self,
        *,
        heartbeat: HeartbeatMonitor,
        capacity_guard: CapacityGuard,
        upload_queue: UploadQueue,
        cache_store: OfflineCacheStore,
        reconciler: Reconciler | None = None,
        capacity_budget_bytes: int = DEFAULT_SOFT_SIZE_BYTES,
    ) -> None:
        if capacity_budget_bytes <= 0:
            raise ValueError("capacity_budget_bytes must be > 0")
        self._heartbeat = heartbeat
        self._capacity_guard = capacity_guard
        self._upload_queue = upload_queue
        self._cache_store = cache_store
        self._reconciler = reconciler
        self._budget = int(capacity_budget_bytes)

    # -- wiring views (read-only; lets deployers/tests reach the seams) ----

    @property
    def upload_queue(self) -> UploadQueue:
        """The injected upload queue (seam view)."""
        return self._upload_queue

    @property
    def cache_store(self) -> OfflineCacheStore:
        """The injected offline cache store (seam view)."""
        return self._cache_store

    # -- read models -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Badge snapshot: {online, pending_upload_count, cache_health{...}}."""
        hb = self._heartbeat.status
        cap = self._capacity_guard.measure()  # pure observation: no alerts fired
        oldest_age_h = (
            round(cap.oldest_age_seconds / 3600.0, 3)
            if cap.oldest_age_seconds is not None
            else None
        )
        return {
            "online": hb.state == STATE_ONLINE,
            "pending_upload_count": self._upload_queue.stats()["pending"],
            "cache_health": {
                "size_bytes": cap.size_bytes,
                "oldest_record_age_h": oldest_age_h,
                "capacity_pct": round(cap.size_bytes / self._budget * 100.0, 2),
                "downloads_paused": cap.downloads_paused,
            },
        }

    def reconcile(self) -> dict[str, Any]:
        """Run one full reconciliation pass; returns a path-free report view."""
        if self._reconciler is None:
            raise OfflineStatusError("reconciler not configured")
        report: ReconcileReport = self._reconciler.reconcile()
        return {
            "ok": report.ok,
            "uploaded": report.uploaded,
            "acked": report.acked,
            "confirmed_entries": report.confirmed_entries,
            "conflicts_resolved": report.conflicts_resolved,
            "quarantined": report.quarantined,
            "locks_released": report.locks_released,
            "duration": report.duration,
            # detail strings embed payload paths - dropped on purpose.
            "quarantine": [
                {
                    "reason": item.reason,
                    "station_id": item.station_id,
                    "execution_id": item.execution_id,
                    "seq_no": item.seq_no,
                    "kind": item.kind,
                    "entry_id": item.entry_id,
                    "version": item.version,
                }
                for item in report.quarantine
            ],
        }

    def cache_items(self) -> list[dict[str, Any]]:
        """Cached entry listing (logical ids only, payloads/paths excluded)."""
        return [
            {
                "kind": entry.kind,
                "id": entry.id,
                "version": entry.version,
                "state": entry.state,
                "checksum": entry.checksum,
                "created_at": entry.created_at,
                "acked_at": entry.acked_at,
            }
            for entry in self._cache_store.list_cached()
        ]


# ---------------------------------------------------------------------------
# Dependency factories (T17 pattern: importable dependency_overrides keys)
# ---------------------------------------------------------------------------


def _get_status_service(request: Request) -> OfflineStatusService:
    """Dependency: fetch the wired OfflineStatusService from app.state."""
    service = getattr(request.app.state, "offline_status_service", None)
    if not isinstance(service, OfflineStatusService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Offline status service not configured",
        )
    return service


def get_sse_bridge(request: Request) -> SSEBridge:
    """Dependency: fetch the SSEBridge from app.state (executions.py pattern)."""
    bridge = getattr(request.app.state, "sse_bridge", None)
    if not isinstance(bridge, SSEBridge):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SSE bridge not available",
        )
    return bridge


def _get_sse_bridge_optional(request: Request) -> SSEBridge | None:
    """Lenient bridge lookup for fire-and-forget publishes (never blocks)."""
    bridge = getattr(request.app.state, "sse_bridge", None)
    return bridge if isinstance(bridge, SSEBridge) else None


# ---------------------------------------------------------------------------
# Publisher helper (existing bridge, isolated stream queue - no bridge edits)
# ---------------------------------------------------------------------------


async def publish_offline_status(bridge: SSEBridge, data: dict[str, Any]) -> None:
    """Publish an ``offline_status`` event onto the dedicated stream queue."""
    await bridge.publish_stream_event(
        OFFLINE_STREAM_RUN_ID, OFFLINE_STREAM_NAME, OFFLINE_STATUS_EVENT, data
    )


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class CacheHealth(BaseModel):
    """Offline cache health view (doc §10.5 capacity fields)."""

    size_bytes: int = Field(..., description="Total cached bytes on disk")
    oldest_record_age_h: float | None = Field(
        ..., description="Age of the oldest cached record in hours (null if empty)"
    )
    capacity_pct: float = Field(
        ..., description="size_bytes as percent of the configured capacity budget"
    )
    downloads_paused: bool = Field(
        ..., description="True when the hard threshold paused new downloads"
    )


class OfflineStatusResponse(BaseModel):
    """GET /offline/status - offline badge data."""

    online: bool = Field(..., description="Station connectivity per heartbeat monitor")
    pending_upload_count: int = Field(..., description="Records awaiting upload+ACK")
    cache_health: CacheHealth


class QuarantineView(BaseModel):
    """Path-free quarantine item (payload paths intentionally excluded)."""

    reason: str
    station_id: str | None = None
    execution_id: str | None = None
    seq_no: int | None = None
    kind: str | None = None
    entry_id: str | None = None
    version: str | None = None


class ReconcileResponse(BaseModel):
    """POST /offline/reconcile - reconciliation report summary."""

    ok: bool
    uploaded: int
    acked: int
    confirmed_entries: int
    conflicts_resolved: int
    quarantined: int
    locks_released: int
    duration: float
    quarantine: list[QuarantineView]


class CacheItemView(BaseModel):
    """One cached entry (logical identity + state; no payload, no paths)."""

    kind: str
    id: str
    version: str
    state: str
    checksum: str
    created_at: float
    acked_at: float | None


class CacheItemsResponse(BaseModel):
    """GET /offline/cache/items."""

    items: list[CacheItemView]
    total: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/status", response_model=OfflineStatusResponse)
async def get_offline_status(
    service: Annotated[OfflineStatusService, Depends(_get_status_service)],
) -> OfflineStatusResponse:
    """GET /api/v1/offline/status - offline badge snapshot.

    Answers instantly from in-process monitors; querying never requires
    station downtime and never exposes filesystem paths.
    """
    return OfflineStatusResponse(**service.status())


@router.post("/reconcile", response_model=ReconcileResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_reconcile(
    request: Request,
    service: Annotated[OfflineStatusService, Depends(_get_status_service)],
    bridge: Annotated[SSEBridge | None, Depends(_get_sse_bridge_optional)],
) -> ReconcileResponse:
    """POST /api/v1/offline/reconcile - manual reconciliation trigger.

    Runs the Reconciler in a worker thread (upload I/O must not block the
    loop) and returns 202 with the report summary. On success, refreshes
    the SSE badge by publishing a fresh status snapshot (best-effort).
    """
    try:
        report = await asyncio.to_thread(service.reconcile)
    except OfflineStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    if bridge is not None:
        try:
            await publish_offline_status(bridge, service.status())
        except Exception:  # noqa: BLE001 - badge refresh is advisory only
            pass
    del request  # reserved for future disconnect-aware semantics
    return ReconcileResponse(**report)


@router.get("/cache/items", response_model=CacheItemsResponse)
async def list_cache_items(
    service: Annotated[OfflineStatusService, Depends(_get_status_service)],
) -> CacheItemsResponse:
    """GET /api/v1/offline/cache/items - cached sequence/topology entries."""
    items = service.cache_items()
    return CacheItemsResponse(items=items, total=len(items))


@sse_router.get("/status/stream", response_class=EventSourceResponse)
async def stream_offline_status(
    request: Request,
    bridge: Annotated[SSEBridge, Depends(get_sse_bridge)],
    service: Annotated[OfflineStatusService, Depends(_get_status_service)],
) -> EventSourceResponse:
    """GET /api/v1/offline/status/stream - SSE feed of ``offline_status`` events.

    Frame 0 is the immediate current snapshot (instant badge paint); later
    frames are events published onto the bridge's isolated
    ``__offline__:status`` stream queue (topology-stream precedent) with
    keep-alive comments between events.
    """
    queue = bridge.get_stream_queue(OFFLINE_STREAM_RUN_ID, OFFLINE_STREAM_NAME)

    async def event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        try:
            yield ServerSentEvent(
                data=json.dumps(service.status()),
                event=OFFLINE_STATUS_EVENT,
            )
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=_SSE_HEARTBEAT_INTERVAL
                    )
                    if event.get("type") != OFFLINE_STATUS_EVENT:
                        continue
                    yield ServerSentEvent(
                        data=json.dumps(event.get("data", {})),
                        event=event.get("type", OFFLINE_STATUS_EVENT),
                        id=event.get("id"),
                    )
                except TimeoutError:
                    yield ServerSentEvent(data="", comment="keep-alive")
        finally:
            bridge.remove_stream_queue(OFFLINE_STREAM_RUN_ID, OFFLINE_STREAM_NAME)

    return EventSourceResponse(event_generator())
