"""FaultSymptomVectorStore — Qdrant-backed store for FaultSymptom vectors.

Holds the embedding vectors used by KGEvolution synonym/dedup detection.
Vectors live ONLY here (Qdrant) — FalkorDB stores entities/relationships
and never carries the float array as a node property.

The collection (``ate_fault_symptoms`` by default) is separate from the
failure-index collection (``ate_failures``) used by RAG retrieval; both use
COSINE distance and dimensions from ``settings.embedding_dimensions``.

Every method degrades gracefully: a Qdrant failure (missing client,
connection error) is logged and surfaced as a benign result
(``False`` / no-op) so graph evolution in the caller is never blocked.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any

from ate_cloud.config import settings

logger = logging.getLogger(__name__)

# Default Qdrant collection for FaultSymptom synonym vectors.
DEFAULT_SYMPTOM_COLLECTION: str = "ate_fault_symptoms"


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity ``dot(a,b) / (|a| * |b|)`` in [-1, 1]; 0.0 if degenerate."""
    if not a or not b:
        return 0.0
    min_len = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(min_len))
    norm_a = math.sqrt(sum(x * x for x in a[:min_len]))
    norm_b = math.sqrt(sum(y * y for y in b[:min_len]))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class FaultSymptomVectorStore:
    """Qdrant nearest-neighbor store for fault symptom embeddings.

    Args:
        qdrant_client: Qdrant client (or compatible fake). ``None`` means
            Qdrant is unavailable — all operations degrade benignly.
        collection_name: Collection name (defaults to
            ``ate_fault_symptoms``).
        embedding_dim: Vector dimensionality (defaults to
            ``settings.embedding_dimensions``).
    """

    def __init__(
        self,
        qdrant_client: Any | None,
        collection_name: str | None = None,
        embedding_dim: int | None = None,
    ) -> None:
        self._qdrant = qdrant_client
        self._collection = collection_name or DEFAULT_SYMPTOM_COLLECTION
        self._embedding_dim = embedding_dim or settings.embedding_dimensions
        self._collection_ready = False

    @property
    def available(self) -> bool:
        """True when a Qdrant client is wired in."""
        return self._qdrant is not None

    def is_synonym(
        self,
        symptom_embedding: list[float],
        threshold: float,
    ) -> bool:
        """Return True if a stored symptom vector scores >= ``threshold`` (COSINE).

        Qdrant NN search with ``limit=1``. Any failure (or no client) returns
        False — the caller treats the fault as novel and graph evolution
        proceeds.
        """
        if self._qdrant is None:
            logger.info(
                "Qdrant client unavailable; synonym detection degraded "
                "(fault treated as novel)"
            )
            return False
        try:
            self._ensure_collection()
            results = self._qdrant.search(
                collection_name=self._collection,
                query_vector=symptom_embedding,
                limit=1,
                with_payload=False,
            )
            if not results:
                return False
            best_score = float(getattr(results[0], "score", 0.0))
            logger.debug(
                "Synonym check: best NN score = %.4f (threshold = %.2f)",
                best_score,
                threshold,
            )
            return best_score >= threshold
        except Exception as e:
            logger.warning(
                "Qdrant synonym search failed (%s); dedup degraded, "
                "treating fault as novel",
                e,
            )
            return False

    def index_symptom(
        self,
        feedback: dict[str, Any],
        symptom_embedding: list[float],
    ) -> None:
        """Upsert a novel symptom vector (best-effort; never raises).

        The point id is a deterministic UUIDv5 derived from the symptom
        text, so repeated feedback for the same symptom upserts instead of
        duplicating.
        """
        if self._qdrant is None:
            return
        try:
            from qdrant_client.http import models as qmodels

            self._ensure_collection()
            point_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"fault-symptom:{str(feedback['fault_symptom']).strip().lower()}",
                )
            )
            self._qdrant.upsert(
                collection_name=self._collection,
                points=[
                    qmodels.PointStruct(
                        id=point_id,
                        vector=symptom_embedding,
                        payload={
                            "fault_symptom": feedback["fault_symptom"],
                            "root_cause": feedback["root_cause"],
                            "error_code": feedback["error_code"],
                            "product_type": feedback["product_type"],
                        },
                    ),
                ],
            )
            logger.debug(
                "Indexed fault symptom vector in Qdrant '%s': %s",
                self._collection,
                feedback["fault_symptom"],
            )
        except Exception as e:
            logger.warning(
                "Failed to index fault symptom vector in Qdrant (%s); "
                "graph entities already written, dedup may miss this symptom",
                e,
            )

    def _ensure_collection(self) -> None:
        """Create the symptom collection if missing (COSINE, once per process).

        Mirrors FailureIndexer.ensure_collection against the sync Qdrant
        client. Exceptions propagate to the callers, which degrade.
        """
        if self._qdrant is None or self._collection_ready:
            return
        from qdrant_client.http import models as qmodels

        collections = self._qdrant.get_collections()
        collection_names = {c.name for c in collections.collections}
        if self._collection not in collection_names:
            self._qdrant.create_collection(
                collection_name=self._collection,
                vectors_config=qmodels.VectorParams(
                    size=self._embedding_dim,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            logger.info(
                "Created Qdrant collection '%s' (dim=%d, distance=COSINE)",
                self._collection,
                self._embedding_dim,
            )
        self._collection_ready = True


__all__ = ["DEFAULT_SYMPTOM_COLLECTION", "FaultSymptomVectorStore", "cosine_similarity"]
