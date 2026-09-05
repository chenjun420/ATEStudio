"""Boundary data types for the KG pipeline — plain, typed, Semantica-free.

Nothing in this module imports Semantica: the facade speaks only these
frozen dataclasses / plain dicts so callers never touch a Semantica type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Document:
    """A single source document fed to the pipeline.

    Args:
        doc_id: Stable identifier for provenance / idempotent upserts.
        text: The raw text content to parse and extract from.
        source: Origin label (e.g. ``"fault_feedback"``, ``"seed_vocab"``).
        metadata: Optional scalar provenance fields (product, run, …).
    """

    doc_id: str
    text: str
    source: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Configuration for :class:`~ate_cloud.services.kg_pipeline.pipeline.KGPipeline`.

    Args:
        llm_api_key: LLM API key. When falsy, pattern extractors are used and
            no LLM call is attempted; when set, the LLM extraction stage is
            selected.
        llm_model: Chat model name for the LLM extraction stage.
        llm_base_url: Optional OpenAI-compatible base URL (DashScope/…).
        embedding_dim: Embedding vector dimensionality (Qdrant collection size).
        vector_collection: Qdrant collection for KG entity vectors.
    """

    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str | None = None
    embedding_dim: int = 1536
    vector_collection: str = "ate_kg_entities"


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """The plain-data outcome of ingesting one document.

    Entities/relationships are plain dicts (never Semantica types) so the
    result crosses the service boundary safely.
    """

    doc_id: str
    extraction_mode: str  # "pattern" | "llm"
    entities: list[dict[str, Any]]
    relationships: list[dict[str, Any]]
    graph_nodes_written: int
    graph_edges_written: int
    vectors_written: int

    @property
    def degraded_vectors(self) -> bool:
        """True when vector persistence was skipped (Qdrant unavailable)."""
        return self.vectors_written == 0 and bool(self.entities)


@runtime_checkable
class LLMExtractor(Protocol):
    """Seam for the LLM-backed extraction stage.

    The production adapter wraps Semantica's LLM extractors; tests inject a
    stub returning a fixed ``{"entities": [...], "relationships": [...]}``
    payload so no network/key is required.
    """

    def extract(self, text: str) -> dict[str, Any]:
        """Extract entities and relationships from ``text``.

        Returns a dict with ``entities`` (list of ``{"id","name","type"}``)
        and ``relationships`` (list of ``{"source","target","type"}``).
        """
        ...


__all__ = ["Document", "LLMExtractor", "PipelineConfig", "PipelineResult"]
