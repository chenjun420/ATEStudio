"""KGEvolution — self-evolving FMEA knowledge graph via diagnosis feedback.

Automatically evolves the FalkorDB FMEA knowledge graph when new diagnosis
feedback arrives. Uses EmbeddingService for Mot-BERT-style synonym detection
(semantic nearest-neighbor) to avoid duplicate entities, creates new entities
when novel faults are discovered, and degrades stale edges over time.

Flow:
    1. Receive diagnosis feedback (fault_symptom + root_cause + error_code + product_type)
    2. Embed fault_symptom via EmbeddingService
    3. Check synonym: Qdrant nearest-neighbor search over previously indexed
       FaultSymptom vectors (COSINE). If the best score >= 0.85, SKIP
       (duplicate/synonym fault).
    4. If novel: CREATE new FaultSymptom/Cause/ErrorCode/Product nodes and
       HAS_CAUSE/TRIGGERS_ERROR_CODE/OCCURS_IN_PRODUCT relationships in
       FalkorDB (MERGE, same schema as KGSeeder); the symptom vector is
       upserted into the Qdrant symptom collection by FaultSymptomVectorStore.
    5. Degrade stale edges: reduce weight by 0.1 on HAS_CAUSE edges not
       accessed in 30 days (floor 0.1, never removed).

Vectors live ONLY in Qdrant (see fault_symptom_vector_store.py) — the graph
stores entities/relationships and never carries the float embedding array as
a node property. When Qdrant is unavailable, synonym detection degrades
gracefully (fault treated as novel, graph entities still written, vector
upsert skipped) and no exception bubbles to the caller.

All graph operations go through the backend-agnostic GraphService
protocol (CircuitBreaker-protected in the implementation).
No external Mot-BERT/transformers dependency — synonym detection uses
EmbeddingService exclusively.
"""

from __future__ import annotations

import logging
from typing import Any

from ate_cloud.services.embedding_service import EmbeddingService
from ate_cloud.services.fault_symptom_vector_store import (
    FaultSymptomVectorStore,
    cosine_similarity,
)
from ate_cloud.services.graph_service import GraphService

logger = logging.getLogger(__name__)

# Synonym detection threshold: Qdrant COSINE nearest-neighbor score >= this
# value means the incoming fault symptom is a duplicate/synonym.
_SYNONYM_THRESHOLD: float = 0.85

# Stale edge threshold in milliseconds (30 days). Edges whose last_accessed
# is older than this are candidates for weight degradation.
_STALE_THRESHOLD_MS: int = 30 * 24 * 60 * 60 * 1000

# Weight degradation step and floor.
_WEIGHT_DECREMENT: float = 0.1
_WEIGHT_FLOOR: float = 0.1
_WEIGHT_DEGRADABLE_MIN: float = 0.2  # Only degrade edges at or above this

# Cypher: create fault entities (MERGE, same schema as KGSeeder).
# Vectors are NOT stored on graph nodes — they live in Qdrant.
# Sets weight=1.0 and last_accessed on HAS_CAUSE for edge degradation.
_CREATE_CYPHER = """
MERGE (s:FaultSymptom {name: $fault_symptom})
  SET s.product_type = $product_type,
      s.last_accessed = timestamp()
MERGE (c:Cause {name: $root_cause})
  SET c.description = $root_cause
MERGE (err:ErrorCode {code: $error_code})
MERGE (prod:Product {name: $product_type})
  SET prod.type = $product_type
MERGE (s)-[r:HAS_CAUSE]->(c)
  ON CREATE SET r.weight = 1.0
  SET r.last_accessed = timestamp()
MERGE (s)-[:TRIGGERS_ERROR_CODE]->(err)
MERGE (s)-[:OCCURS_IN_PRODUCT]->(prod)
"""

# Cypher: degrade stale HAS_CAUSE edges.
# Only edges with weight >= 0.2 are degraded (floor 0.1 maintained).
# last_accessed older than 30 days → reduce weight by 0.1.
_DEGRADE_CYPHER = """
MATCH (s:FaultSymptom)-[r:HAS_CAUSE]->(c:Cause)
WHERE r.weight >= $min_weight
  AND r.last_accessed IS NOT NULL
  AND r.last_accessed < (timestamp() - $stale_threshold)
SET r.weight = r.weight - $decrement
RETURN count(r) AS degraded
"""


class KGEvolution:
    """Self-evolving FMEA knowledge graph via diagnosis feedback.

    Uses EmbeddingService for symptom embeddings, a GraphService backend for
    graph operations, and FaultSymptomVectorStore (Qdrant) for synonym
    nearest-neighbor search and symptom vector storage. Graph writes are
    protected by the CircuitBreaker integrated into the GraphService
    implementation; Qdrant failures degrade dedup but never interrupt graph
    evolution.

    Args:
        graph_service: The :class:`GraphService` for query execution.
        embedding_service: The :class:`EmbeddingService` for text embedding.
        qdrant_client: Qdrant client (or compatible mock). When ``None``,
            synonym detection degrades gracefully (every fault treated as
            novel) and graph evolution proceeds normally.
        symptom_collection: Qdrant collection for FaultSymptom vectors
            (defaults to ``ate_fault_symptoms``).
        embedding_dim: Vector dimensionality (defaults to
            ``settings.embedding_dimensions``).
    """

    def __init__(
        self,
        graph_service: GraphService,
        embedding_service: EmbeddingService,
        qdrant_client: Any | None = None,
        symptom_collection: str | None = None,
        embedding_dim: int | None = None,
    ) -> None:
        self._graph = graph_service
        self._embedding = embedding_service
        self._vectors = FaultSymptomVectorStore(
            qdrant_client,
            collection_name=symptom_collection,
            embedding_dim=embedding_dim,
        )

    async def process_feedback(self, feedback: dict[str, Any]) -> dict[str, Any]:
        """Process diagnosis feedback and evolve the knowledge graph.

        Embeds the fault symptom, checks for synonyms via Qdrant
        nearest-neighbor over existing FaultSymptom vectors, creates new
        graph entities if novel (and upserts the vector to Qdrant), and
        degrades stale edges.

        Args:
            feedback: Dict with keys ``fault_symptom``, ``root_cause``,
                ``error_code``, ``product_type``.

        Returns:
            Dict with ``action`` ("created" or "skipped"), ``nodes_created``,
            and ``edges_created``.

        Raises:
            CircuitBreakerOpenError: If the graph circuit is OPEN.
            Exception: Any graph or embedding error. Qdrant errors never
                propagate — dedup degrades and graph evolution continues.
        """
        fault_symptom: str = feedback["fault_symptom"]

        # Embed the fault symptom for synonym detection / vector storage.
        symptom_embedding = await self._embedding.embed(fault_symptom)

        # Check if a synonym already exists (Qdrant NN; False when Qdrant down).
        if await self._check_synonym(symptom_embedding):
            logger.info("Synonym detected for symptom '%s' — skipping creation", fault_symptom)
            await self._degrade_stale_edges()
            return {"action": "skipped", "nodes_created": 0, "edges_created": 0}

        # Novel fault — create graph entities.
        creation_result = await self._create_fault_entities(feedback)

        # Store the symptom vector in Qdrant for future synonym detection
        # (best-effort; failure is logged and never blocks graph evolution).
        self._vectors.index_symptom(feedback, symptom_embedding)

        # Degrade stale edges as part of graph maintenance.
        await self._degrade_stale_edges()

        logger.info(
            "Created entities for novel symptom '%s' — %d nodes, %d edges",
            fault_symptom,
            creation_result["nodes_created"],
            creation_result["edges_created"],
        )
        return {
            "action": "created",
            "nodes_created": creation_result["nodes_created"],
            "edges_created": creation_result["edges_created"],
        }

    async def _check_synonym(
        self,
        symptom_embedding: list[float],
        threshold: float = _SYNONYM_THRESHOLD,
    ) -> bool:
        """Check if a synonym exists via Qdrant nearest-neighbor (degrades to False).

        Args:
            symptom_embedding: Embedding vector of the incoming fault symptom.
            threshold: Cosine similarity threshold (default 0.85).

        Returns:
            True if a synonym exists (best NN score >= threshold), False
            otherwise — also False when Qdrant is unavailable.
        """
        return self._vectors.is_synonym(symptom_embedding, threshold)

    async def _create_fault_entities(self, feedback: dict[str, Any]) -> dict[str, int]:
        """Create fault entities in the FalkorDB knowledge graph.

        Uses MERGE (idempotent) to create FaultSymptom, Cause, ErrorCode,
        and Product nodes with HAS_CAUSE, TRIGGERS_ERROR_CODE, and
        OCCURS_IN_PRODUCT relationships. Follows the same schema as KGSeeder.
        Embedding vectors are NOT written to the graph — they live in Qdrant.

        Args:
            feedback: Dict with fault_symptom, root_cause, error_code,
                product_type.

        Returns:
            Dict with ``nodes_created`` and ``edges_created`` counts.
        """
        params: dict[str, Any] = {
            "fault_symptom": feedback["fault_symptom"],
            "root_cause": feedback["root_cause"],
            "error_code": feedback["error_code"],
            "product_type": feedback["product_type"],
        }
        await self._graph.write(_CREATE_CYPHER, params)

        # Nodes: FaultSymptom, Cause, ErrorCode, Product
        # Edges: HAS_CAUSE, TRIGGERS_ERROR_CODE, OCCURS_IN_PRODUCT
        return {"nodes_created": 4, "edges_created": 3}

    async def _degrade_stale_edges(self) -> int:
        """Degrade stale HAS_CAUSE edges by reducing their weight.

        Scans HAS_CAUSE relationships with weight >= 0.2 whose
        ``last_accessed`` is older than 30 days. Reduces weight by 0.1
        (floor 0.1 — edges at 0.1 are never degraded further or removed).

        Returns:
            Number of edges degraded.
        """
        params: dict[str, Any] = {
            "min_weight": _WEIGHT_DEGRADABLE_MIN,
            "stale_threshold": _STALE_THRESHOLD_MS,
            "decrement": _WEIGHT_DECREMENT,
        }
        results = await self._graph.write(_DEGRADE_CYPHER, params)
        if results:
            degraded: int = int(results[0].get("degraded", 0))
            if degraded > 0:
                logger.info("Degraded %d stale HAS_CAUSE edges", degraded)
            return degraded
        return 0

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Cosine similarity between two vectors (delegates to the shared helper)."""
        return cosine_similarity(a, b)


__all__ = ["KGEvolution"]
