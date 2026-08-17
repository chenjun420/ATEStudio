"""KGEvolution — self-evolving FMEA knowledge graph via diagnosis feedback.

Automatically evolves the Neo4j FMEA knowledge graph when new diagnosis
feedback arrives. Uses EmbeddingService for Mot-BERT-style synonym detection
(semantic similarity) to avoid duplicate entities, creates new entities when
novel faults are discovered, and degrades stale edges over time.

Flow:
    1. Receive diagnosis feedback (fault_symptom + root_cause + error_code + product_type)
    2. Embed fault_symptom via EmbeddingService
    3. Check synonym: cosine similarity against ALL existing FaultSymptom embeddings
       in Neo4j. If max similarity >= 0.85, SKIP (no duplicate).
    4. If novel: CREATE new FaultSymptom/Cause/ErrorCode/Product nodes and
       HAS_CAUSE/TRIGGERS_ERROR_CODE/OCCURS_IN_PRODUCT relationships (MERGE,
       same schema as KGSeeder).
    5. Degrade stale edges: reduce weight by 0.1 on HAS_CAUSE edges not
       accessed in 30 days (floor 0.1, never removed).

All Neo4j operations go through Neo4jGraphService (CircuitBreaker-protected).
No external Mot-BERT/transformers dependency — synonym detection uses
EmbeddingService exclusively.
"""

from __future__ import annotations

import logging
import math
from typing import Any, cast

from ate_cloud.services.embedding_service import EmbeddingService
from ate_cloud.services.neo4j_graph_service import Neo4jGraphService

logger = logging.getLogger(__name__)

# Synonym detection threshold: embeddings with cosine similarity >= this
# value are considered the same fault symptom (synonym).
_SYNONYM_THRESHOLD: float = 0.85

# Stale edge threshold in milliseconds (30 days). Edges whose last_accessed
# is older than this are candidates for weight degradation.
_STALE_THRESHOLD_MS: int = 30 * 24 * 60 * 60 * 1000

# Weight degradation step and floor.
_WEIGHT_DECREMENT: float = 0.1
_WEIGHT_FLOOR: float = 0.1
_WEIGHT_DEGRADABLE_MIN: float = 0.2  # Only degrade edges at or above this

# Cypher: query all existing FaultSymptom embeddings for synonym detection.
_SYNONYM_QUERY_CYPHER = """
MATCH (s:FaultSymptom)
WHERE s.embedding IS NOT NULL
RETURN s.embedding AS embedding
"""

# Cypher: create fault entities (MERGE, same schema as KGSeeder).
# Stores embedding on FaultSymptom for future synonym detection.
# Sets weight=1.0 and last_accessed on HAS_CAUSE for edge degradation.
_CREATE_CYPHER = """
MERGE (s:FaultSymptom {name: $fault_symptom})
  SET s.embedding = $embedding,
      s.product_type = $product_type,
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

    Uses EmbeddingService for synonym detection (semantic similarity) and
    Neo4jGraphService for graph operations. All Neo4j writes are protected
    by the CircuitBreaker integrated into Neo4jGraphService.

    Args:
        graph_service: The :class:`Neo4jGraphService` for Cypher execution.
        embedding_service: The :class:`EmbeddingService` for text embedding.
    """

    def __init__(
        self,
        graph_service: Neo4jGraphService,
        embedding_service: EmbeddingService,
    ) -> None:
        self._graph = graph_service
        self._embedding = embedding_service

    async def process_feedback(self, feedback: dict[str, Any]) -> dict[str, Any]:
        """Process diagnosis feedback and evolve the knowledge graph.

        Embeds the fault symptom, checks for synonyms against existing
        FaultSymptom embeddings, creates new entities if novel, and
        degrades stale edges.

        Args:
            feedback: Dict with keys ``fault_symptom``, ``root_cause``,
                ``error_code``, ``product_type``.

        Returns:
            Dict with ``action`` ("created" or "skipped"), ``nodes_created``,
            and ``edges_created``.

        Raises:
            CircuitBreakerOpenError: If the Neo4j circuit is OPEN.
            Exception: Any Neo4j or embedding error.
        """
        fault_symptom: str = feedback["fault_symptom"]

        # Embed the fault symptom for synonym detection.
        symptom_embedding = await self._embedding.embed(fault_symptom)

        # Check if a synonym already exists in the graph.
        is_synonym = await self._check_synonym(symptom_embedding)
        if is_synonym:
            logger.info("Synonym detected for symptom '%s' — skipping creation", fault_symptom)
            await self._degrade_stale_edges()
            return {"action": "skipped", "nodes_created": 0, "edges_created": 0}

        # Novel fault — create entities. Pass embedding via feedback dict.
        feedback_with_embedding = {**feedback, "_embedding": symptom_embedding}
        creation_result = await self._create_fault_entities(feedback_with_embedding)

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
        """Check if a synonym already exists via cosine similarity.

        Queries all existing FaultSymptom embeddings from Neo4j and computes
        cosine similarity against the incoming embedding. If the maximum
        similarity is >= threshold, a synonym is found.

        Args:
            symptom_embedding: Embedding vector of the incoming fault symptom.
            threshold: Cosine similarity threshold (default 0.85).

        Returns:
            True if a synonym exists (max similarity >= threshold),
            False otherwise.
        """
        results = await self._graph.query(_SYNONYM_QUERY_CYPHER)
        if not results:
            return False

        max_similarity = 0.0
        for row in results:
            existing: Any = row.get("embedding")
            if existing is None:
                continue
            existing_vec: list[float] = [float(v) for v in existing]
            similarity = self._cosine_similarity(symptom_embedding, existing_vec)
            if similarity > max_similarity:
                max_similarity = similarity

        logger.debug(
            "Synonym check: max similarity = %.4f (threshold = %.2f)",
            max_similarity,
            threshold,
        )
        return max_similarity >= threshold

    async def _create_fault_entities(self, feedback: dict[str, Any]) -> dict[str, int]:
        """Create fault entities in the Neo4j knowledge graph.

        Uses MERGE (idempotent) to create FaultSymptom, Cause, ErrorCode,
        and Product nodes with HAS_CAUSE, TRIGGERS_ERROR_CODE, and
        OCCURS_IN_PRODUCT relationships. Follows the same schema as KGSeeder.

        The embedding is read from ``feedback["_embedding"]`` (set by
        :meth:`process_feedback`) or computed on-the-fly if missing.

        Args:
            feedback: Dict with fault_symptom, root_cause, error_code,
                product_type, and optionally ``_embedding``.

        Returns:
            Dict with ``nodes_created`` and ``edges_created`` counts.
        """
        embedding_raw: Any = feedback.get("_embedding")
        if embedding_raw is None:
            embedding = await self._embedding.embed(feedback["fault_symptom"])
        else:
            embedding = cast(list[float], embedding_raw)

        params: dict[str, Any] = {
            "fault_symptom": feedback["fault_symptom"],
            "root_cause": feedback["root_cause"],
            "error_code": feedback["error_code"],
            "product_type": feedback["product_type"],
            "embedding": embedding,
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
        """Compute cosine similarity between two vectors.

        ``cos(A, B) = dot(A, B) / (|A| * |B|)``

        Args:
            a: First vector.
            b: Second vector.

        Returns:
            Cosine similarity in [-1, 1]. Returns 0.0 if either vector
            has zero magnitude.
        """
        if not a or not b:
            return 0.0
        # Only compare up to the shorter vector's length (defensive).
        min_len = min(len(a), len(b))
        dot = sum(a[i] * b[i] for i in range(min_len))
        norm_a = math.sqrt(sum(x * x for x in a[:min_len]))
        norm_b = math.sqrt(sum(y * y for y in b[:min_len]))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)


__all__ = ["KGEvolution"]
