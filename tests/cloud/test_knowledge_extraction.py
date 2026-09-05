"""Tests for task 12: deterministic knowledge extraction into ORM + KG.

Covers the traceability chain TestRequirement → TestCase → DSL step →
(recorded) UUT result, built deterministically from DSL YAML plans and
recordings JSONL, with ATML import driven through the task-11 importer.

All graph persistence goes through an in-memory FAKE GraphService that
models the Cypher MERGE contract (nodes keyed by ``id``, edges keyed by
src/rel/dst), so idempotency is proven by running extraction TWICE — no
live FalkorDB/Qdrant is required.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from ate_cloud.models.knowledge import TestCase, TestRequirement
from ate_cloud.services.knowledge_extraction import KnowledgeExtractionService
from ate_cloud.services.knowledge_extraction.recordings import read_recording

FIXTURES = Path(__file__).parents[1] / "fixtures"
SAMPLE_PLAN = FIXTURES / "sample_plan.yaml"

SAMPLE_1671_XML = """<?xml version="1.0" encoding="UTF-8"?>
<TestDescription xmlns="urn:IEEE-1671:2010:TestDescription">
  <UUT><Identifier>ATML-DEMO</Identifier></UUT>
  <TestRequirement id="REQ-ATML-1" name="Voltage present">
    <Description>The 5 V rail must be present.</Description>
  </TestRequirement>
  <TestGroups><TestGroup id="G1">
    <Test id="TC-ATML-1" name="Measure 5V" requirementId="REQ-ATML-1" />
  </TestGroup></TestGroups>
</TestDescription>"""


class InMemoryGraph:
    """Fake GraphService modeling id-based MERGE (mirrors test_kg_seeder)."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: set[tuple[str, str, str]] = set()

    async def query(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return []

    async def write(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if "CREATE INDEX" in statement or "CONSTRAINT" in statement:
            return []
        rows = (params or {}).get("rows", [])
        if not rows:
            return []
        if "id" in rows[0] and "MERGE (n:" in statement:
            label = re.search(r"MERGE \(n:(\w+)", statement).group(1)
            for row in rows:
                node = self.nodes.get(row["id"], {})
                node.update({"label": label, "name": row["name"], **(row.get("props") or {})})
                self.nodes[row["id"]] = node
        else:
            rel = re.search(r"\[r:(\w+)\]", statement).group(1)
            for row in rows:
                self.edges.add((row["src"], rel, row["dst"]))
        return []

    async def create_constraints(self) -> None:
        return None

    async def count_nodes(self) -> int:
        return len(self.nodes)

    async def count_relationships(self) -> int:
        return len(self.edges)

    async def health(self) -> dict[str, Any]:
        return {"status": "healthy", "backend": "in-memory"}

    def labels(self) -> Counter[str]:
        return Counter(n["label"] for n in self.nodes.values())

    def rels(self) -> Counter[str]:
        return Counter(rel for _s, rel, _d in self.edges)


def _write_recording(path: Path, execution_id: str, lines: list[dict[str, Any]]) -> None:
    header = {"kind": "recording_header", "version": 1, "execution_id": execution_id}
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(header) + "\n")
        for i, line in enumerate(lines):
            line = {"seq": i, "t": float(i), "execution_id": execution_id, **line}
            fh.write(json.dumps(line) + "\n")


@pytest.fixture
def graph() -> InMemoryGraph:
    return InMemoryGraph()


@pytest.fixture
def service(graph: InMemoryGraph) -> KnowledgeExtractionService:
    return KnowledgeExtractionService(graph=graph)  # type: ignore[arg-type]


# ── DSL YAML → requirement/case/step chain ────────────────────────────────


async def test_dsl_yaml_builds_requirement_case_step_chain(
    db_session, service: KnowledgeExtractionService, graph: InMemoryGraph
) -> None:
    result = await service.extract_dsl_yaml(
        db_session, SAMPLE_PLAN, product_code="DEMO-BOARD"
    )
    await db_session.commit()

    assert result.requirements.created == 1
    assert result.cases.created == 3

    req = (
        await db_session.execute(
            select(TestRequirement).where(TestRequirement.product_code == "DEMO-BOARD")
        )
    ).scalar_one()
    assert req.source == "dsl"
    assert req.requirement_code.startswith("REQ-DSL-")

    cases = (
        await db_session.execute(
            select(TestCase).where(TestCase.requirement_id == req.id).order_by(TestCase.case_code)
        )
    ).scalars().all()
    assert [c.step_id for c in cases] == ["step_init", "step_measure", "step_validate"]
    assert all(c.status == "active" for c in cases)

    labels = graph.labels()
    assert labels["TestRequirement"] == 1
    assert labels["TestCase"] == 3
    assert labels["TestStep"] == 3
    assert labels["Product"] == 1
    rels = graph.rels()
    assert rels["HAS_REQUIREMENT"] == 1
    assert rels["VERIFIED_BY"] == 3
    assert rels["HAS_STEP"] == 3
    # Every case node links to its DSL step node by the stable step id.
    step_ids = {n.get("step_id") for n in graph.nodes.values() if n["label"] == "TestCase"}
    assert step_ids == {"step_init", "step_measure", "step_validate"}


async def test_dsl_extraction_is_idempotent_on_rerun(
    db_session, service: KnowledgeExtractionService, graph: InMemoryGraph
) -> None:
    first = await service.extract_dsl_yaml(db_session, SAMPLE_PLAN, product_code="DEMO-BOARD")
    await db_session.commit()
    assert first.requirements.created == 1 and first.cases.created == 3
    nodes_after_first = len(graph.nodes)
    edges_after_first = len(graph.edges)

    second = await service.extract_dsl_yaml(db_session, SAMPLE_PLAN, product_code="DEMO-BOARD")
    await db_session.commit()
    assert second.requirements.created == 0 and second.requirements.updated == 1
    assert second.cases.created == 0 and second.cases.updated == 3
    req_count = len(
        (await db_session.execute(select(TestRequirement))).scalars().all()
    )
    case_count = len((await db_session.execute(select(TestCase))).scalars().all())
    assert req_count == 1 and case_count == 3
    assert len(graph.nodes) == nodes_after_first
    assert len(graph.edges) == edges_after_first


async def test_dsl_extraction_works_without_graph_backend(db_session) -> None:
    """App must boot/run with no graph: ORM rows still created, no raise."""
    service = KnowledgeExtractionService(graph=None)
    result = await service.extract_dsl_yaml(
        db_session, SAMPLE_PLAN, product_code="DEMO-BOARD"
    )
    await db_session.commit()
    assert result.requirements.created == 1 and result.cases.created == 3


# ── Recordings → UUT results linked to DSL steps ──────────────────────────


async def test_recording_links_executed_results_to_dsl_steps(
    db_session, tmp_path: Path, service: KnowledgeExtractionService, graph: InMemoryGraph
) -> None:
    await service.extract_dsl_yaml(db_session, SAMPLE_PLAN, product_code="DEMO-BOARD")
    await db_session.commit()

    rec = tmp_path / "rec_demo.jsonl"
    _write_recording(
        rec,
        "run-001",
        [
            {"kind": "step_started", "step_id": "step_measure"},
            {"kind": "instrument_call", "resource": "DMM_1", "method": "measure"},
            {"kind": "step_completed", "step_id": "step_measure"},
            {"kind": "step_started", "step_id": "step_validate"},
            {"kind": "step_failed", "step_id": "step_validate", "error": "3.0V out of range"},
        ],
    )

    result = await service.extract_recordings(db_session, [rec])
    assert result.files_read == 1
    assert result.results_written == 2
    assert result.skipped_events == 0

    labels = graph.labels()
    assert labels["UUTResult"] == 2
    rels = graph.rels()
    assert rels["PRODUCED_RESULT"] == 2
    results = [n for n in graph.nodes.values() if n["label"] == "UUTResult"]
    outcomes = {n["outcome"] for n in results}
    assert outcomes == {"Passed", "Failed"}
    # Result edges land on the stable DSL step nodes (no dangling endpoints).
    step_node_ids = {nid for nid, n in graph.nodes.items() if n["label"] == "TestStep"}
    for src, rel, _dst in graph.edges:
        if rel == "PRODUCED_RESULT":
            assert src in step_node_ids


async def test_recording_missing_step_field_is_skipped_with_warning(
    db_session, tmp_path: Path, service: KnowledgeExtractionService, caplog
) -> None:
    await service.extract_dsl_yaml(db_session, SAMPLE_PLAN, product_code="DEMO-BOARD")
    await db_session.commit()

    rec = tmp_path / "rec_bad.jsonl"
    _write_recording(
        rec,
        "run-002",
        [
            {"kind": "step_started"},  # malformed: no step_id
            {"kind": "step_failed", "error": "boom"},  # malformed: no step_id
            {"kind": "step_completed", "step_id": "step_init"},  # valid
        ],
    )

    with caplog.at_level("WARNING"):
        result = await service.extract_recordings(db_session, [rec])

    assert result.skipped_events == 2
    assert result.results_written == 1
    assert "step_id" in caplog.text and "skipping" in caplog.text.lower()


async def test_legacy_instrument_only_recording_yields_no_results(
    db_session, tmp_path: Path, service: KnowledgeExtractionService
) -> None:
    """The shipped data/recordings/*.jsonl files carry instrument calls only."""
    rec = tmp_path / "rec_legacy.jsonl"
    with rec.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"resource_id": "ELOAD_1", "action": "connect"}) + "\n")
        fh.write("not-a-json-line\n")  # torn tail line must not crash

    result = await service.extract_recordings(db_session, [rec])
    assert result.files_read == 1
    assert result.results_written == 0
    assert result.skipped_events == 0


def test_read_recording_aggregates_outcome_per_step(tmp_path: Path) -> None:
    rec = tmp_path / "r.jsonl"
    _write_recording(
        rec,
        "run-9",
        [
            {"kind": "step_started", "step_id": "s1"},
            {"kind": "step_completed", "step_id": "s1"},
            {"kind": "step_started", "step_id": "s2"},
            {"kind": "step_failed", "step_id": "s2", "error": "x"},
            {"kind": "step_started", "step_id": "s3"},  # started only
        ],
    )
    events, skipped = read_recording(rec)
    assert skipped == 0
    outcomes = {e.step_id: e.outcome for e in events}
    assert outcomes == {"s1": "Passed", "s2": "Failed", "s3": "Inconclusive"}


# ── ATML drive-through ────────────────────────────────────────────────────


async def test_atml_import_is_driven_and_synced_to_kg(
    db_session, service: KnowledgeExtractionService, graph: InMemoryGraph
) -> None:
    result = await service.extract_atml(db_session, SAMPLE_1671_XML)
    await db_session.commit()
    assert result.requirements.created == 1
    assert result.cases.created == 1

    labels = graph.labels()
    assert labels["TestRequirement"] == 1
    assert labels["TestCase"] == 1
    req_props = next(n for n in graph.nodes.values() if n["label"] == "TestRequirement")
    assert req_props["source"] == "atml"
    assert graph.rels()["VERIFIED_BY"] == 1


# ── HTTP route (thin smoke) ───────────────────────────────────────────────


async def test_extract_route_runs_deterministic_extraction(client) -> None:
    """POST /api/v1/knowledge/extract over a real fixture YAML (ORM-only graph)."""
    from ate_cloud.api.v1.knowledge import get_extraction_service

    client.app.dependency_overrides[get_extraction_service] = lambda: (
        KnowledgeExtractionService(graph=None)
    )
    response = await client.post(
        "/api/v1/knowledge/extract",
        json={
            "product_code": "ROUTE-DEMO",
            "dsl_paths": [str(SAMPLE_PLAN)],
            "recording_paths": [],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["product_code"] == "ROUTE-DEMO"
    assert body["requirements"]["created"] == 1
    assert body["cases"]["created"] == 3
    assert body["graph_status"] == "degraded"  # no graph backend in test app


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
