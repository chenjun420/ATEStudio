"""KGPipeline facade — the single Semantica GraphRAG entry point.

Stages: Sources → ingest → domain parse/normalize → extract (pattern when no
LLM key, LLM-backed when a key is present) → Semantica GraphBuilder
conflict/dedup (``merge_entities=True``) → ontology-ready KG → persist to the
injected GraphService (FalkorDB LPG, via Cypher MERGE) AND Qdrant vectors.

Graph writes go through the existing :class:`GraphService`
(:class:`~ate_cloud.services.falkordb_graph_service.FalkorDBGraphService` in
production) — the pipeline never opens a second FalkorDB connection. Vectors
go through the injected Qdrant client. Both are injectable fakes in tests.

Semantica import/construction failure raises :class:`KGPipelineUnavailable`
(callers map to 503); the application still boots and non-graph endpoints are
unaffected. Qdrant failure degrades vector persistence but never blocks graph
writes.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ate_cloud.services.kg_pipeline import _semantica
from ate_cloud.services.kg_pipeline.errors import KGPipelineUnavailable
from ate_cloud.services.kg_pipeline.models import (
    Document,
    LLMExtractor,
    PipelineConfig,
    PipelineResult,
)
from ate_cloud.services.kg_pipeline.patterns import domain_extract
from ate_cloud.services.kg_pipeline.vector_writer import VectorWriter

logger = logging.getLogger(__name__)

_LABEL_RE = re.compile(r"[^A-Za-z0-9_]")


def _safe_label(value: str) -> str:
    """Coerce an entity/relation type into a valid Cypher identifier."""
    cleaned = _LABEL_RE.sub("", str(value))
    return cleaned or "Entity"


class KGPipeline:
    """End-to-end GraphRAG pipeline behind the service boundary.

    Args:
        config: Pipeline configuration (LLM key, model, vector settings).
        graph_service: GraphService implementation (FalkorDB in prod; fake in tests).
        embedding_service: Async embedder with ``await embed(text) -> list[float]``.
            When ``None``, vector persistence is skipped (degraded).
        qdrant_client: Qdrant client (sync API, as elsewhere in the app).
            When ``None``, vector persistence is skipped (degraded).
        llm_extractor: Optional injection seam for the LLM extraction stage
            (tests pass a stub). When omitted and a key is configured, the
            Semantica-backed LLM extractor is built.

    Raises:
        KGPipelineUnavailable: If Semantica cannot be imported or the
            GraphBuilder stage cannot be constructed.
    """

    def __init__(
        self,
        config: PipelineConfig,
        graph_service: Any,
        embedding_service: Any | None = None,
        qdrant_client: Any | None = None,
        llm_extractor: LLMExtractor | None = None,
    ) -> None:
        self._config = config
        self._graph = graph_service
        self._vectors = VectorWriter(
            qdrant_client=qdrant_client,
            embedding_service=embedding_service,
            collection=config.vector_collection,
            embedding_dim=config.embedding_dim,
        )

        # Critical Semantica stage: construction failure is a controlled 503.
        try:
            self._builder = _semantica._load_graph_builder()
        except Exception as e:  # noqa: BLE001 — boundary converts any failure
            logger.error("Semantica pipeline unavailable (GraphBuilder build failed): %s", e)
            raise KGPipelineUnavailable(f"Semantica GraphBuilder unavailable: {e}") from e

        # Extraction stage selection.
        self._llm: LLMExtractor | None = None
        self._extraction_mode = "pattern"
        if config.llm_api_key:
            self._llm = llm_extractor or self._build_llm_extractor(config)
            if self._llm is not None:
                self._extraction_mode = "llm"

    @staticmethod
    def _build_llm_extractor(config: PipelineConfig) -> LLMExtractor | None:
        """Build the Semantica LLM extraction stage; degrade to pattern on failure."""
        try:
            return _semantica.SemanticaLLMExtractor(
                api_key=config.llm_api_key or "",
                model=config.llm_model,
                base_url=config.llm_base_url,
            )
        except Exception as e:  # noqa: BLE001 — degrade explicitly, do not crash boot
            logger.warning(
                "LLM extraction stage unavailable (%s); falling back to pattern extractors",
                e,
            )
            return None

    @property
    def extraction_mode(self) -> str:
        """Active extraction stage: ``"pattern"`` or ``"llm"``."""
        return self._extraction_mode

    async def ingest(self, document: Document) -> PipelineResult:
        """Run a source document through the full pipeline and persist it.

        Args:
            document: The source document to ingest.

        Returns:
            A :class:`PipelineResult` with plain-dict entities/relationships
            and persistence counts.

        Raises:
            Exception: Graph write errors propagate (breaker-protected by the
                GraphService); callers map them to 503/502.
        """
        entities, relationships = self._extract(document.text)

        merged = _semantica.build_merged_graph(self._builder, entities, relationships)
        merged_entities = merged.get("entities", [])
        merged_rels = merged.get("relationships", [])

        nodes_written = await self._persist_graph(document, merged_entities, merged_rels)
        edges_written = await self._persist_edges(document, merged_rels)
        vectors_written = await self._vectors.write_entities(
            document.doc_id,
            document.source,
            document.metadata,
            merged_entities,
        )

        logger.info(
            "KG ingest doc=%s mode=%s entities=%d relationships=%d nodes=%d edges=%d vectors=%d",
            document.doc_id,
            self._extraction_mode,
            len(merged_entities),
            len(merged_rels),
            nodes_written,
            edges_written,
            vectors_written,
        )
        return PipelineResult(
            doc_id=document.doc_id,
            extraction_mode=self._extraction_mode,
            entities=merged_entities,
            relationships=merged_rels,
            graph_nodes_written=nodes_written,
            graph_edges_written=edges_written,
            vectors_written=vectors_written,
        )

    # ------------------------------------------------------------------ #
    # Extraction
    # ------------------------------------------------------------------ #

    def _extract(self, text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if self._llm is not None:
            return self._extract_llm(text)
        return self._extract_pattern(text)

    def _extract_pattern(self, text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Key-free domain pattern stage → dicts shaped for GraphBuilder."""
        result = domain_extract(text)
        entities = [
            {
                "id": f"pat-{i}",
                "name": e.text,
                "type": e.label,
                "confidence": e.confidence,
            }
            for i, e in enumerate(result.entities)
        ]
        relationships = [
            {
                "subject": r.subject.text,
                "predicate": r.predicate,
                "object": r.object.text,
                "confidence": r.confidence,
            }
            for r in result.relations
        ]
        return entities, relationships

    def _extract_llm(self, text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """LLM stage → normalized dicts shaped for GraphBuilder."""
        assert self._llm is not None
        payload = self._llm.extract(text)
        entities = payload.get("entities", [])
        relationships = [
            {
                "subject": r.get("source") or r.get("subject"),
                "predicate": r.get("type") or r.get("predicate"),
                "object": r.get("target") or r.get("object"),
                "confidence": r.get("confidence", 1.0),
            }
            for r in payload.get("relationships", [])
        ]
        return entities, relationships

    # ------------------------------------------------------------------ #
    # Persistence — graph (via injected GraphService)
    # ------------------------------------------------------------------ #

    async def _persist_graph(
        self,
        document: Document,
        entities: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
    ) -> int:
        """MERGE entity nodes (grouped by label) through the GraphService."""
        await self._graph.create_constraints()

        by_label: dict[str, list[dict[str, Any]]] = {}
        for ent in entities:
            name = ent.get("name") or ent.get("text")
            if not name:
                continue
            label = _safe_label(str(ent.get("type") or ent.get("label") or "Entity"))
            by_label.setdefault(label, []).append(
                {
                    "name": str(name),
                    "etype": label,
                    "doc_id": document.doc_id,
                    "source": document.source,
                }
            )

        count = 0
        for label, rows in by_label.items():
            statement = (
                f"UNWIND $rows AS row "
                f"MERGE (n:{label} {{name: row.name}}) "
                f"SET n.type = row.etype, n.doc_id = row.doc_id, "
                f"n.source = row.source, n.last_seen = timestamp()"
            )
            await self._graph.write(statement, {"rows": rows})
            count += len(rows)
        return count

    async def _persist_edges(
        self,
        document: Document,
        relationships: list[dict[str, Any]],
    ) -> int:
        """MERGE relationship edges (grouped by type) through the GraphService."""
        by_type: dict[str, list[dict[str, Any]]] = {}
        for rel in relationships:
            subject = rel.get("subject") or rel.get("source")
            obj = rel.get("object") or rel.get("target")
            predicate = rel.get("predicate") or rel.get("type")
            if not subject or not obj or not predicate:
                continue
            rel_type = _safe_label(str(predicate).upper().replace(" ", "_"))
            by_type.setdefault(rel_type, []).append(
                {"subject": str(subject), "object": str(obj), "doc_id": document.doc_id}
            )

        count = 0
        for rel_type, rows in by_type.items():
            statement = (
                "UNWIND $rows AS row "
                "MERGE (s {name: row.subject}) "
                "MERGE (o {name: row.object}) "
                f"MERGE (s)-[r:{rel_type}]->(o) "
                "SET r.doc_id = row.doc_id, r.last_seen = timestamp()"
            )
            await self._graph.write(statement, {"rows": rows})
            count += len(rows)
        return count


__all__ = ["KGPipeline"]
