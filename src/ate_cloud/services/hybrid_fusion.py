"""RRF fusion + semantic re-ranking for hybrid diagnosis retrieval.

Pure, backend-agnostic helpers extracted from ``hybrid_retriever``:

* :func:`fusion_key` — the shared-stable-id join. A vector hit and a graph
  hit describe the SAME fault when they carry the same ontology entity id
  (``fault:<slug(error_code)>``). There is no text-prefix heuristic: vector
  payloads are normalized to their ``entity_id`` (or an error-code-derived
  fault id) and graph hits already carry their Fault node id.
* :func:`reciprocal_rank_fusion` — merges the two ranked lists with
  ``score(d) = sum(1 / (k + rank_i(d)))``, ``k=60`` (Cormack et al. 2009).
* :func:`rerank` — optional semantic re-sort by cosine similarity between
  the query embedding and each fused result's text embedding.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from ate_platform.common.circuit_breaker import CircuitBreakerOpenError

logger = logging.getLogger(__name__)

#: RRF constant — standard value from the original paper (Cormack et al. 2009).
RRF_K: int = 60

#: Graph/vector relationship fields merged into a fused result.
_GRAPH_FIELDS: tuple[str, ...] = (
    "symptom", "cause", "solution", "component", "product", "instrument",
)


def fusion_key(result: dict[str, Any]) -> str:
    """Stable join key for a result dict.

    Prefers the ontology ``entity_id`` (a vector hit carrying ``entity_id``
    or ``error_code`` resolves to the same ``fault:<...>`` id as the graph
    hit). Falls back to a point-id-namespaced key for vector hits that have
    no ontology entity (free failure cases never indexed into the KG), so
    they never falsely fuse with anything.
    """
    entity_id = result.get("entity_id")
    if entity_id:
        return f"entity:{entity_id}"
    return f"point:{result.get('id', id(result))}"


def reciprocal_rank_fusion(
    vector_results: list[dict[str, Any]],
    graph_results: list[dict[str, Any]],
    k: int = RRF_K,
) -> list[dict[str, Any]]:
    """Fuse two ranked lists with Reciprocal Rank Fusion (k=60).

    Documents are deduplicated by :func:`fusion_key` (stable entity id). A
    vector hit and graph hit with the same key merge — the fused entry keeps
    fields from both sources and its ``source`` becomes ``"fused"``.

    Args:
        vector_results: Ranked Qdrant hits (best first), each with a
            ``score`` (cosine similarity) and payload fields.
        graph_results: Ranked ontology-graph Fault records (best first),
            ``score=0.0`` (rank comes from traversal order).
        k: RRF constant (default 60).

    Returns:
        Fused list sorted by ``rrf_score`` descending. Each dict gains
        ``rrf_score``; ``score`` is replaced by the RRF score.
    """
    merged: dict[str, dict[str, Any]] = {}

    # Vector branch first (rank starts at 1).
    for rank, result in enumerate(vector_results, start=1):
        key = fusion_key(result)
        rrf_score = 1.0 / (k + rank)
        entry = dict(result)
        entry["rrf_score"] = rrf_score
        entry["vector_score"] = result.get("score")
        entry["vector_rank"] = rank
        entry.pop("score", None)
        merged[key] = entry

    # Graph branch: fuse on matching entity id, else append.
    for rank, result in enumerate(graph_results, start=1):
        key = fusion_key(result)
        rrf_score = 1.0 / (k + rank)
        if key in merged:
            existing = merged[key]
            existing["rrf_score"] = existing.get("rrf_score", 0.0) + rrf_score
            existing["graph_rank"] = rank
            for field in _GRAPH_FIELDS:
                if result.get(field) and not existing.get(field):
                    existing[field] = result[field]
            # A fused entry has evidence from both branches.
            existing["source"] = "fused"
        else:
            entry = dict(result)
            entry["rrf_score"] = rrf_score
            entry["graph_rank"] = rank
            entry.pop("score", None)
            merged[key] = entry

    return sorted(merged.values(), key=lambda x: x.get("rrf_score", 0.0), reverse=True)


def result_text(result: dict[str, Any]) -> str:
    """Build a text representation of a result for embedding/re-ranking."""
    parts: list[str] = []
    for field in ("symptom", "cause", "solution", "error_message",
                  "fault_symptom", "root_cause", "failed_step_name"):
        val = result.get(field)
        if val and isinstance(val, str) and val.strip():
            parts.append(val.strip())
    return " ".join(parts) if parts else str(result.get("id", ""))


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Cosine similarity in [-1, 1]; 0.0 if either vector has zero magnitude."""
    if not vec_a or not vec_b:
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=True))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


async def rerank(
    embedding_service: Any,
    results: list[dict[str, Any]],
    query: str,
) -> list[dict[str, Any]]:
    """Re-rank fused results by semantic similarity to the original query.

    Computes cosine similarity between the query embedding and each result's
    text embedding, then re-sorts by similarity (rrf_score as tiebreaker).
    If embeddings are unavailable (breaker open / error) results are returned
    unchanged with a warning — re-ranking is best-effort.
    """
    if not results:
        return results

    texts = [result_text(r) for r in results]
    try:
        query_vec = await embedding_service.embed(query)
        result_vecs = await embedding_service.embed_batch(texts)
    except CircuitBreakerOpenError:
        logger.warning("Embedding service breaker open; skipping re-ranking")
        return results
    except Exception as e:  # noqa: BLE001 — re-rank must never break retrieval
        logger.warning("Re-ranking embedding failed: %s; skipping re-ranking", e)
        return results

    for result, vec in zip(results, result_vecs, strict=True):
        result["rerank_score"] = cosine_similarity(query_vec, vec)

    return sorted(
        results,
        key=lambda x: (x.get("rerank_score", 0.0), x.get("rrf_score", 0.0)),
        reverse=True,
    )


__all__ = [
    "RRF_K",
    "cosine_similarity",
    "fusion_key",
    "reciprocal_rank_fusion",
    "rerank",
    "result_text",
]
