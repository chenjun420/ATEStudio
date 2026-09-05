"""Tests for the FalkorDB graph section of the readiness health surface.

GET /api/v1/health/ready aggregates named dependency probes. The graph
section is NON-FATAL by contract (graph presence is not a boot
requirement — NATS remains the only fatal dependency):

- graph up (GraphService.health() answers)             -> ``graph: "ok"``
- graph down (health() raises / no reachable backend)  -> ``graph: "down"``

In both cases the endpoint returns HTTP 200 with per-component status so
operators see a degraded-but-alive service; it never 500s or crashes the
app. The graph service is obtained lazily — no socket is opened until the
first readiness probe — and cached on ``app.state.graph_service``.

No live FalkorDB/Redis is required: a stub GraphService is injected via
``app.state`` (the same cache slot the faults/diagnose lazy factories
write to).
"""

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


class _StubGraphService:
    """Minimal GraphService double whose health() is configurable."""

    def __init__(self, *, healthy: bool) -> None:
        self._healthy = healthy
        self.ping_count = 0

    async def health(self) -> dict[str, Any]:
        self.ping_count += 1
        if self._healthy:
            return {"status": "healthy", "backend": "falkordb"}
        raise ConnectionError("FalkorDB unreachable: connection refused")


def _set_graph(app: FastAPI, service: object) -> None:
    """Inject a (stub) graph service into the app.state cache slot."""
    app.state.graph_service = service


async def _get_ready(app: FastAPI) -> Any:
    """GET /api/v1/health/ready on a one-shot ASGI client."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        return await ac.get("/api/v1/health/ready")


class TestGraphHealthReady:
    """Graph section of GET /api/v1/health/ready."""

    @pytest.mark.asyncio
    async def test_graph_ok_when_health_answers(self, app: FastAPI) -> None:
        """Given: a graph service whose health() answers
        When:  GET /api/v1/health/ready
        Then:  200 and body["graph"] == "ok" (other sections unaffected)
        """
        _set_graph(app, _StubGraphService(healthy=True))

        response = await _get_ready(app)

        assert response.status_code == 200
        data = response.json()
        assert data["graph"] == "ok"
        assert "database" in data and "nats" in data

    @pytest.mark.asyncio
    async def test_graph_down_when_health_raises(self, app: FastAPI) -> None:
        """Given: a graph service whose health() raises (backend down)
        When:  GET /api/v1/health/ready
        Then:  200 (graceful degrade, NOT a 500/crash) and
               body["graph"] == "down"; sibling sections still reported
        """
        _set_graph(app, _StubGraphService(healthy=False))

        response = await _get_ready(app)

        assert response.status_code == 200
        data = response.json()
        assert data["graph"] == "down"
        assert "database" in data
        assert "nats" in data

    @pytest.mark.asyncio
    async def test_graph_down_without_live_backend_and_cached_lazily(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given: no app.state.graph_service and the configured FalkorDB
               URL points at a guaranteed-dead port (no live backend)
        When:  GET /api/v1/health/ready is called
        Then:  200, body["graph"] == "down", and the lazily-constructed
               service is cached on app.state (construction happens once,
               on first probe — never at import/boot)
        """
        from ate_cloud.config import settings

        # Dead local port: connection refused fast, independent of whether
        # a dev FalkorDB happens to listen on the default 6379.
        monkeypatch.setattr(settings, "falkordb_url", "redis://127.0.0.1:6399")

        response = await client.get("/api/v1/health/ready")

        assert response.status_code == 200
        assert response.json()["graph"] == "down"
        assert getattr(client.app.state, "graph_service", None) is not None

    @pytest.mark.asyncio
    async def test_graph_service_absent_at_boot(self, app: FastAPI) -> None:
        """Given: a freshly built app that has never run a probe
        When:  app.state is inspected before any /health/ready call
        Then:  graph_service is absent — no graph connection at boot/import
        """
        assert getattr(app.state, "graph_service", None) is None
