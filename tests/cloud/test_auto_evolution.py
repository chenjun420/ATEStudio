"""Tests for task 16 — automatic failure→KG evolution on indexed failures.

When a real edge failure (STEP_FAILED / EXECUTION_COMPLETED(FAILED) /
SPC ALARM_RAISED) is indexed by :class:`FailureIndexer`, the system now
AUTOMATICALLY triggers KG evolution through the existing task-7
:class:`~ate_cloud.services.kg_pipeline.KGPipeline` (extract → merge →
GraphService writes + Qdrant vectors), without requiring the manual
``POST /faults/evolve`` call.

Contract under test (all with fakes — no live FalkorDB/Qdrant/OpenAI):

- indexing a failed-step event invokes the evolution path (a GraphService
  MERGE write is attempted and KG vectors are upserted);
- evolution is idempotent (stable document id + MERGE on stable ids);
- ANY evolution failure (pipeline raises, graph down, no pipeline at all)
  is swallowed: failure indexing still succeeds and nothing propagates;
- no LLM key → pattern extractors still run (graph still written);
- the FailureIndexer constructed without a trigger behaves exactly as before.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from ate_cloud.services.failure_evolution import (
    FailureEvolutionTrigger,
    build_failure_document,
)
from ate_cloud.services.failure_indexer import FailureIndexer
from ate_cloud.services.kg_pipeline import KGPipeline, PipelineConfig
from shared.events import Event, EventType

# ---------------------------------------------------------------------------
# Fakes (no FalkorDB / Qdrant server / OpenAI)
# ---------------------------------------------------------------------------


class FakeGraphService:
    """In-memory GraphService double recording Cypher MERGE writes."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.constraints_created = False
        self.raise_on_write = False

    async def query(
        self, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return []

    async def write(
        self, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if self.raise_on_write:
            raise ConnectionError("falkordb down")
        self.writes.append((statement, params or {}))
        return []

    async def create_constraints(self) -> None:
        self.constraints_created = True

    async def count_nodes(self) -> int:
        return len(self.writes)

    async def count_relationships(self) -> int:
        return 0

    async def health(self) -> dict[str, Any]:
        return {"status": "healthy", "backend": "fake"}


class FakeEmbedding:
    """Deterministic async embedder (dim=4) — no network."""

    def __init__(self, dim: int = 4) -> None:
        self._dim = dim
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        h = abs(hash(text))
        return [float((h >> (i * 4)) & 0xF) / 15.0 for i in range(self._dim)]


class FakeQdrant:
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


class BoomPipeline:
    """Stand-in pipeline whose ingest always raises (evolution fault injection)."""

    def __init__(self) -> None:
        self.ingest_calls = 0

    async def ingest(self, document: Any) -> Any:
        self.ingest_calls += 1
        raise RuntimeError("pipeline exploded")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config() -> PipelineConfig:
    # NO LLM key → pattern extractors (key-free) must still drive graph writes.
    return PipelineConfig(
        llm_api_key=None,
        llm_model="gpt-4o-mini",
        embedding_dim=4,
        vector_collection="ate_kg_entities",
    )


def _failed_step_event() -> Event:
    """A STEP_FAILED event whose text the domain pattern extractor recognizes."""
    return Event(
        type=EventType.STEP_FAILED,
        data={
            "step_id": "functional_test",
            "failed_step_id": "functional_test",
            "failed_step_name": "functional test",
            "error_message": (
                "Power supply PSU-1 exhibits excessive ripple on the 3.3V rail; "
                "the root cause is a degraded capacitor C12; replacing "
                "capacitor C12 resolves the fault; instrument DMM-42 measures "
                "the 3.3V rail"
            ),
            "variable_snapshot": {"voltage": 3.28},
            "run_id": "run-auto-001",
            "plan_name": "power_test",
        },
    )


@pytest.fixture
def fakes() -> tuple[FakeGraphService, FakeEmbedding, FakeQdrant]:
    return FakeGraphService(), FakeEmbedding(), FakeQdrant()


def _indexer_with_trigger(
    fakes: tuple[FakeGraphService, FakeEmbedding, FakeQdrant],
    trigger: FailureEvolutionTrigger,
) -> tuple[FailureIndexer, FakeQdrant]:
    graph, embedder, qdrant = fakes
    indexer = FailureIndexer(
        qdrant_client=qdrant,
        embedding_service=embedder,
        collection_name="ate_failures",
        embedding_dim=4,
    )
    indexer.set_evolution_trigger(trigger.evolve_from_failure)
    return indexer, qdrant


# ---------------------------------------------------------------------------
# Tests: pure document builder (idempotency)
# ---------------------------------------------------------------------------


def test_failure_document_is_deterministic_and_sourced() -> None:
    """build_failure_document yields a stable id for identical failure content."""
    metadata = {
        "event_type": "STEP_FAILED",
        "run_id": "run-1",
        "failed_step_id": "rf_cal",
        "error_message": "VISA timeout",
    }
    doc1 = build_failure_document(metadata)
    doc2 = build_failure_document(dict(metadata))
    other = build_failure_document({**metadata, "run_id": "run-2"})

    assert doc1.doc_id == doc2.doc_id  # same failure → same id (idempotent upsert)
    assert doc1.doc_id != other.doc_id  # different run → different id
    assert doc1.source == "auto_failure_evolution"
    assert "VISA timeout" in doc1.text
    assert doc1.metadata["run_id"] == "run-1"


# ---------------------------------------------------------------------------
# Tests: auto evolution on indexed failure
# ---------------------------------------------------------------------------


async def test_indexing_failure_invokes_kg_evolution_graph_write(
    fakes: tuple[FakeGraphService, FakeEmbedding, FakeQdrant],
) -> None:
    """Given a failed-step event, indexing triggers a GraphService MERGE write."""
    graph, _embedder, _qdrant = fakes
    pipeline = KGPipeline(
        config=_config(), graph_service=graph, embedding_service=fakes[1],
        qdrant_client=fakes[2],
    )
    trigger = FailureEvolutionTrigger(pipeline=pipeline)
    indexer, qdrant = _indexer_with_trigger(fakes, trigger)

    indexer.index_failure(_failed_step_event())
    await asyncio.sleep(0.1)  # let the background index + evolve task finish

    # Failure was indexed into the RAG failure collection...
    assert qdrant.points.get("ate_failures"), "failure index point must be upserted"
    # ...AND the KG evolution path wrote to the graph (MERGE) via GraphService.
    assert graph.constraints_created, "evolution pipeline must ensure constraints"
    merge_writes = [stmt for stmt, _params in graph.writes if "MERGE" in stmt]
    assert merge_writes, "evolution must attempt graph MERGE writes"
    # Pattern extractors (no LLM key) still produced KG entity vectors.
    assert qdrant.points.get("ate_kg_entities"), "KG entity vectors must be upserted"


async def test_evolution_trigger_ingests_a_failure_document(
    fakes: tuple[FakeGraphService, FakeEmbedding, FakeQdrant],
) -> None:
    """The trigger hands pipeline.ingest a Document built from failure metadata."""
    graph, embedder, qdrant = fakes
    pipeline = KGPipeline(
        config=_config(), graph_service=graph, embedding_service=embedder,
        qdrant_client=qdrant,
    )
    trigger = FailureEvolutionTrigger(pipeline=pipeline)

    metadata = {
        "event_type": "STEP_FAILED",
        "run_id": "run-x",
        "failed_step_name": "functional test",
        "error_message": "PSU-1 exhibits excessive ripple; root cause is a "
        "degraded capacitor C12",
    }
    result = await trigger.evolve_from_failure(metadata)

    assert result is True
    assert graph.writes, "graph write attempted via FakeGraphStore"
    assert any("MERGE" in stmt for stmt, _p in graph.writes)


async def test_evolution_failure_does_not_break_indexing() -> None:
    """Fault injection: pipeline raises → indexing still succeeds, nothing propagates."""
    qdrant = FakeQdrant()
    embedder = FakeEmbedding()
    indexer = FailureIndexer(
        qdrant_client=qdrant,
        embedding_service=embedder,
        collection_name="ate_failures",
        embedding_dim=4,
    )
    boom = BoomPipeline()
    indexer.set_evolution_trigger(FailureEvolutionTrigger(pipeline=boom).evolve_from_failure)

    # Must not raise even though evolution explodes.
    indexer.index_failure(_failed_step_event())
    await asyncio.sleep(0.1)

    # Failure indexing completed despite the evolution failure.
    assert qdrant.points.get("ate_failures"), "failure must still be indexed"
    assert boom.ingest_calls == 1, "evolution was attempted once"

    # Directly, the trigger reports failure as False (swallowed, non-fatal).
    ok = await FailureEvolutionTrigger(pipeline=boom).evolve_from_failure(
        {"error_message": "x", "run_id": "r"}
    )
    assert ok is False


async def test_graph_down_skips_evolution_but_indexing_succeeds(
    fakes: tuple[FakeGraphService, FakeEmbedding, FakeQdrant],
) -> None:
    """Graph backend down: evolution degrades to a logged skip, indexing proceeds."""
    graph, embedder, qdrant = fakes
    graph.raise_on_write = True
    pipeline = KGPipeline(
        config=_config(), graph_service=graph, embedding_service=embedder,
        qdrant_client=qdrant,
    )
    trigger = FailureEvolutionTrigger(pipeline=pipeline)
    indexer, qdrant_store = _indexer_with_trigger(fakes, trigger)

    ok = await trigger.evolve_from_failure(
        {"error_message": "PSU-1 ripple fault", "run_id": "r-down"}
    )
    assert ok is False  # graph failure swallowed

    indexer.index_failure(_failed_step_event())
    await asyncio.sleep(0.1)
    assert qdrant_store.points.get("ate_failures"), "indexing survives graph outage"


async def test_no_pipeline_configured_skips_evolution_gracefully(
    fakes: tuple[FakeGraphService, FakeEmbedding, FakeQdrant],
) -> None:
    """No graph/pipeline wired (e.g. app booted without graph): silent skip."""
    graph, _embedder, _qdrant = fakes
    trigger = FailureEvolutionTrigger()  # no pipeline, no resolver

    ok = await trigger.evolve_from_failure(
        {"error_message": "some fault", "run_id": "r-none"}
    )
    assert ok is False
    assert graph.writes == []


async def test_indexer_without_trigger_indexes_as_before(
    fakes: tuple[FakeGraphService, FakeEmbedding, FakeQdrant],
) -> None:
    """Default FailureIndexer (no trigger) indexes failures with no evolution."""
    graph, embedder, qdrant = fakes
    indexer = FailureIndexer(
        qdrant_client=qdrant,
        embedding_service=embedder,
        collection_name="ate_failures",
        embedding_dim=4,
    )
    indexer.index_failure(_failed_step_event())
    await asyncio.sleep(0.1)

    assert qdrant.points.get("ate_failures")
    assert graph.writes == []  # no trigger → no graph evolution


async def test_same_failure_evolves_with_same_document_id(
    fakes: tuple[FakeGraphService, FakeEmbedding, FakeQdrant],
) -> None:
    """Re-indexing the identical failure feeds the pipeline the same doc id."""
    graph, embedder, qdrant = fakes
    seen_doc_ids: list[str] = []

    class RecordingPipeline:
        async def ingest(self, document: Any) -> Any:
            seen_doc_ids.append(document.doc_id)
            return SimpleNamespace(graph_nodes_written=1)

    trigger = FailureEvolutionTrigger(pipeline=RecordingPipeline())
    metadata = {"event_type": "STEP_FAILED", "run_id": "dup", "error_message": "same fault"}

    await trigger.evolve_from_failure(metadata)
    await trigger.evolve_from_failure(dict(metadata))

    assert len(seen_doc_ids) == 2
    assert seen_doc_ids[0] == seen_doc_ids[1]  # idempotent identity
