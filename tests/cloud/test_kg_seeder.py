"""Tests for the ontology-aligned KG seeder (task 8).

The seeder writes ontology entities/relationships (Fault/Symptom/Cause/
Solution/Component/Product/Instrument + the fault chain) through the
GraphService protocol via Cypher ``UNWIND ... MERGE`` keyed on stable ids.

These tests use an in-memory FAKE GraphService that models the MERGE
contract (nodes keyed by ``id``, edges keyed by src/rel/dst) so idempotency
is proven by running the seed TWICE and asserting counts do not grow — no
live FalkorDB/Qdrant required.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import pytest

from ate_cloud.services.kg_seed_data import FAULT_RECORDS
from ate_cloud.services.kg_seed_facts import build_seed_graph
from ate_cloud.services.kg_seeder import KGSeeder
from ate_cloud.services.ontology.vocab import (
    FaultCategory,
    FaultKind,
    InstrumentKind,
)


class InMemoryGraphService:
    """Fake GraphService modeling id-based MERGE idempotency.

    Interprets the two statement shapes the seeder emits:
      * node upsert: params ``{"rows": [{"id","name","props"}]}`` → MERGE by id;
      * edge upsert: params ``{"rows": [{"src","dst"}]}``       → MERGE by (src,rel,dst).
    Index/constraint DDL is a no-op; count_nodes/count_relationships read stores.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}  # id -> {label, name, props}
        self.edges: set[tuple[str, str, str]] = set()
        self.writes: list[tuple[str, dict[str, Any] | None]] = []
        self.fail_with: Exception | None = None

    async def query(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if "count(n)" in statement:
            return [{"total": len(self.nodes)}]
        if "count(r)" in statement:
            return [{"total": len(self.edges)}]
        return []

    async def write(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self.fail_with is not None:
            raise self.fail_with
        self.writes.append((statement, params))
        if "CREATE INDEX" in statement or "CONSTRAINT" in statement:
            return []
        rows = (params or {}).get("rows", [])
        if rows and "id" in rows[0]:
            label = _label_from_node_stmt(statement)
            for row in rows:
                existing = self.nodes.get(row["id"], {})
                existing.update({"label": label, "name": row["name"], **(row.get("props") or {})})
                self.nodes[row["id"]] = existing
        elif rows and "src" in rows[0]:
            rel = _rel_from_edge_stmt(statement)
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

    def label_counts(self) -> dict[str, int]:
        return Counter(n["label"] for n in self.nodes.values())

    def edge_counts(self) -> dict[str, int]:
        return Counter(rel for _s, rel, _d in self.edges)


def _label_from_node_stmt(stmt: str) -> str:
    match = re.search(r"MERGE \(n:(\w+)", stmt)
    assert match, f"no node label in statement: {stmt}"
    return match.group(1)


def _rel_from_edge_stmt(stmt: str) -> str:
    match = re.search(r"MERGE \(s\)-\[r:(\w+)\]", stmt)
    assert match, f"no rel type in statement: {stmt}"
    return match.group(1)


# ── Ontology mapping (facts) ──────────────────────────────────────────────


def test_seed_preserves_all_104_facts() -> None:
    nodes, _edges = build_seed_graph()
    assert len(nodes["Fault"]) == 104
    assert len(FAULT_RECORDS) == 104


def test_seed_nodes_use_unified_vocab_ids() -> None:
    nodes, _edges = build_seed_graph()
    # Every Fault carries a valid FaultKind + FaultCategory from the unified vocab.
    for fault in nodes["Fault"]:
        assert FaultKind(fault.props["fault_kind"])
        assert FaultCategory(fault.props["fault_category"])
        assert fault.props["error_code"]
    # Every Instrument is one canonical node per InstrumentKind (unified vocab).
    instrument_kinds = {n.props["instrument_kind"] for n in nodes["Instrument"]}
    assert instrument_kinds == {k.value for k in InstrumentKind}
    assert len(nodes["Instrument"]) == len(list(InstrumentKind))


def test_seed_node_ids_are_stable_and_unique_per_label() -> None:
    nodes, _edges = build_seed_graph()
    for label, rows in nodes.items():
        ids = [n.node_id for n in rows]
        assert len(ids) == len(set(ids)), f"duplicate ids under {label}"
    # Deterministic across builds.
    again_nodes, again_edges = build_seed_graph()
    assert {n.node_id for n in again_nodes["Fault"]} == {n.node_id for n in nodes["Fault"]}
    assert len(again_edges) == len(build_seed_graph()[1])


def test_seed_edges_have_no_dangling_endpoints() -> None:
    nodes, edges = build_seed_graph()
    all_ids = {n.node_id for rows in nodes.values() for n in rows}
    for edge in edges:
        assert edge.src in all_ids, edge
        assert edge.dst in all_ids, edge


def test_seed_contains_fault_chain_relationships() -> None:
    _nodes, edges = build_seed_graph()
    rel_types = {e.rel for e in edges}
    assert {
        "HAS_SYMPTOM", "HAS_CAUSE", "HAS_SOLUTION",
        "AFFECTS_COMPONENT", "OCCURS_IN_PRODUCT", "DIAGNOSED_WITH",
    } <= rel_types


def test_seed_has_no_legacy_labels_or_errorcode_entity() -> None:
    """The old ad-hoc FaultSymptom/ErrorCode labels are gone from the seed."""
    nodes, _edges = build_seed_graph()
    labels = set(nodes)
    assert "FaultSymptom" not in labels
    assert "ErrorCode" not in labels
    # Error code survives as a Fault property, not a node.
    assert all(n.props["error_code"] for n in nodes["Fault"])


# ── Persistence via GraphService (idempotency) ────────────────────────────


@pytest.fixture
def graph() -> InMemoryGraphService:
    return InMemoryGraphService()


async def test_seed_all_writes_ontology_nodes_and_edges(graph: InMemoryGraphService) -> None:
    seeder = KGSeeder(graph)  # type: ignore[arg-type]
    result = await seeder.seed_all()

    labels = graph.label_counts()
    assert labels["Fault"] == 104
    assert labels["Symptom"] == 104
    assert labels["Cause"] == 104
    assert labels["Solution"] == 104
    assert labels["Instrument"] == len(list(InstrumentKind))
    assert labels["Component"] >= 60
    assert labels["Product"] >= 20
    assert result["facts_seeded"] == 104
    assert result["nodes_created"] == len(graph.nodes)
    assert result["relationships_created"] == len(graph.edges)


async def test_seed_all_is_idempotent_across_two_runs(graph: InMemoryGraphService) -> None:
    seeder = KGSeeder(graph)  # type: ignore[arg-type]
    await seeder.seed_all()
    nodes_after_first = len(graph.nodes)
    edges_after_first = len(graph.edges)
    assert nodes_after_first > 0

    await seeder.seed_all()  # re-run must not duplicate
    assert len(graph.nodes) == nodes_after_first
    assert len(graph.edges) == edges_after_first


async def test_seed_writes_use_merge_on_stable_id_not_free_text(graph: InMemoryGraphService) -> None:
    seeder = KGSeeder(graph)  # type: ignore[arg-type]
    await seeder.seed_all()
    node_writes = [s for s, p in graph.writes if "MERGE (n:" in s]
    assert node_writes
    for stmt in node_writes:
        assert "id: row.id" in stmt  # MERGE keyed on stable id
        # No per-record raw MERGE on a free-text name property.
        assert "name: row.name" not in stmt


async def test_seed_degrades_when_graph_write_fails(graph: InMemoryGraphService) -> None:
    """A graph failure propagates to the caller (route maps to 502/503); app boot is unaffected."""
    graph.fail_with = RuntimeError("FalkorDB unreachable")
    seeder = KGSeeder(graph)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="unreachable"):
        await seeder.seed_all()


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
