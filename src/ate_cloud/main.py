import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import nats
from fastapi import FastAPI
from nats.aio.client import Client as NatsClient

from ate_cloud.api.v1.router import api_router
from ate_cloud.config import settings
from ate_cloud.nats.sse_bridge import SSEBridge
from ate_cloud.services.execution_status_relay import ExecutionStatusRelay
from ate_cloud.services.failure_indexer import FailureIndexer
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
        await failure_indexer.ensure_collection()
        failure_indexer.subscribe_to_events(bridge)
        app.state.failure_indexer = failure_indexer

        # Automatic failure→KG evolution (task 16): after a failure is
        # indexed, evolve the ontology KG via the task-7 pipeline. The
        # pipeline is built lazily on first failure and cached on app.state;
        # any construction failure (no graph / Semantica unusable) degrades
        # to a logged skip — it never blocks failure indexing.
        from ate_cloud.services.failure_evolution import FailureEvolutionTrigger

        def _resolve_failure_pipeline() -> object | None:
            cached: object | None = getattr(app.state, "failure_kg_pipeline", None)
            if cached is not None:
                return cached
            try:
                from ate_cloud.services.falkordb_graph_service import FalkorDBGraphService
                from ate_cloud.services.kg_pipeline import build_pipeline

                graph_service = FalkorDBGraphService(
                    url=settings.falkordb_url,
                    graph_name=settings.falkordb_graph,
                    password=settings.falkordb_password or None,
                )
                pipeline = build_pipeline(
                    graph_service=graph_service,
                    embedding_service=embedding_service,
                    qdrant_client=qdrant_client,
                )
            except Exception as exc:  # noqa: BLE001 — graph/key absent is a benign skip
                print(  # noqa: T201
                    "Auto KG evolution disabled (pipeline unavailable: "
                    f"{type(exc).__name__}: {exc}); failures still indexed"
                )
                return None
            app.state.failure_kg_pipeline = pipeline
            app.state.graph_service = graph_service
            return pipeline

        failure_indexer.set_evolution_trigger(
            FailureEvolutionTrigger(resolve=_resolve_failure_pipeline).evolve_from_failure
        )
        print(f"Failure indexer initialized with Qdrant at {settings.qdrant_url}")  # noqa: T201
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


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs",
        lifespan=lifespan,
    )
    app.include_router(api_router, prefix="/api/v1")
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
