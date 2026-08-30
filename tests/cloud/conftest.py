"""Test configuration for cloud module tests.

Provides SQLite in-memory database fixtures for testing database operations.
"""

import asyncio
from collections.abc import AsyncGenerator, Generator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ate_cloud.main import create_app
from ate_cloud.models import Base

# Test database URL - SQLite in-memory with async driver
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session", autouse=True)
def _dev_mode_enabled() -> Generator[None, None, None]:
    """Default the cloud test environment to ATE_DEV_MODE=true.

    The cloud API guards debug/breakpoint endpoints (and auth bypasses
    with a synthetic admin user) behind ``settings.dev_mode``. Tests that
    need the guarded behavior (auth verification, dev-mode 403 checks)
    override this back to False with a function-scoped monkeypatch.
    """
    from ate_cloud.config import settings

    old = settings.dev_mode
    settings.dev_mode = True
    yield
    settings.dev_mode = old


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Create a test database engine with SQLite in-memory database."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
    )

    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    # Drop all tables after test
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Create a test database session.

    Each test gets a fresh session that rolls back after the test.
    """
    async_session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    async with async_session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
def app(db_session: AsyncSession) -> FastAPI:
    """Create a FastAPI app with test database session override and SSE bridge."""
    from ate_cloud.db import get_db
    from ate_cloud.nats.sse_bridge import SSEBridge

    app = create_app()

    # Override the database dependency to use test session
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    # Attach SSE bridge to app state (lifespan doesn't run in tests)
    app.state.sse_bridge = SSEBridge(nc=None)

    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """Create an async HTTP client for testing.

    Attaches ``client.app`` so tests can reach ``app.state`` (e.g. the
    SSE bridge) the way the ASGI transport resolves dependencies.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # httpx AsyncClient supports attribute attachment; expose the app
        # so tests can inspect app.state without a separate fixture.
        ac.app = app  # type: ignore[attr-defined]
        yield ac
