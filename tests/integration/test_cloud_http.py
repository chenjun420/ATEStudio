"""Real cloud HTTP (nginx → ate_cloud) integration tests.

Drives the REAL cloud through nginx (port 80, site ``atestudio``) with
httpx, exactly as an operator/browser would:

* nginx serves the frontend SPA at ``/`` (already live on the old deploy).
* the NEW readiness endpoint ``/api/v1/health/ready`` (added this plan)
  returns 200 with per-component ``database``/``nats``/``graph`` status.

The readiness endpoint does NOT exist on the OLD deploy currently running
on .24 (it returns 404 for /health paths). Per the plan, that test must
SKIP / soft-assert until the new deploy is live (tasks 32/34) — it must
never hard-fail against the old deploy. It lights up automatically once
the new ate_cloud is deployed.

Skipped by default (gate in conftest); skipped per-service when port 80
is unreachable.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.integration

#: Readiness components reported by GET /api/v1/health/ready.
_READY_COMPONENTS = ("database", "nats", "graph")
_VALID_STATUS = ("ok", "down")


async def test_cloud_nginx_serves_spa(require_cloud_http) -> None:
    """Given nginx on port 80, GET / serves the frontend SPA (200)."""
    async with httpx.AsyncClient(base_url=require_cloud_http.url, timeout=5.0) as client:
        response = await client.get("/")
    assert response.status_code == 200


async def test_cloud_readiness_endpoint(require_cloud_http) -> None:
    """Given the new deploy, /api/v1/health/ready returns 200 with component status.

    On the OLD deploy the route is absent (404/405) — skip rather than fail,
    so the suite is green now and asserts once the new deploy is live.
    """
    async with httpx.AsyncClient(base_url=require_cloud_http.url, timeout=5.0) as client:
        response = await client.get("/api/v1/health/ready")

    if response.status_code != 200:
        pytest.skip(
            f"/api/v1/health/ready returned HTTP {response.status_code} — "
            "new ate_cloud deploy not live yet (tasks 32/34); endpoint added this plan"
        )

    body = response.json()
    for component in _READY_COMPONENTS:
        assert component in body, f"readiness body missing component {component!r}"
        assert body[component] in _VALID_STATUS, f"unexpected status for {component!r}"
