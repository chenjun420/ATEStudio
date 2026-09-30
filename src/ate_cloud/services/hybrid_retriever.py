"""Retriever — Qdrant vector search over the fault-case index (RRF).

One retrieval strategy: **Qdrant semantic similarity** finds past failures with
similar embedding vectors (text-level semantic match).

This module used to combine two:

1. Qdrant semantic similarity.
2. Ontology knowledge-graph reasoning, traversing the task-8 seed / task-12
   extraction KG via ``ate_cloud.services.kg_retrieval`` for the
   Fault -> Symptom -> Cause -> Solution chain (structural/causal match).

The two branches were joined on **stable ontology entity ids** — a vector hit
carrying an ``error_code`` was normalized to the same ``fault:<slug(error_code)>``
id the KG seed MERGEd, so a semantic hit and a graph hit describing the same
fault would fuse (see :mod:`ate_cloud.services.hybrid_fusion`).

Branch 2 is removed, and it was never live: the graph backend it needed was
never provisioned, so every deployment logged ``Graph retrieval failed: Error
111 connecting to localhost:6379`` and fused an empty list. The field evidence
for not building a graph in the first place is recorded in the removal commit
and in the design spec §6.1; the short version is that a RAG baseline measured
against an FMEA *knowledge graph* as its data source scored F1@20 = 0.267,
with the graph method's 0.523 reached only on a single line with three
scenarios, and the gains coming from algorithms rather than from storage.

Results still pass through **Reciprocal Rank Fusion**, ``k=60``, then an
optional semantic re-ranking step re-sorts by similarity to the query. With one
input list RRF preserves ordering; it is kept because it is the seam where a
second retrieval source would join, and because ``hybrid_fusion`` is a pure
function that knows nothing about graphs.

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
from ate_cloud.services.hybrid_fusion import reciprocal_rank_fusion
from ate_cloud.services.hybrid_fusion import rerank as rerank_fuse
from ate_cloud.services.qdrant_query import qdrant_points, query_points
from ate_cloud.services.query_rewrite import QueryRewriter
from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)


class HybridRetriever:
    """Retrieval: Qdrant semantic search with query rewriting and re-ranking.

    Pipeline: query rewrite (dictionary + LLM) -> embed -> Qdrant semantic
    search -> Reciprocal Rank Fusion -> optional semantic re-ranking.

    **The name is now historical.** It used to fuse Qdrant with ontology-KG
    reasoning, and the RRF fusion is still in the pipeline because it is a pure
    function over two ranked lists — but only one list exists now.

    That is not a claim that fusion is pointless in general; it is a statement
    that one input was always empty on every deployment, because the graph
    backend was never provisioned. The DENSO/JST field study behind that
    decision found the RAG baseline scored F1@20 = 0.267 *while already using an
    FMEA knowledge graph as its data source*, and the full graph method reached
    0.523 only on a single line with three scenarios, with the gains coming from
    the algorithms (domain conceptualisation, process-aware scoring) rather than
    from the storage. Keeping a fusion step that merges one real list with one
    empty one would only obscure that the retrieval is vector retrieval.

    External calls (Qdrant, the OpenAI LLM for query rewriting) are
    CircuitBreaker-protected (failure_threshold=5, timeout=30s).

    Args:
        embedding_service: EmbeddingService for query/result vectors.
        qdrant_client: Qdrant client instance (or compatible mock).
        collection_name: Qdrant collection name (defaults to settings).
        api_key: OpenAI API key for LLM query rewriting (defaults to settings).
        model: Chat model name for query rewriting.
        embedding_dim: Expected embedding vector dimensionality.
    """

    def __init__(
        self,
        embedding_service: EmbeddingService,
        qdrant_client: Any,
        collection_name: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        embedding_dim: int | None = None,
    ) -> None:
        self._embedding_service = embedding_service
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
        """Vector search over the fault-case index, with RRF and re-ranking.

        Pipeline:
        1. Rewrite query (dictionary expansion + optional LLM).
        2. Embed the rewritten query and run Qdrant semantic search.
        3. Reciprocal Rank Fusion (k=60) — with a single ranked list this is
           the identity on ordering, kept because it is the seam where a second
           retrieval source would join, and because ``hybrid_fusion`` is a pure
           function with no knowledge of the graph at all.
        4. Optional semantic re-ranking against the original query.

        Args:
            query: Natural-language fault description or error text.
            top_k: Maximum number of results to return.
            rerank: If True, re-rank fused results by semantic similarity
                to ``query``.
            error_code: Structured error code, kept for call compatibility.
                It previously seeded the ontology-KG traversal; with that gone
                it contributes nothing, and it is **not** used to filter or
                re-weight the vector results — doing so would silently drop
                cases that share a symptom but not an error code.

        Returns:
            Result dicts with ``rrf_score`` and ``source`` plus the indexed
            payload. A Qdrant failure degrades to an empty list.
        """
        rewritten = await self._rewriter.rewrite(query)
        query_vector = await self._embedding_service.embed(rewritten)

        vector_results = await self._safe_vector_search(query_vector, top_k)

        # Fused against an empty second list on purpose rather than skipping the
        # call: `reciprocal_rank_fusion` is where a second retrieval source
        # belongs, and having it pass through untouched keeps that visible.
        fused = reciprocal_rank_fusion(vector_results, [])
        if rerank and fused:
            fused = await rerank_fuse(self._embedding_service, fused, query)
        return fused[:top_k]

    # ── Qdrant branch ────────────────────────────────────────────────

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
            hits = qdrant_points(
                query_points(
                    self._qdrant_client,
                    collection_name=self._collection_name,
                    query=query_vector,
                    limit=top_k,
                    with_payload=True,
                )
            )
            return [self._vector_hit_to_result(h) for h in hits]

        return await self._qdrant_breaker.call(_do_search)

    @staticmethod
    def _vector_hit_to_result(hit: Any) -> dict[str, Any]:
        """Map a Qdrant hit to a result dict.

        Previously this also synthesized an ``entity_id`` from the payload's
        ``error_code`` (mapping to a seeded ontology Fault node) so that vector
        hits and graph hits could fuse on a shared key. With the graph gone there
        is nothing to fuse with, and the synthesized id pointed at nodes that no
        longer exist — so it is dropped. A payload-provided ``entity_id`` still
        arrives via ``**payload``.
        """
        payload: dict[str, Any] = dict(hit.payload or {})
        return {
            "id": str(hit.id),
            "score": float(hit.score),
            "source": "qdrant",
            **payload,
        }

    # ── Ontology-KG graph branch ────────────────────────────────────
    #
    # Removed with the knowledge-graph subsystem. `_graph_candidate_ids` seeded
    # traversal from the request error code and the entity ids harvested from
    # vector hits; `_safe_graph_search` called `retrieve_faults` and degraded to
    # [] on any failure. On every deployment that ran, the branch logged
    # "Graph retrieval failed: Error 111 connecting to localhost:6379" and
    # contributed nothing.

__all__ = ["HybridRetriever", "CircuitBreakerOpenError"]
