"""KGSeeder — seeds the FalkorDB FMEA knowledge graph from the domain ontology.

Replaces the legacy ad-hoc Neo4j seed (per-record raw Cypher with free-text
instrument/component vocabularies and ``FaultSymptom``/``ErrorCode`` labels).
The seed is now ontology-aligned and carries no Cypher/Neo4j in this module:

* the canonical seed facts live in :mod:`ate_cloud.services.kg_seed_data`
  (104 electronics FMEA records across 6 categories — preserved verbatim);
* they are mapped onto the deterministic ontology ID space in
  :mod:`ate_cloud.services.kg_seed_facts` (ontology class labels, stable
  ``id`` keys, unified ``InstrumentKind`` / ``FaultKind`` / ``FaultCategory``
  vocab IDs — no free-text / duplicate vocabularies);
* :mod:`ate_cloud.services.kg_seed_writer` persists those ontology entities/
  relationships through the backend-agnostic
  :class:`~ate_cloud.services.graph_service.GraphService` (batched
  ``UNWIND ... MERGE`` on stable ids — the same idiom as the KG pipeline).

Re-running the seed is idempotent (MERGE keys on the stable entity id). The
app boots with no reachable graph: construction is cheap and a seed failure
surfaces to the caller (the ``/faults/seed`` route maps breaker errors to
503 and other graph errors to 502).
"""

from __future__ import annotations

import logging

from ate_cloud.services.graph_service import GraphService
from ate_cloud.services.kg_seed_data import FAULT_RECORDS, FaultRecord
from ate_cloud.services.kg_seed_facts import build_seed_graph
from ate_cloud.services.kg_seed_writer import write_seed_graph

logger = logging.getLogger(__name__)


class KGSeeder:
    """Seeds the FMEA knowledge graph from the deterministic ontology.

    Builds the ontology Instrument/Fault/Symptom/Cause/Solution/Component/
    Product nodes and the fault symptom→cause→solution / affects-component /
    product / diagnostic-instrument relationships from the seed facts, then
    persists them idempotently through the injected :class:`GraphService`.

    Args:
        graph_service: The :class:`GraphService` backend (FalkorDB in
            production; an in-memory fake in tests).
    """

    def __init__(self, graph_service: GraphService) -> None:
        self._graph = graph_service

    @property
    def records(self) -> list[FaultRecord]:
        """All raw seed fault records (the preserved 104 facts)."""
        return list(FAULT_RECORDS)

    @property
    def record_count(self) -> int:
        """Total number of seed fault records."""
        return len(FAULT_RECORDS)

    async def seed_all(self) -> dict[str, int]:
        """Seed the ontology FMEA graph (idempotent).

        Returns:
            Dict with ``nodes_created`` and ``relationships_created`` (total
            counts in the graph after seeding), plus ``facts_seeded``.

        Raises:
            CircuitBreakerOpenError: If the graph circuit is OPEN (caller → 503).
            Exception: Any graph write error (caller → 502).
        """
        nodes_by_label, edges = build_seed_graph()
        await write_seed_graph(self._graph, nodes_by_label, edges)

        logger.info(
            "Seeded ontology FMEA graph: %d facts, %d nodes, %d relationships",
            len(FAULT_RECORDS),
            sum(len(rows) for rows in nodes_by_label.values()),
            len(edges),
        )

        nodes = await self._graph.count_nodes()
        rels = await self._graph.count_relationships()
        return {
            "nodes_created": nodes,
            "relationships_created": rels,
            "facts_seeded": len(FAULT_RECORDS),
        }


__all__ = ["KGSeeder", "FaultRecord"]
