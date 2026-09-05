"""Task: knowledge READ APIs supporting frontend tasks 25/26.

Covers the GET surface added to the EXISTING knowledge router
(``/api/v1/knowledge`` — no new mount, so the auth sentinel stays
protected==27 / anonymous==5):

- ``GET /knowledge/requirements`` — paged TestRequirement list with
  ``product_code`` / ``source`` filters ({items,total} envelope like fmea).
- ``GET /knowledge/cases`` — paged TestCase list with ``requirement_id`` /
  ``product_code`` filters; each row carries its requirement link plus the
  DSL ``sequence_id``/``step_id`` mapping for the traceability matrix.
- ``GET /knowledge/traceability`` — requirement → cases → DSL-step tree
  filtered by ``product_code`` (unlinked cases land on a None-requirement
  bucket so matrix gaps stay visible).
- ``GET /knowledge/graph`` — {nodes, edges} browse payload sourced through
  the GraphService protocol; 503 with a clear message when no graph backend
  is configured/healthy (graph browse data lives in the graph, unlike the
  extraction endpoint which degrades to ORM-only).

Graph tests use an in-memory ``BrowseGraphFake`` that answers the two
statement SHAPES the browse helper emits (label-scoped / full scans) — no
live FalkorDB, same shape-dispatch approach as ``ontology_graph_fake.py``.
The mount-level JWT guard (anonymous -> 401) is owned by
``test_auth_enforcement.py``; one anon-401 smoke is included here too.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ate_cloud.models.knowledge import TestCase, TestRequirement

# ── Fakes ──────────────────────────────────────────────────────────────────


class BrowseGraphFake:
    """In-memory GraphService fake for the graph-browse endpoint.

    Holds nodes as id -> {"id","label","name",**props} and edges as
    (src, rel, dst). ``query`` dispatches on statement SHAPE (no Cypher
    parser): node scans (``labels(n)`` / ``RETURN n``) and relationship
    scans (``-[r]->`` / ``type(r)``). ``fail_with`` forces every query to
    raise (graph-outage 503 path); ``unavailable=True`` simulates a backend
    whose health() probe fails.
    """

    def __init__(
        self, *, unavailable: bool = False, fail_with: Exception | None = None
    ) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[tuple[str, str, str]] = []
        self.queries: list[str] = []
        self._unavailable = unavailable
        self._fail_with = fail_with

    def add_node(
        self, node_id: str, label: str, name: str = "", **props: Any
    ) -> BrowseGraphFake:
        self.nodes[node_id] = {"id": node_id, "label": label, "name": name, **props}
        return self

    def add_edge(self, src: str, rel: str, dst: str) -> BrowseGraphFake:
        self.edges.append((src, rel, dst))
        return self

    def seed_small(self) -> BrowseGraphFake:
        """A tiny requirement→case→step graph (task-12 writer shapes)."""
        self.add_node("product:DEMO", "Product", "DEMO-BOARD", product_code="DEMO")
        self.add_node("req:1", "TestRequirement", "REQ-1", requirement_code="REQ-1")
        self.add_node("case:1", "TestCase", "TC-1", case_code="TC-1", step_id="step_a")
        self.add_node("step:1", "TestStep", "step_a", step_id="step_a")
        self.add_node("fault:1", "Fault", "OVP fault", error_code="E-OVP-01")
        self.add_edge("product:DEMO", "HAS_REQUIREMENT", "req:1")
        self.add_edge("req:1", "VERIFIED_BY", "case:1")
        self.add_edge("case:1", "HAS_STEP", "step:1")
        return self

    async def query(
        self, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if self._unavailable:
            raise ConnectionError("FalkorDB unreachable: connection refused")
        if self._fail_with is not None:
            raise self._fail_with
        self.queries.append(statement)
        params = params or {}
        if "labels(n)" in statement:
            # Node browse scan: return scalar columns aliased exactly as the
            # graph_browse Cypher projects them (id/labels/name/properties).
            # The label filter is the bound ``$label`` param ('' = no filter).
            wanted = str((params or {}).get("label", "") or "")
            labels = {wanted} if wanted else set()
            limit = int(params.get("limit", 100))
            rows: list[dict[str, Any]] = []
            for node in self.nodes.values():
                if labels and node["label"] not in labels:
                    continue
                props = {k: v for k, v in node.items() if k not in ("id", "label")}
                rows.append(
                    {
                        "id": node["id"],
                        "labels": [node["label"]],
                        "name": node.get("name", ""),
                        "properties": props,
                    }
                )
            return rows[:limit]
        if "type(r)" in statement or "-[r]->" in statement:
            return [
                {"source": s, "target": d, "type": rel}
                for (s, rel, d) in self.edges
            ]
        return []

    async def write(
        self, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return []

    async def create_constraints(self) -> None:
        return None

    async def count_nodes(self) -> int:
        return len(self.nodes)

    async def count_relationships(self) -> int:
        return len(self.edges)

    async def health(self) -> dict[str, Any]:
        if self._unavailable or self._fail_with is not None:
            raise ConnectionError("FalkorDB unreachable: connection refused")
        return {"status": "healthy", "backend": "in-memory-browse"}


def _set_graph(client: Any, service: Any | None) -> None:
    """Inject/clear the graph service in the app.state cache slot."""
    if service is None:
        if hasattr(client.app.state, "graph_service"):
            del client.app.state.graph_service
    else:
        client.app.state.graph_service = service


# ── ORM seed helpers ───────────────────────────────────────────────────────


async def _seed_requirement(
    db_session: Any,
    *,
    product_code: str = "DEMO-BOARD",
    requirement_code: str | None = None,
    source: str = "manual",
    title: str = "Requirement",
) -> TestRequirement:
    req = TestRequirement(
        id=str(uuid.uuid4()),
        product_code=product_code,
        requirement_code=requirement_code or f"REQ-{uuid.uuid4().hex[:6]}",
        title=title,
        source=source,
    )
    db_session.add(req)
    await db_session.flush()
    return req


async def _seed_case(
    db_session: Any,
    *,
    requirement_id: str | None = None,
    case_code: str | None = None,
    sequence_id: str | None = None,
    step_id: str = "",
    status: str = "active",
    title: str = "Case",
) -> TestCase:
    case = TestCase(
        id=str(uuid.uuid4()),
        requirement_id=requirement_id,
        case_code=case_code or f"TC-{uuid.uuid4().hex[:6]}",
        title=title,
        sequence_id=sequence_id,
        step_id=step_id,
        status=status,
    )
    db_session.add(case)
    await db_session.flush()
    return case


# ── GET /knowledge/requirements ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_requirements_list_paged_with_total(client: Any, db_session: Any) -> None:
    """Given 3 requirements, GET list returns the {items,total} envelope."""
    for _ in range(3):
        await _seed_requirement(db_session)
    await db_session.commit()

    resp = await client.get("/api/v1/knowledge/requirements")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    item = body["items"][0]
    assert {"id", "product_code", "requirement_code", "title", "source", "created_at"} <= set(item)


@pytest.mark.asyncio
async def test_requirements_pagination_skip_and_limit(client: Any, db_session: Any) -> None:
    """Given 5 requirements, skip=2&limit=2 returns 2 items but total stays 5."""
    for _ in range(5):
        await _seed_requirement(db_session)
    await db_session.commit()

    resp = await client.get(
        "/api/v1/knowledge/requirements", params={"skip": 2, "limit": 2}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


@pytest.mark.asyncio
async def test_requirements_filter_by_product_and_source(
    client: Any, db_session: Any
) -> None:
    """product_code and source filters narrow the paged list independently."""
    await _seed_requirement(db_session, product_code="P1", source="dsl")
    await _seed_requirement(db_session, product_code="P1", source="manual")
    await _seed_requirement(db_session, product_code="P2", source="dsl")
    await db_session.commit()

    by_product = await client.get(
        "/api/v1/knowledge/requirements", params={"product_code": "P1"}
    )
    assert by_product.status_code == 200
    assert by_product.json()["total"] == 2
    assert all(i["product_code"] == "P1" for i in by_product.json()["items"])

    by_source = await client.get(
        "/api/v1/knowledge/requirements", params={"source": "dsl"}
    )
    assert by_source.status_code == 200
    assert by_source.json()["total"] == 2
    assert all(i["source"] == "dsl" for i in by_source.json()["items"])

    both = await client.get(
        "/api/v1/knowledge/requirements",
        params={"product_code": "P1", "source": "manual"},
    )
    assert both.json()["total"] == 1


# ── GET /knowledge/cases ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cases_list_includes_requirement_link_and_dsl_mapping(
    client: Any, db_session: Any
) -> None:
    """Each case row carries requirement_id + sequence_id/step_id (matrix join)."""
    req = await _seed_requirement(db_session, product_code="DEMO-BOARD")
    await _seed_case(
        db_session,
        requirement_id=req.id,
        case_code="TC-1",
        sequence_id="seq-main",
        step_id="step_measure",
    )
    await db_session.commit()

    resp = await client.get("/api/v1/knowledge/cases")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["case_code"] == "TC-1"
    assert item["requirement_id"] == req.id
    assert item["sequence_id"] == "seq-main"
    assert item["step_id"] == "step_measure"


@pytest.mark.asyncio
async def test_cases_filter_by_requirement_and_product(
    client: Any, db_session: Any
) -> None:
    """requirement_id joins through to product; both filters narrow the list."""
    req_a = await _seed_requirement(db_session, product_code="PA")
    req_b = await _seed_requirement(db_session, product_code="PB")
    await _seed_case(db_session, requirement_id=req_a.id, case_code="TC-A1")
    await _seed_case(db_session, requirement_id=req_a.id, case_code="TC-A2")
    await _seed_case(db_session, requirement_id=req_b.id, case_code="TC-B1")
    await _seed_case(db_session, requirement_id=None, case_code="TC-ORPHAN")
    await db_session.commit()

    by_req = await client.get(
        "/api/v1/knowledge/cases", params={"requirement_id": req_a.id}
    )
    assert by_req.status_code == 200
    assert by_req.json()["total"] == 2
    assert {i["case_code"] for i in by_req.json()["items"]} == {"TC-A1", "TC-A2"}

    by_product = await client.get(
        "/api/v1/knowledge/cases", params={"product_code": "PB"}
    )
    assert by_product.json()["total"] == 1
    assert by_product.json()["items"][0]["case_code"] == "TC-B1"

    # Orphan cases (ingested before their requirement) are still listable:
    # they appear in the unfiltered list with requirement_id == None.
    all_cases = await client.get("/api/v1/knowledge/cases")
    assert all_cases.status_code == 200
    assert all_cases.json()["total"] == 4
    orphan = next(
        i for i in all_cases.json()["items"] if i["case_code"] == "TC-ORPHAN"
    )
    assert orphan["requirement_id"] is None


# ── GET /knowledge/traceability ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_traceability_builds_requirement_cases_step_tree(
    client: Any, db_session: Any
) -> None:
    """Tree shape: requirement -> cases[] with DSL sequence/step links."""
    req = await _seed_requirement(
        db_session, product_code="DEMO-BOARD", requirement_code="REQ-PSU-001"
    )
    await _seed_case(
        db_session,
        requirement_id=req.id,
        case_code="TC-VOLT",
        sequence_id="seq-psu",
        step_id="step_measure",
    )
    await _seed_case(
        db_session,
        requirement_id=req.id,
        case_code="TC-OVP",
        sequence_id="seq-psu",
        step_id="step_validate",
    )
    other = await _seed_requirement(db_session, product_code="OTHER", requirement_code="REQ-X")
    await _seed_case(db_session, requirement_id=other.id, case_code="TC-OTHER")
    await db_session.commit()

    resp = await client.get(
        "/api/v1/knowledge/traceability", params={"product_code": "DEMO-BOARD"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["product_code"] == "DEMO-BOARD"
    assert len(body["requirements"]) == 1
    node = body["requirements"][0]
    assert node["requirement_code"] == "REQ-PSU-001"
    assert {c["case_code"] for c in node["cases"]} == {"TC-VOLT", "TC-OVP"}
    volt = next(c for c in node["cases"] if c["case_code"] == "TC-VOLT")
    assert volt["sequence_id"] == "seq-psu"
    assert volt["step_id"] == "step_measure"


@pytest.mark.asyncio
async def test_traceability_includes_unlinked_cases_bucket(
    client: Any, db_session: Any
) -> None:
    """Cases with no requirement (ingestion gap) surface under a null bucket."""
    await _seed_case(db_session, requirement_id=None, case_code="TC-ORPHAN")
    await db_session.commit()

    resp = await client.get("/api/v1/knowledge/traceability")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Null bucket keeps matrix gaps visible instead of silently dropping rows.
    unlinked = body.get("unlinked_cases", [])
    assert any(c["case_code"] == "TC-ORPHAN" for c in unlinked)


# ── GET /knowledge/graph ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graph_returns_nodes_and_edges_shape(
    client: Any, db_session: Any
) -> None:
    """Graph browse returns {nodes:[{id,label,type,properties}], edges:[{source,target,type}]}."""
    graph = BrowseGraphFake().seed_small()
    _set_graph(client, graph)

    resp = await client.get("/api/v1/knowledge/graph")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["nodes"]) == 5
    assert len(body["edges"]) == 3
    node = body["nodes"][0]
    assert {"id", "label", "type", "properties"} <= set(node)
    # `type` mirrors the graph label for the UI; properties carry node props.
    assert node["type"] == node["label"]
    edge = body["edges"][0]
    assert {"source", "target", "type"} <= set(edge)
    assert edge["type"] in {"HAS_REQUIREMENT", "VERIFIED_BY", "HAS_STEP"}


@pytest.mark.asyncio
async def test_graph_respects_limit_and_label_filter(
    client: Any, db_session: Any
) -> None:
    """limit caps node count; label filter restricts node labels (edges pruned)."""
    graph = BrowseGraphFake().seed_small()
    _set_graph(client, graph)

    limited = await client.get("/api/v1/knowledge/graph", params={"limit": 2})
    assert limited.status_code == 200
    assert len(limited.json()["nodes"]) == 2

    filtered = await client.get(
        "/api/v1/knowledge/graph", params={"label": "TestCase"}
    )
    assert filtered.status_code == 200
    fbody = filtered.json()
    assert len(fbody["nodes"]) == 1
    assert fbody["nodes"][0]["label"] == "TestCase"
    # Edges touching only filtered-out nodes are pruned.
    assert all(
        e["source"] == "case:1" or e["target"] == "case:1" for e in fbody["edges"]
    )


@pytest.mark.asyncio
async def test_graph_503_when_no_graph_backend(
    client: Any, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No reachable graph backend -> honest 503 (data lives in the graph).

    Uses a guaranteed-dead local port (not the default 6379) so the result is
    deterministic even if a dev FalkorDB happens to run locally — the lazy
    service constructs fine (no socket at construction) but the browse query
    fails fast with connection refused -> 503; app stays up.
    """
    from ate_cloud.config import settings

    monkeypatch.setattr(settings, "falkordb_url", "redis://127.0.0.1:6399")
    _set_graph(client, None)

    resp = await client.get("/api/v1/knowledge/graph")

    assert resp.status_code == 503, resp.text
    assert "graph" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_graph_503_when_backend_unhealthy(
    client: Any, db_session: Any
) -> None:
    """A configured but unreachable graph (health raises) -> 503, app stays up."""
    _set_graph(client, BrowseGraphFake(unavailable=True))

    resp = await client.get("/api/v1/knowledge/graph")

    assert resp.status_code == 503
    assert "graph" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_graph_503_when_query_fails(client: Any, db_session: Any) -> None:
    """A breaker-open / connection error during query -> 503."""
    from ate_platform.common.circuit_breaker import CircuitBreakerOpenError

    _set_graph(client, BrowseGraphFake(fail_with=CircuitBreakerOpenError("open")))

    resp = await client.get("/api/v1/knowledge/graph")

    assert resp.status_code == 503


# ── Auth smoke (mount-level guard owned by test_auth_enforcement.py) ────────


@pytest.mark.asyncio
async def test_knowledge_read_endpoints_anonymous_401(
    client: Any, monkeypatch: pytest.MonkeyPatch, db_session: Any
) -> None:
    """Anonymous requests to the new GET endpoints are rejected with 401."""
    from ate_cloud.config import settings

    monkeypatch.setattr(settings, "dev_mode", False)
    for path in (
        "/api/v1/knowledge/requirements",
        "/api/v1/knowledge/cases",
        "/api/v1/knowledge/traceability",
        "/api/v1/knowledge/graph",
    ):
        resp = await client.get(path)
        assert resp.status_code == 401, f"{path}: expected 401, got {resp.status_code}"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
