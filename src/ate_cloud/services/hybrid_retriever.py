"""Hybrid Retriever — Qdrant vector + ontology-KG graph fusion (RRF).

Combines two complementary retrieval strategies for electronics test fault
diagnosis:

1. **Qdrant semantic similarity** — finds past failures with similar embedding
   vectors (text-level semantic match).
2. **Ontology knowledge-graph reasoning** — traverses the task-8 seed /
   task-12 extraction KG via :mod:`ate_cloud.services.kg_retrieval` to return
   the Fault -> Symptom -> Cause -> Solution chain, affected component,
   product and diagnostic instrument (structural/causal match).

The two branches are joined on **stable ontology entity ids**, NOT on text
prefixes: a vector hit carrying an ``error_code`` (or ``entity_id``) is
normalized to the same ``fault:<slug(error_code)>`` id the KG seed MERGEd, so
a semantic hit and a graph hit describing the same fault fuse. See
:mod:`ate_cloud.services.hybrid_fusion`.

Results are fused with **Reciprocal Rank Fusion (RRF)**, ``k=60``, then an
optional semantic re-ranking step re-sorts by similarity to the query.

**Golden-Retriever query rewriting** (:mod:`ate_cloud.services.query_rewrite`)
disambiguates domain jargon (I2C, SPI, BGA, ESD, ...) before retrieval; the
LLM augmentation is CircuitBreaker-protected and falls back to the
deterministic dictionary expansion.

Per AGENTS.md section 7: Qdrant and the graph backend are protected by
CircuitBreakers. If either is configured but unreachable, its breaker opens
and ``CircuitBreakerOpenError`` propagates from that branch — ``search()``
catches it per branch and returns results from the surviving branch (a
single-source result is still a valid answer; an empty list signals total
failure).
"""

from __future__ import annotations

import logging
from typing import Any

from ate_cloud.config import settings
from ate_cloud.services.embedding_service import EmbeddingService
from ate_cloud.services.graph_service import GraphService
from ate_cloud.services.hybrid_fusion import reciprocal_rank_fusion
from ate_cloud.services.hybrid_fusion import rerank as rerank_fuse
from ate_cloud.services.kg_retrieval import extract_keyword, fault_entity_id, retrieve_faults
from ate_cloud.services.query_rewrite import QueryRewriter
from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)


class HybridRetriever:
    """Hybrid retrieval: Qdrant semantic search fused with ontology-KG reasoning.

    Pipeline: query rewrite (dictionary + LLM) -> embed -> Qdrant semantic
    search -> ontology-KG traversal seeded from the request error code and
    the vector hits' entity ids -> Reciprocal Rank Fusion (k=60) -> optional
    semantic re-ranking.

    All external calls (Qdrant, the graph backend via GraphService, the
    OpenAI LLM) are CircuitBreaker-protected (failure_threshold=5,
    timeout=30s). EmbeddingService and the GraphService implementation carry
    their own internal breakers; this class adds breakers around direct
    Qdrant calls and the LLM rewrite call.

    Args:
        embedding_service: EmbeddingService for query/result vectors.
        graph_service: GraphService backend for ontology Cypher/GQL queries.
        qdrant_client: Qdrant client instance (or compatible mock).
        collection_name: Qdrant collection name (defaults to settings).
        api_key: OpenAI API key for LLM query rewriting (defaults to settings).
        model: Chat model name for query rewriting.
        embedding_dim: Expected embedding vector dimensionality.
    """

    def __init__(
        self,
        embedding_service: EmbeddingService,
        graph_service: GraphService,
        qdrant_client: Any,
        collection_name: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        embedding_dim: int | None = None,
    ) -> None:
        self._embedding_service = embedding_service
        self._graph_service = graph_service
        self._qdrant_client = qdrant_client
        self._collection_name = collection_name or settings.qdrant_collection_failures
        self._embedding_dim = embedding_dim or settings.embedding_dimensions

        self._qdrant_breaker = CircuitBreaker(
            failure_threshold=5, timeout=30.0, name="qdrant-hybrid-retriever"
        )
        self._llm_breaker = CircuitBreaker(
            failure_threshold=5, timeout=30.0, name="llm-query-rewriter"
        )
        self._rewriter = QueryRewriter(
            api_key=api_key or settings.openai_api_key,
            model=model or settings.openai_model,
            breaker=self._llm_breaker,
        )

    @property
    def qdrant_circuit_breaker(self) -> CircuitBreaker:
        """CircuitBreaker protecting direct Qdrant calls."""
        return self._qdrant_breaker

    @property
    def llm_circuit_breaker(self) -> CircuitBreaker:
        """CircuitBreaker protecting LLM query-rewriting calls."""
        return self._llm_breaker

    # ── Public API ──────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        top_k: int = 10,
        *,
        rerank: bool = True,
        error_code: str = "",
    ) -> list[dict[str, Any]]:
        """Hybrid search fusing Qdrant + the ontology KG with RRF.

        Pipeline:
        1. Rewrite query (dictionary expansion + optional LLM).
        2. Embed the rewritten query and run Qdrant semantic search.
        3. Traverse the ontology KG, seeded from ``error_code`` and from the
           stable entity ids carried by the vector hits.
        4. Reciprocal Rank Fusion (k=60) on shared entity ids.
        5. Optional semantic re-ranking against the original query.

        Args:
            query: Natural-language fault description or error text.
            top_k: Maximum number of results to return.
            rerank: If True, re-rank fused results by semantic similarity
                to ``query``.
            error_code: Structured error code (e.g. ``"ERR_I2C_TIMEOUT"``)
                when the caller has one — resolved to the seed Fault id.

        Returns:
            Result dicts, each with ``rrf_score`` and ``source`` (``"qdrant"``,
            ``"graph"``, or ``"fused"``) plus payload/relationship fields.
            A branch failure degrades to the surviving branch.
        """
        rewritten = await self._rewriter.rewrite(query)
        query_vector = await self._embedding_service.embed(rewritten)

        vector_results = await self._safe_vector_search(query_vector, top_k)

        candidate_ids = self._graph_candidate_ids(vector_results, error_code)
        graph_results = await self._safe_graph_search(candidate_ids, query, top_k)

        fused = reciprocal_rank_fusion(vector_results, graph_results)
        if rerank and fused:
            fused = await rerank_fuse(self._embedding_service, fused, query)
        return fused[:top_k]

    # ── Qdrant semantic branch ──────────────────────────────────────

    async def _safe_vector_search(
        self, query_vector: list[float], top_k: int
    ) -> list[dict[str, Any]]:
        """Qdrant search; on breaker/error log and return [] (degrade)."""
        try:
            return await self._search_qdrant(query_vector, top_k)
        except CircuitBreakerOpenError:
            logger.warning("Qdrant circuit breaker open; vector branch unavailable")
            return []
        except Exception as e:  # noqa: BLE001 — one branch must not kill retrieval
            logger.warning("Qdrant retrieval failed: %s", e)
            return []

    async def _search_qdrant(
        self, query_vector: list[float], top_k: int
    ) -> list[dict[str, Any]]:
        """Search Qdrant for semantically similar fault cases (breaker-protected).

        Raises:
            CircuitBreakerOpenError: If the Qdrant circuit is OPEN.
        """
        async def _do_search() -> list[dict[str, Any]]:
            hits = self._qdrant_client.search(
                collection_name=self._collection_name,
                query_vector=query_vector,
                limit=top_k,
                with_payload=True,
            )
            return [self._vector_hit_to_result(h) for h in hits]

        return await self._qdrant_breaker.call(_do_search)

    @staticmethod
    def _vector_hit_to_result(hit: Any) -> dict[str, Any]:
        """Map a Qdrant hit to a result dict, normalizing its ontology id.

        The shared-ID join: if the payload carries an explicit ``entity_id``
        use it; otherwise an ``error_code`` resolves to the seed Fault id
        (``fault:<slug(code)>``). With neither, the hit stays a point-id-only
        entry that cannot falsely fuse with graph results.
        """
        payload: dict[str, Any] = dict(hit.payload or {})
        result: dict[str, Any] = {
            "id": str(hit.id),
            "score": float(hit.score),
            "source": "qdrant",
            **payload,
        }
        entity_id = payload.get("entity_id")
        if not entity_id and payload.get("error_code"):
            entity_id = fault_entity_id(str(payload["error_code"]))
        if entity_id:
            result["entity_id"] = entity_id
        return result

    # ── Ontology-KG graph branch ────────────────────────────────────

    @staticmethod
    def _graph_candidate_ids(
        vector_results: list[dict[str, Any]], error_code: str
    ) -> list[str]:
        """Collect stable Fault ids to seed graph traversal.

        From the structured error code (if any) and the entity ids harvested
        from vector hits (de-duplicated, order preserved).
        """
        ids: list[str] = []
        if error_code.strip():
            ids.append(fault_entity_id(error_code.strip()))
        for hit in vector_results:
            entity_id = hit.get("entity_id")
            if entity_id and entity_id not in ids:
                ids.append(str(entity_id))
        return ids

    async def _safe_graph_search(
        self, candidate_ids: list[str], query: str, top_k: int
    ) -> list[dict[str, Any]]:
        """Ontology-KG traversal; on breaker/error log and return [] (degrade)."""
        try:
            return await retrieve_faults(
                self._graph_service,
                candidate_ids=candidate_ids,
                keyword=extract_keyword(query),
                limit=top_k,
            )
        except CircuitBreakerOpenError:
            logger.warning("Graph circuit breaker open; graph branch unavailable")
            return []
        except Exception as e:  # noqa: BLE001 — one branch must not kill retrieval
            logger.warning("Graph retrieval failed: %s", e)
            return []


__all__ = ["HybridRetriever", "CircuitBreakerOpenError"]
