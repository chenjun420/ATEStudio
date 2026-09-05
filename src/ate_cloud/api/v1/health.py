from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from nats.js.errors import NotFoundError
from sqlalchemy import text

from ate_cloud.config import settings
from ate_cloud.db import async_session_factory
from ate_cloud.services.falkordb_graph_service import FalkorDBGraphService
from ate_cloud.services.graph_service import GraphService

# Worker registry KV bucket name — must match stream_manager.py / main.py.
_WORKER_KV_BUCKET = "ate-workers"

_STATUS_OK = "ok"
_STATUS_DOWN = "down"

router = APIRouter(tags=["health"])


def _get_or_create_graph_service(request: Request) -> GraphService | None:
    """Lazily fetch/create the GraphService, cached on ``app.state``.

    Mirrors the lazy factories in faults.py/diagnose.py: the service is
    reused from ``app.state.graph_service`` when present (e.g. injected by
    tests or created by another router) and otherwise constructed from
    settings on first use. Construction opens no socket (the FalkorDB
    client connects lazily on first command), so a missing/unreachable
    graph never blocks boot. Returns ``None`` when construction fails —
    the readiness probe reports ``graph: "down"`` instead of raising.
    """
    service: GraphService | None = getattr(request.app.state, "graph_service", None)
    if service is not None:
        return service
    try:
        service = FalkorDBGraphService(
            url=settings.falkordb_url,
            graph_name=settings.falkordb_graph,
            password=settings.falkordb_password or None,
        )
    except Exception:
        return None
    request.app.state.graph_service = service
    return service


@router.get("/health/db")
async def check_db_health() -> dict[str, str]:
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "healthy"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database connection failed: {str(e)}"
        ) from e


@router.get("/health/nats")
async def check_nats_health() -> dict[str, Any]:
    """Report NATS connection, JetStream, and worker registry KV status.

    Reads the module-level ``_nats_client`` set by the lifespan (read through
    the module so tests that stub ``ate_cloud.main._nats_client`` are seen).
    Returns 503 when NATS is not connected. Per AGENTS.md §7 the lifespan
    pre-check already crashes on a missing NATS/JetStream at startup; this
    endpoint reports the runtime status for operators.
    """
    import ate_cloud.main as main_module

    nc = main_module._nats_client
    if nc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NATS client not connected",
        )

    jetstream_available = False
    js: Any = None
    try:
        js = nc.jetstream()
        await js.account_info()
        jetstream_available = True
    except Exception:
        pass

    worker_registry_kv = "error"
    if jetstream_available and js is not None:
        try:
            await js.key_value(_WORKER_KV_BUCKET)
            worker_registry_kv = "ready"
        except NotFoundError:
            worker_registry_kv = "not_initialized"
        except Exception:
            worker_registry_kv = "error"

    return {
        "nats_connected": True,
        "jetstream_available": jetstream_available,
        "worker_registry_kv": worker_registry_kv,
    }


@router.get("/health/ready")
async def readiness(request: Request) -> dict[str, str]:
    """Aggregated readiness probe: per-component ``ok``/``down`` status.

    Always returns HTTP 200 — this endpoint degrades gracefully rather
    than failing on a single dependency; operators inspect the per-component
    values (NATS remains the only FATAL boot dependency, enforced by the
    lifespan pre-check — this surface reports runtime reachability).

    Components:
    - ``database`` — ``SELECT 1`` round-trip (mirrors ``/health/db``).
    - ``nats``     — connected client set by the lifespan (mirrors the
      ``nats_connected`` flag of ``/health/nats``).
    - ``graph``    — FalkorDBGraphService.health() Redis PING. The graph
      is optional: the service is constructed lazily on first probe
      (cached on ``app.state.graph_service``, shared with faults/diagnose)
      and any construction/connection error maps to ``"down"`` — graph
      presence is never a boot requirement and a down graph must not 500.

    Qdrant/vector health is intentionally out of scope here.
    """

    async def _database_ok() -> bool:
        try:
            async with async_session_factory() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def _nats_connected() -> bool:
        import ate_cloud.main as main_module

        return main_module._nats_client is not None

    async def _graph_ok() -> bool:
        graph_service = _get_or_create_graph_service(request)
        if graph_service is None:
            return False
        try:
            await graph_service.health()
            return True
        except Exception:
            return False

    return {
        "database": _STATUS_OK if await _database_ok() else _STATUS_DOWN,
        "nats": _STATUS_OK if _nats_connected() else _STATUS_DOWN,
        "graph": _STATUS_OK if await _graph_ok() else _STATUS_DOWN,
    }
