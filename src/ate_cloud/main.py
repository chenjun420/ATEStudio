from contextlib import asynccontextmanager
from pathlib import Path

import nats
from fastapi import FastAPI
from nats.aio.client import Client as NatsClient

from ate_cloud.api.v1.router import api_router
from ate_cloud.config import settings
from ate_cloud.nats.sse_bridge import SSEBridge
from ate_cloud.services.script_versioning import ScriptVersioningService

# Global NATS client (optional - not blocking startup)
_nats_client: NatsClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage NATS connection lifecycle, SSE bridge, and script versioning.

    NATS connection is optional and will not block startup on failure.
    SSE bridge is always initialized (works with or without NATS).
    Script versioning service is initialized with SCRIPTS_ROOT_DIR env var.
    """
    # Startup - NATS is optional, connection happens in background
    global _nats_client
    _nats_client = None
    print("NATS connection skipped (optional)")  # noqa: T201

    # Initialize SSE bridge (works with or without NATS)
    bridge = SSEBridge(nc=_nats_client)
    app.state.sse_bridge = bridge
    print("SSE bridge initialized")  # noqa: T201

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