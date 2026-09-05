"""Ontology-seed graph persistence — writes ontology entities via GraphService.

This is the ONLY module in the ontology-seed path that builds Cypher. It
persists the ontology :class:`~ate_cloud.services.kg_seed_facts.SeedNode` /
:class:`~ate_cloud.services.kg_seed_facts.SeedEdge` sets produced from the
deterministic ontology through the backend-agnostic
:class:`~ate_cloud.services.graph_service.GraphService` using batched
``UNWIND ... MERGE`` keyed on stable entity ids (the same persistence
idiom as :class:`ate_cloud.services.kg_pipeline.pipeline.KGPipeline`).

There is no Neo4j driver here and no per-record ad-hoc Cypher: node/edge
shapes come from the ontology mapping and the MERGE key is the stable
ontology id, so re-running the seed is idempotent.
"""

from __future__ import annotations

import logging
from typing import Any

from ate_cloud.services.graph_service import GraphService
from ate_cloud.services.kg_seed_facts import SeedEdge, SeedNode

logger = logging.getLogger(__name__)

# Ontology node labels seeded here; a range index on `id` per label makes the
# id-based MERGE fast and backs idempotency. (FalkorDBGraphService.
# create_constraints additionally indexes the legacy evolution labels on
# `name`.) Index creation is best-effort and tolerates "already exists".
_SEED_LABELS: tuple[str, ...] = (
    "Fault", "Symptom", "Cause", "Solution", "Component", "Product", "Instrument",
)


async def write_seed_graph(
    graph: GraphService,
    nodes_by_label: dict[str, list[SeedNode]],
    edges: list[SeedEdge],
) -> None:
    """Persist ontology seed nodes + edges through the GraphService (idempotent).

    Args:
        graph: The GraphService backend (FalkorDB in prod; fake in tests).
        nodes_by_label: Ontology label → seed nodes (deduplicated by id).
        edges: Ontology relationships (deduplicated by src/rel/dst).
    """
    await graph.create_constraints()
    await _ensure_id_indexes(graph)

    for label, nodes in nodes_by_label.items():
        await _write_nodes(graph, label, nodes)
    await _write_edges(graph, edges)

    logger.info(
        "Ontology seed persisted: %d nodes across %d labels, %d relationships",
        sum(len(rows) for rows in nodes_by_label.values()),
        len(nodes_by_label),
        len(edges),
    )


async def _ensure_id_indexes(graph: GraphService) -> None:
    """Best-effort range indexes on the seeded labels' ``id`` property."""
    for label in _SEED_LABELS:
        statement = f"CREATE INDEX FOR (e:{label}) ON (e.id)"
        try:
            await graph.write(statement)
        except Exception as e:  # noqa: BLE001 - index already exists / DDL quirk
            logger.debug("Seed id index for %s not created: %s", label, e)


async def _write_nodes(graph: GraphService, label: str, nodes: list[SeedNode]) -> None:
    """MERGE ontology nodes of one label, keyed on stable id."""
    if not nodes:
        return
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


async def _write_edges(graph: GraphService, edges: list[SeedEdge]) -> None:
    """MERGE ontology relationships, grouped by type, keyed on endpoint ids."""
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


__all__ = ["write_seed_graph"]
