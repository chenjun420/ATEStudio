import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import nats
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from nats.aio.client import Client as NatsClient

from ate_cloud.api.v1.router import api_router
from ate_cloud.config import settings
from ate_cloud.nats.sse_bridge import SSEBridge
from ate_cloud.services.execution_status_relay import ExecutionStatusRelay
from ate_cloud.services.failure_indexer import EmbeddingDimensionMismatchError, FailureIndexer
from ate_cloud.services.script_versioning import ScriptVersioningService

# Global NATS client (optional - not blocking startup)
_nats_client: NatsClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage NATS connection lifecycle, SSE bridge, Qdrant, and services.

    Per AGENTS.md §7: NATS and JetStream are REQUIRED, not optional.
    Startup crashes fatally if NATS is unreachable or JetStream is disabled —
    no silent degradation. Qdrant and script versioning degrade gracefully.
    """
    # Startup - connect to NATS (needed for worker registry, config distribution,
    # and SSE bridge). Per AGENTS.md §7, a connection failure is FATAL: the
    # app must not start in a half-degraded state.
    global _nats_client
    try:
        _nats_client = await nats.connect(settings.nats_url, max_reconnect_attempts=-1)
        app.state.nc = _nats_client
        print(f"NATS connected to {settings.nats_url}")  # noqa: T201
    except Exception as e:
        _nats_client = None
        raise RuntimeError(
            f"Failed to connect to NATS at {settings.nats_url}: {type(e).__name__}: {e}"
        ) from e

    # Verify JetStream is enabled on the server (AGENTS.md §7 — required).
    # The worker registry KV bucket, config distribution, and status relay all
    # depend on JetStream; fail fast rather than limping along.
    try:
        js = _nats_client.jetstream()
        await js.account_info()
    except Exception as e:
        raise RuntimeError(
            f"JetStream not available on {settings.nats_url}: {type(e).__name__}: {e}"
        ) from e

    # Initialize SSE bridge (works with or without NATS)
    bridge = SSEBridge(nc=_nats_client)
    app.state.sse_bridge = bridge
    print("SSE bridge initialized")  # noqa: T201

    # Ensure KV buckets exist (non-fatal if NATS is down)
    if _nats_client is not None:
        try:
            from ate_cloud.services.config_distribution import ConfigDistributionService
            config_svc = ConfigDistributionService(_nats_client)
            await config_svc.ensure_bucket()
            app.state.config_distribution = config_svc
            print("Config distribution KV bucket ready")  # noqa: T201
        except Exception as e:
            print(f"Config distribution init failed ({type(e).__name__}: {e})")  # noqa: T201

        # Ensure ate-workers KV bucket exists (TTL=30s for heartbeat expiry)
        try:
            js = _nats_client.jetstream()
            try:
                await js.key_value("ate-workers")
            except Exception:
                await js.create_key_value(
                    bucket="ate-workers",
                    ttl=30,  # 30-second TTL — auto-expire stale heartbeats
                )
                print("Worker registry KV bucket 'ate-workers' created (TTL=30s)")  # noqa: T201
        except Exception as e:
            print(f"Worker registry KV bucket creation failed ({type(e).__name__}: {e})")  # noqa: T201

    # Initialize Qdrant client and failure indexer (optional — graceful degradation).
    # The Qdrant client is ALWAYS stored on app.state when constructed (the
    # diagnose endpoint's lazy _get_qdrant_client reads it; absent client →
    # 503 on vector paths). The failure indexer embeds via the real
    # EmbeddingService when an OpenAI-compatible key is configured; with no
    # key it still indexes (zero vectors) so the non-ontology Qdrant failure
    # index keeps working and vector search degrades to no semantic results.
    failure_indexer = None
    try:
        from qdrant_client import QdrantClient

        qdrant_client = QdrantClient(url=settings.qdrant_url)
        app.state.qdrant_client = qdrant_client

        embedding_service = None
        if settings.openai_api_key:
            from ate_cloud.services.embedding_service import EmbeddingService

            embedding_service = EmbeddingService(
                api_key=settings.openai_api_key,
                model=settings.openai_embedding_model,
                dimensions=settings.embedding_dimensions,
            )
            app.state.embedding_service = embedding_service
        else:
            print(  # noqa: T201
                "No OPENAI_API_KEY configured; failure indexer runs without "
                "embeddings (vector search degraded, graph paths unaffected)"
            )

        failure_indexer = FailureIndexer(
            qdrant_client=qdrant_client,
            embedding_service=embedding_service,
            embedding_dim=settings.embedding_dimensions,
        )
        try:
            await failure_indexer.ensure_collection()
        except EmbeddingDimensionMismatchError as exc:
            # Recorded rather than raised: the mismatch means "this capability
            # is off", not "the service cannot start". Swallowing it silently is
            # what made this failure invisible before — index_failure logs and
            # never raises, so fault history just stopped growing.
            # /diagnose/readiness reads this and stops claiming availability.
            print(f"Failure indexing disabled: {exc}")  # noqa: T201
            app.state.failure_index_blockers = [str(exc)]
            app.state.failure_indexer = None
            # Nulled locally too, so nothing downstream is handed an indexer
            # that was never subscribed. ExecutionStatusRelay takes None for
            # "no indexing", which is the truth here.
            failure_indexer = None
        else:
            app.state.failure_index_blockers = []
            failure_indexer.subscribe_to_events(bridge)
            app.state.failure_indexer = failure_indexer

        # Automatic failure→KG evolution was removed with the knowledge-graph
        # subsystem. It resolved a `kg_pipeline` lazily on first failure, and
        # the resolution failed every time ("Auto KG evolution disabled
        # (pipeline unavailable: ...)"), because the graph backend it needed was
        # never deployed. Fault cases now accumulate in the station case base
        # and are indexed for retrieval; nothing evolves a graph.

        if failure_indexer is not None:
            print(  # noqa: T201
                "Failure indexer initialized with Qdrant at "
                f"{settings.qdrant_url} (dim={settings.embedding_dimensions})"
            )
    except ImportError:
        print("qdrant-client not installed; failure indexing disabled")  # noqa: T201
    except Exception as e:
        print(f"Qdrant initialization failed ({type(e).__name__}: {e}); failure indexing disabled")  # noqa: T201

    # Initialize script versioning service
    import os

    scripts_root = Path(os.environ.get("SCRIPTS_ROOT_DIR", str(Path(__file__).parent.parent.parent / "scripts")))
    versioning_service = ScriptVersioningService(scripts_root=scripts_root)
    app.state.script_versioning = versioning_service
    print(f"Script versioning initialized at: {scripts_root}")  # noqa: T201

    # Wire ExecutionStatusRelay as a background task (NATS only) — it
    # bridges ATE_STATUS JetStream messages to DB updates + SSE queue.
    if _nats_client is not None:
        from ate_cloud.db import async_session_factory
        from ate_cloud.services.breakpoint_registry import BreakpointRegistry

        app.state.breakpoint_registry = BreakpointRegistry()
        status_relay = ExecutionStatusRelay(
            nats_client=_nats_client,
            sse_bridge=bridge,
            async_session_factory=async_session_factory,
            breakpoint_registry=app.state.breakpoint_registry,
            failure_indexer=failure_indexer,
        )
        app.state.status_relay = status_relay
        app.state.status_relay_task = asyncio.create_task(status_relay.start())
        print("ExecutionStatusRelay started")  # noqa: T201

    yield

    # Shutdown
    relay_task = getattr(app.state, "status_relay_task", None)
    if relay_task is not None:
        relay_task.cancel()
        try:
            await relay_task
        except asyncio.CancelledError:
            pass

    await bridge.cleanup()

    if _nats_client is not None:
        try:
            await _nats_client.close()
            print("NATS connection closed")  # noqa: T201
        except Exception as e:
            print(f"Warning: Error closing NATS connection: {e}")  # noqa: T201
        finally:
            _nats_client = None


def _mount_spa(app: FastAPI) -> None:
    """Serve the built single-page app at "/".

    Mounted last so ``/api/v1`` keeps winning, and as a catch-all rather than
    a bare ``StaticFiles`` mount because the router is in history mode
    (``createWebHistory``): a deep link like ``/aterag-review`` is served by
    vue-router, not by a file, so anything that is not a real asset has to
    return ``index.html`` or a page refresh mid-flow lands on a 404.

    The dist directory being absent is a supported state, not a crash: in
    development the API is run without a build. It is logged loudly rather
    than skipped silently, because a *deployed* box whose UI is unreachable
    looks exactly like a healthy one from every check that exists — the
    process is up, the health endpoint answers, and the deploy stamp says the
    frontend was built. That combination is what made this go unnoticed.
    """
    dist = Path(settings.frontend_dist_dir)
    if not dist.is_absolute():
        dist = Path.cwd() / dist
    index = dist / "index.html"

    if not index.is_file():
        print(  # noqa: T201
            f"No SPA at {dist} (index.html missing); serving the API only. "
            "Build the frontend or set ATE_FRONTEND_DIST_DIR."
        )
        return

    assets_dir = dist / "assets"

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(full_path: str) -> Response:
        """Serve a real file if there is one, else hand back the SPA shell.

        HEAD is accepted alongside GET because it is how a health check or a
        load balancer asks "is this up?". Answering 405 to ``curl -I /`` makes
        the page look broken to exactly the tool an operator reaches for first,
        and the 405 says nothing about whether the service is actually serving.

        API paths are passed through untouched. A catch-all that answered them
        with the HTML shell would turn every typo'd or unauthorised endpoint
        into a 200 that fails to parse — the caller then debugs a JSON decode
        error a long way from the actual mistake. Letting the API router's own
        404 (or its 401) stand keeps the failure where it belongs.
        """
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        if full_path:
            candidate = (dist / full_path).resolve()
            # Reject escapes: a path like "../../.env" would otherwise be read
            # out of the parent of dist and served as a static asset.
            if candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
        return FileResponse(index)

    # Assets are mounted separately so long-lived file caching can apply to
    # them while index.html stays uncached — a cached shell pins every user to
    # a stale bundle hash after a deploy, which reads as "the fix did not take".
    # Guarded because a missing assets/ would make StaticFiles raise at import.
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    print(f"SPA mounted at / from {dist}")  # noqa: T201


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs",
        lifespan=lifespan,
    )
    app.include_router(api_router, prefix="/api/v1")
    _mount_spa(app)
    return app


app = create_app()


def get_nats() -> NatsClient:
    """Get the global NATS client instance.

    Per AGENTS.md §7, NATS is required: if the client is not connected this
    raises instead of returning None, so callers fail loudly rather than
    silently degrading.

    Raises:
        RuntimeError: If NATS is not connected.
    """
    if _nats_client is None:
        raise RuntimeError("NATS client not connected")
    return _nats_client
