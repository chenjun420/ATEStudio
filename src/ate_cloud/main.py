from contextlib import asynccontextmanager

import nats
from fastapi import FastAPI
from nats.aio.client import Client as NatsClient

from ate_cloud.api.v1.router import api_router
from ate_cloud.config import settings

# Global NATS client (optional - not blocking startup)
_nats_client: NatsClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage NATS connection lifecycle.

    NATS connection is optional and will not block startup on failure.
    """
    # Startup
    global _nats_client
    try:
        _nats_client = await nats.connect(settings.nats_url)
        print(f"Connected to NATS: {settings.nats_url}")  # noqa: T201
    except Exception as e:
        print(f"Warning: Could not connect to NATS: {e}")  # noqa: T201
        _nats_client = None

    yield

    # Shutdown
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