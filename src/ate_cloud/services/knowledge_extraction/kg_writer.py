"""Knowledge-graph persistence for extracted requirements/cases/results.

The ONLY module in the extraction package that builds Cypher. It writes the
traceability ontology nodes (Product / TestRequirement / TestCase /
TestStep / UUTResult) and relationships through the backend-agnostic
:class:`~ate_cloud.services.graph_service.GraphService` using batched
``UNWIND ... MERGE`` keyed on stable ids — the same persistence idiom as
:mod:`ate_cloud.services.kg_seed_writer` and the task-7 pipeline. Re-running
extraction MERGEs on the same ids and never duplicates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ate_cloud.services.graph_service import GraphService

logger = logging.getLogger(__name__)

#: Ontology labels written by extraction (range index on ``id`` per label).
_KG_LABELS: tuple[str, ...] = (
    "Product", "TestRequirement", "TestCase", "TestStep", "UUTResult",
)


@dataclass(frozen=True, slots=True)
class KGNode:
    """One ontology node to MERGE (``node_id`` is the idempotency key)."""

    label: str
    node_id: str
    name: str
    props: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KGEdge:
    """One ontology relationship (by node id)."""

    src: str
    rel: str
    dst: str


async def write_knowledge_graph(
    graph: GraphService, nodes: list[KGNode], edges: list[KGEdge]
) -> None:
    """Persist extracted nodes + edges through the GraphService (idempotent).

    Nodes are MERGEd by stable ``id`` (grouped per label); relationships are
    MERGEd by endpoint ids (grouped per relationship type).
    """
    await graph.create_constraints()
    await _ensure_id_indexes(graph)

    by_label: dict[str, list[KGNode]] = {}
    for node in nodes:
        by_label.setdefault(node.label, []).append(node)
    for label, label_nodes in by_label.items():
        await _write_nodes(graph, label, label_nodes)
    await _write_edges(graph, edges)

    logger.info(
        "Extraction KG persisted: %d nodes across %d labels, %d relationships",
        len(nodes), len(by_label), len(edges),
    )


async def _ensure_id_indexes(graph: GraphService) -> None:
    """Best-effort range indexes on the extraction labels' ``id`` property."""
    for label in _KG_LABELS:
        try:
            await graph.write(f"CREATE INDEX FOR (e:{label}) ON (e.id)")
        except Exception as exc:  # noqa: BLE001 - index exists / DDL quirk
            logger.debug("Extraction id index for %s not created: %s", label, exc)


async def _write_nodes(graph: GraphService, label: str, nodes: list[KGNode]) -> None:
    rows = [
        {"id": n.node_id, "name": n.name, "props": _scalar_props(n.props)}
        for n in nodes
    ]
    statement = (
        f"UNWIND $rows AS row "
        f"MERGE (n:{label} {{id: row.id}}) "
        f"SET n.name = row.name "
        f"SET n += row.props"
    )
    await graph.write(statement, {"rows": rows})


async def _write_edges(graph: GraphService, edges: list[KGEdge]) -> None:
    by_type: dict[str, list[dict[str, str]]] = {}
    for edge in edges:
        by_type.setdefault(edge.rel, []).append({"src": edge.src, "dst": edge.dst})

    for rel_type, rows in by_type.items():
        statement = (
            "UNWIND $rows AS row "
            "MATCH (s {id: row.src}) "
            "MATCH (d {id: row.dst}) "
            f"MERGE (s)-[r:{rel_type}]->(d)"
        )
        await graph.write(statement, {"rows": rows})


def _scalar_props(props: dict[str, Any]) -> dict[str, Any]:
    """Keep only non-null scalar properties for Cypher parameter binding."""
    return {k: v for k, v in props.items() if v is not None}


__all__ = ["KGEdge", "KGNode", "write_knowledge_graph"]
