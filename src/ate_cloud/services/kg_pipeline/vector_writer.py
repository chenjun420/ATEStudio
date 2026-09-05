"""Vector persistence stage — entity embeddings to Qdrant (best-effort).

Mirrors FailureIndexer / FaultSymptomVectorStore: a SYNC Qdrant client, a
COSINE collection sized from config, and deterministic uuid5 point ids so
re-ingestion upserts instead of duplicating. Vectors are never graph node
properties. Any Qdrant failure degrades benignly (logged, returns 0) and never
blocks graph writes — the graph remains the source of truth.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class VectorWriter:
    """Upserts KG entity embeddings into Qdrant; degrades when unavailable."""

    def __init__(
        self,
        qdrant_client: Any | None,
        embedding_service: Any | None,
        collection: str,
        embedding_dim: int,
    ) -> None:
        self._qdrant = qdrant_client
        self._embedding = embedding_service
        self._collection = collection
        self._dim = embedding_dim
        self._ready = False

    async def write_entities(
        self,
        doc_id: str,
        source: str,
        metadata: dict[str, Any],
        entities: list[dict[str, Any]],
    ) -> int:
        """Embed each entity and upsert a point. Returns points written.

        Returns 0 when Qdrant/embedding are unavailable or a call fails —
        the caller treats vectors as degraded, not fatal.
        """
        qdrant = self._qdrant
        if qdrant is None or self._embedding is None:
            logger.info("Qdrant/embedding unavailable; vector persistence degraded")
            return 0
        try:
            from qdrant_client.http import models as qmodels

            self._ensure_collection(qdrant, qmodels)
            written = 0
            for ent in entities:
                name = str(ent.get("name") or ent.get("text") or "").strip()
                if not name:
                    continue
                vector = await self._embedding.embed(name)
                point_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"kg-entity:{doc_id}:{name.lower()}")
                )
                qdrant.upsert(
                    collection_name=self._collection,
                    points=[
                        qmodels.PointStruct(
                            id=point_id,
                            vector=vector,
                            payload={
                                "name": name,
                                "type": ent.get("type") or ent.get("label"),
                                "doc_id": doc_id,
                                "source": source,
                                **metadata,
                            },
                        )
                    ],
                )
                written += 1
            return written
        except Exception as e:  # noqa: BLE001 — vectors must never block graph writes
            logger.warning("Vector persistence failed (%s); graph already written", e)
            return 0

    def _ensure_collection(self, qdrant: Any, qmodels: Any) -> None:
        """Create the KG entity collection once (COSINE, configured dim)."""
        if self._ready:
            return
        collections = qdrant.get_collections()
        names = {c.name for c in collections.collections}
        if self._collection not in names:
            qdrant.create_collection(
                collection_name=self._collection,
                vectors_config=qmodels.VectorParams(
                    size=self._dim,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            logger.info(
                "Created Qdrant collection '%s' (dim=%d, COSINE)", self._collection, self._dim
            )
        self._ready = True


__all__ = ["VectorWriter"]
