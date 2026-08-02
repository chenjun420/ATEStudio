from contextlib import asynccontextmanager
from pathlib import Path

import nats
from fastapi import FastAPI
from nats.aio.client import Client as NatsClient

from ate_cloud.api.v1.router import api_router
from ate_cloud.config import settings
from ate_cloud.nats.sse_bridge import SSEBridge
from ate_cloud.services.failure_indexer import FailureIndexer
from ate_cloud.services.script_versioning import ScriptVersioningService

# Global NATS client (optional - not blocking startup)
_nats_client: NatsClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage NATS connection lifecycle, SSE bridge, Qdrant, and services.

    NATS connection is optional and will not block startup on failure.
    SSE bridge is always initialized (works with or without NATS).
    Qdrant is initialized for failure indexing (optional — graceful degradation).
    Script versioning service is initialized with SCRIPTS_ROOT_DIR env var.
    """
    # Startup - connect to NATS (needed for worker registry, config distribution,
    # and SSE bridge). Connection failure is non-fatal — services that depend on
    # NATS will return 503, but the app still starts.
    global _nats_client
    try:
        _nats_client = await nats.connect(settings.nats_url, max_reconnect_attempts=-1)
        app.state.nc = _nats_client
        print(f"NATS connected to {settings.nats_url}")  # noqa: T201
    except Exception as e:
        _nats_client = None
        print(f"NATS connection failed ({type(e).__name__}: {e}); NATS-dependent services disabled")  # noqa: T201

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

    # Initialize Qdrant client and failure indexer (optional — graceful degradation)
    try:
        from qdrant_client import QdrantClient

        qdrant_client = QdrantClient(url=settings.qdrant_url)
        # Simple embedding function — production should use DeepAgents or similar
        def _embed_text(text: str) -> list[float]:
            """Placeholder embedding: returns a deterministic hash-based vector.

            In production, replace with DeepAgents embedding API call.
            """
            import hashlib

            seed = hashlib.sha256(text.encode()).digest()
            vec = [0.0] * settings.embedding_dimensions
            for i in range(min(settings.embedding_dimensions, len(seed) * 8)):
                byte_idx = i // 8
                bit_idx = i % 8
                vec[i] = 1.0 if (seed[byte_idx] >> bit_idx) & 1 else -1.0
            return vec

        failure_indexer = FailureIndexer(
            qdrant_client=qdrant_client,
            embedding_model=_embed_text,
        )
        await failure_indexer.ensure_collection()
        failure_indexer.subscribe_to_events(bridge)
        app.state.failure_indexer = failure_indexer
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

    yield

    # Shutdown
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


def get_nats() -> NatsClient | None:
    """Get the global NATS client instance.

    Returns None if NATS is not connected.
    """
    return _nats_client