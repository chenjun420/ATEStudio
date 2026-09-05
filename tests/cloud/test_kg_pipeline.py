"""Tests for the Semantica GraphRAG pipeline service boundary (task 7).

The pipeline is the ONLY place in the application that imports Semantica.
These tests exercise it entirely behind fakes — no FalkorDB, no Qdrant
server, no OpenAI key — and assert:

- Pattern extractors (no LLM key) extract domain entities + relationships
  from a fault document and write to BOTH the graph (GraphService) and the
  vector store (Qdrant) via injected clients.
- When an LLM extractor is selected (key present), the LLM path is chosen.
- Semantica import/construction failure is converted to a controlled
  ``KGPipelineUnavailable`` exception (callers map to 503); the app still
  boots and non-graph paths are unaffected.
- Semantica types never cross the facade boundary (result is plain dicts).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ate_cloud.services.kg_pipeline import (
    Document,
    KGPipeline,
    KGPipelineUnavailable,
    PipelineConfig,
    PipelineResult,
    build_pipeline,
)

# A short, fault-style document for the end-to-end pattern path.
FAULT_DOC = Document(
    doc_id="fault-001",
    text=(
        "Power supply PSU-1 on station SMT-01 exhibits excessive ripple on the "
        "3.3V rail during functional test. The root cause is a degraded "
        "capacitor C12. Replacing capacitor C12 resolves the fault. "
        "Instrument DMM-42 measures the 3.3V voltage on the power rail."
    ),
    source="fixture",
    metadata={"product": "ServerBoard A1"},
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeGraphService:
    """In-memory GraphService double recording Cypher writes (no FalkorDB)."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.constraints_created = False
        self.nodes: dict[tuple[str, str], dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []

    async def query(
        self, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return []

    async def write(
        self, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.writes.append((statement, params or {}))
        return []

    async def create_constraints(self) -> None:
        self.constraints_created = True

    async def count_nodes(self) -> int:
        return len(self.nodes)

    async def count_relationships(self) -> int:
        return len(self.edges)

    async def health(self) -> dict[str, Any]:
        return {"status": "healthy", "backend": "fake"}


class FakeEmbeddingService:
    """Deterministic async embedder (dim=4) — no network."""

    def __init__(self, dim: int = 4) -> None:
        self._dim = dim
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        # Deterministic pseudo-embedding from the text hash.
        h = abs(hash(text))
        return [float((h >> (i * 4)) & 0xF) / 15.0 for i in range(self._dim)]

    @property
    def dimensions(self) -> int:
        return self._dim


class FakeQdrantClient:
    """Minimal in-memory Qdrant double tracking collections + points."""

    def __init__(self) -> None:
        self.collections: dict[str, int] = {}
        self.points: dict[str, list[Any]] = {}

    def get_collections(self) -> Any:
        result = SimpleNamespace()
        result.collections = [SimpleNamespace(name=n) for n in self.collections]
        return result

    def create_collection(
        self, collection_name: str, vectors_config: Any = None, **_: Any
    ) -> None:
        self.collections.setdefault(collection_name, getattr(vectors_config, "size", 4))
        self.points.setdefault(collection_name, [])

    def upsert(self, collection_name: str, points: list[Any]) -> None:
        store = self.points.setdefault(collection_name, [])
        for point in points:
            store[:] = [p for p in store if p.id != point.id]
            store.append(point)


class StubLLMExtractor:
    """Stand-in for the LLM-backed extraction stage.

    Returns a fixed, LLM-flavoured extraction so the test can prove the LLM
    path was selected (entities carry ``extraction_method == "llm"``).
    """

    def __init__(self) -> None:
        self.called = False

    def extract(self, text: str) -> dict[str, Any]:
        self.called = True
        return {
            "entities": [
                {"id": "llm-1", "name": "PSU-1", "type": "Component"},
                {"id": "llm-2", "name": "ripple anomaly", "type": "Symptom"},
            ],
            "relationships": [
                {"source": "PSU-1", "target": "ripple anomaly", "type": "EXHIBITS"},
            ],
        }


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def fakes() -> tuple[FakeGraphService, FakeEmbeddingService, FakeQdrantClient]:
    return FakeGraphService(), FakeEmbeddingService(), FakeQdrantClient()


def _config(**overrides: Any) -> PipelineConfig:
    base: dict[str, Any] = {
        "llm_api_key": None,
        "llm_model": "gpt-4o-mini",
        "embedding_dim": 4,
        "vector_collection": "ate_kg_entities",
    }
    base.update(overrides)
    return PipelineConfig(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


async def test_pattern_extraction_writes_to_graph_and_vectors(
    fakes: tuple[FakeGraphService, FakeEmbeddingService, FakeQdrantClient],
) -> None:
    """Given a fault doc and NO LLM key, pattern extractors pull domain
    entities/relationships and the pipeline persists to graph + Qdrant."""
    graph, embedder, qdrant = fakes
    pipeline = KGPipeline(
        config=_config(),
        graph_service=graph,
        embedding_service=embedder,
        qdrant_client=qdrant,
    )

    result = await pipeline.ingest(FAULT_DOC)

    # Result is a plain dataclass of plain data — no Semantica type leaks.
    assert isinstance(result, PipelineResult)
    assert result.extraction_mode == "pattern"
    assert result.doc_id == "fault-001"
    # Domain pattern extractor must find the labelled entities.
    labels = {e["type"] for e in result.entities}
    assert {"Component", "Instrument"} <= labels
    names = {e["name"] for e in result.entities}
    assert any("PSU-1" in n for n in names), names
    assert "DMM-42" in names
    assert len(result.relationships) >= 1
    # Graph received writes (MERGE nodes / edges), constraints ensured.
    assert graph.constraints_created
    assert graph.writes, "expected at least one graph write statement"
    # Vectors: collection created and one point per entity upserted.
    assert "ate_kg_entities" in qdrant.collections
    assert len(qdrant.points["ate_kg_entities"]) == len(result.entities)
    # Embeddings were computed for the entity texts.
    assert embedder.calls, "expected embedding calls"


async def test_llm_path_selected_when_key_present(
    fakes: tuple[FakeGraphService, FakeEmbeddingService, FakeQdrantClient],
) -> None:
    """Given an API key and an injected LLM extractor, the LLM stage runs."""
    graph, embedder, qdrant = fakes
    llm = StubLLMExtractor()
    pipeline = KGPipeline(
        config=_config(llm_api_key="sk-test"),
        graph_service=graph,
        embedding_service=embedder,
        qdrant_client=qdrant,
        llm_extractor=llm,  # injection seam; production builds Semantica LLM stage
    )

    result = await pipeline.ingest(FAULT_DOC)

    assert llm.called, "LLM extractor must be invoked when a key is configured"
    assert result.extraction_mode == "llm"
    assert {e["name"] for e in result.entities} == {"PSU-1", "ripple anomaly"}
    # LLM-extracted graph is still persisted to both stores.
    assert graph.writes
    assert len(qdrant.points["ate_kg_entities"]) == 2


async def test_construction_failure_raises_controlled_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Semantica construction fails, the boundary raises
    KGPipelineUnavailable (callers map to 503); app boot is unaffected."""

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("semantica exploded")

    # Force the internal Semantica GraphBuilder construction to fail.
    monkeypatch.setattr(
        "ate_cloud.services.kg_pipeline._semantica._load_graph_builder",
        staticmethod(_boom),
        raising=True,
    )

    graph, embedder, qdrant = FakeGraphService(), FakeEmbeddingService(), FakeQdrantClient()
    with pytest.raises(KGPipelineUnavailable):
        KGPipeline(
            config=_config(),
            graph_service=graph,
            embedding_service=embedder,
            qdrant_client=qdrant,
        )

    # The rest of the app is unaffected: graph/vector fakes are untouched
    # and independently usable (proving the boundary contained the failure).
    assert graph.writes == []


async def test_build_pipeline_factory_returns_facade(
    fakes: tuple[FakeGraphService, FakeEmbeddingService, FakeQdrantClient],
) -> None:
    """The ``build_pipeline`` factory wires injected deps into a KGPipeline."""
    graph, embedder, qdrant = fakes
    pipeline = build_pipeline(
        graph_service=graph,
        embedding_service=embedder,
        qdrant_client=qdrant,
        config=_config(),
    )
    assert isinstance(pipeline, KGPipeline)
    result = await pipeline.ingest(FAULT_DOC)
    assert result.doc_id == "fault-001"


async def test_no_qdrant_degrades_vector_but_graph_still_written(
    fakes: tuple[FakeGraphService, FakeEmbeddingService, FakeQdrantClient],
) -> None:
    """Qdrant unavailable (None) must not block graph persistence."""
    graph, embedder, _qdrant = fakes
    pipeline = KGPipeline(
        config=_config(),
        graph_service=graph,
        embedding_service=embedder,
        qdrant_client=None,
    )
    result = await pipeline.ingest(FAULT_DOC)
    assert result.vectors_written == 0
    assert graph.writes, "graph writes must proceed without Qdrant"
