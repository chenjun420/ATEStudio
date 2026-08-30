from typing import Any

from fastapi import APIRouter, HTTPException, status
from nats.js.errors import NotFoundError
from sqlalchemy import text

from ate_cloud.db import async_session_factory

# Worker registry KV bucket name — must match stream_manager.py / main.py.
_WORKER_KV_BUCKET = "ate-workers"

router = APIRouter(tags=["health"])


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
