"""Real FalkorDB (Redis RESP/6379) integration tests — skip-until task 31.

FalkorDB is NOT installed on 192.168.5.24 yet (provisioning is task 31,
gated on user approval). These tests therefore SKIP cleanly today:

* the conftest TCP probe skips when 6379 is closed (the current state);
* even when 6379 answers, a plain Redis without the FalkorDB module would
  fail a graph command — a missing graph module also maps to a skip so the
  suite stays green and "lights up" only once FalkorDB is live.

They drive the REAL async falkordb client through the production
``FalkorDBGraphService`` (the same seam the ``/health/ready`` endpoint
uses), so after deployment they verify the FMEA graph backend end to end.
"""

from __future__ import annotations

import pytest

from ate_cloud.config import settings
from ate_cloud.services.falkordb_graph_service import FalkorDBGraphService

pytestmark = pytest.mark.integration

#: Graph key the platform uses (settings.falkordb_graph, default "fmea").
_GRAPH_NAME = settings.falkordb_graph


def _service(target, password) -> FalkorDBGraphService:
    return FalkorDBGraphService(
        url=target.url,
        graph_name=_GRAPH_NAME,
        password=password,
    )


async def test_falkordb_redis_ping(require_falkordb, falkordb_password) -> None:
    """Given a RESP server on 6379, when we PING through the graph service, it answers.

    Uses the exact production health() path (breaker-protected Redis PING).
    """
    service = _service(require_falkordb, falkordb_password)
    info = await service.health()
    assert info.get("status") == "healthy"


async def test_falkordb_graph_module_available(require_falkordb, falkordb_password) -> None:
    """Given FalkorDB, the graph module answers a read query on the FMEA graph.

    A plain Redis (no graph module) returns an unknown-command error, which
    we treat as a skip (FalkorDB not yet deployed) rather than a failure.
    """
    service = FalkorDBGraphService(
        url=require_falkordb.url,
        graph_name=_GRAPH_NAME,
        password=falkordb_password,
    )
    try:
        result = await service.query("MATCH (n) RETURN count(n) AS c")
    except Exception as exc:  # module missing / not FalkorDB ⇒ skip, not fail
        message = str(exc).lower()
        if "unknown command" in message or "graph" in message and "command" in message:
            pytest.skip(f"RESP/6379 reachable but FalkorDB graph module absent: {exc}")
        raise
    # Query returns positional-then-dict rows; a count query yields one row.
    assert isinstance(result, list)
