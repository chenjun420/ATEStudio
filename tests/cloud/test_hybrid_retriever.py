"""Tests for HybridRetriever — Qdrant vector + ontology-KG fusion (task 14).

The retriever fuses two branches on STABLE ONTOLOGY ENTITY IDS (no text-prefix
heuristic, no legacy ``FaultSymptom`` label):

* Qdrant semantic hits, normalized to ``entity_id`` (payload ``entity_id`` or
  an ``error_code`` resolved to ``fault:<slug(code)>``);
* ontology-KG traversal (:mod:`ate_cloud.services.kg_retrieval`) seeded from
  the request error code and the vector hits' entity ids.

All dependencies are fakes:
* ``OntologyGraphFake`` — in-memory graph seeded from the real
  ``build_seed_graph()``, answers the retrieval Cypher shapes;
* ``FakeQdrant`` / ``FakeEmbedding`` — deterministic vector hits/embeddings.

No live FalkorDB/Qdrant/OpenAI. Covered: ontology graph traversal, shared-id
fusion, per-branch circuit-breaker degrade, and removal of the legacy
``_match_key`` / ``_search_neo4j`` join.
"""

from __future__ import annotations

from typing import Any

import pytest

from ate_cloud.services.hybrid_fusion import (
    RRF_K,
    fusion_key,
    reciprocal_rank_fusion,
)
from ate_cloud.services.hybrid_retriever import HybridRetriever
from ate_cloud.services.kg_retrieval import (
    extract_keyword,
    fault_entity_id,
    retrieve_faults,
)

from .ontology_graph_fake import OntologyGraphFake

# ── Fakes ─────────────────────────────────────────────────────────────────


class _Point:
    def __init__(self, point_id: str, score: float, payload: dict[str, Any]) -> None:
        self.id = point_id
        self.score = score
        self.payload = payload


class FakeQdrant:
    """Minimal Qdrant client: returns scripted points, records calls."""

    def __init__(self, points: list[_Point] | None = None) -> None:
        self._points = points or []
        self.searches: list[dict[str, Any]] = []
        self.fail: Exception | None = None

    def search(self, **kwargs: Any) -> list[_Point]:
        self.searches.append(kwargs)
        if self.fail is not None:
            raise self.fail
        limit = kwargs.get("limit", len(self._points))
        return self._points[:limit]


class FakeEmbedding:
    """Embedding service stand-in: deterministic vectors, batch works."""

    async def embed(self, text: str) -> list[float]:
        return [0.1] * 8

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 8 for _ in texts]


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def graph() -> OntologyGraphFake:
    return OntologyGraphFake().seed_ontology()


@pytest.fixture
def qdrant() -> FakeQdrant:
    return FakeQdrant()


@pytest.fixture
def retriever(
    graph: OntologyGraphFake, qdrant: FakeQdrant
) -> HybridRetriever:
    return HybridRetriever(
        embedding_service=FakeEmbedding(),  # type: ignore[arg-type]
        graph_service=graph,  # type: ignore[arg-type]
        qdrant_client=qdrant,
        collection_name="test_fault_cases",
        api_key="",  # no LLM: dictionary-only rewrite (deterministic)
        embedding_dim=8,
    )


# ── kg_retrieval: ontology graph traversal ────────────────────────────────


class TestOntologyGraphRetrieval:
    async def test_fault_found_by_stable_id_from_error_code(
        self, graph: OntologyGraphFake
    ) -> None:
        """An error code resolves to the seed Fault id and enriches the chain."""
        fid = fault_entity_id("I2C_TIMEOUT")
        results = await retrieve_faults(graph, candidate_ids=[fid], limit=5)

        assert len(results) == 1
        row = results[0]
        assert row["id"] == fid
        assert row["source"] == "graph"
        assert row["error_code"] == "I2C_TIMEOUT"
        # Fault -> Symptom -> Cause -> Solution chain is enriched.
        assert row["symptom"]
        assert row["cause"]
        assert row["solution"]
        assert row["component"]
        assert row["instrument"]

    async def test_unknown_id_returns_empty(self, graph: OntologyGraphFake) -> None:
        """A candidate id that matches no Fault yields no rows."""
        results = await retrieve_faults(
            graph, candidate_ids=["fault:does_not_exist"], limit=5
        )
        assert results == []

    async def test_keyword_fallback_finds_fault(
        self, graph: OntologyGraphFake
    ) -> None:
        """Free-text keyword scans ontology Fault/Symptom/Cause properties."""
        results = await retrieve_faults(graph, keyword="framing", limit=10)
        codes = {r["error_code"] for r in results}
        assert "UART_FRAME_ERR" in codes

    async def test_keyword_fallback_empty_when_no_match(
        self, graph: OntologyGraphFake
    ) -> None:
        results = await retrieve_faults(graph, keyword="zzz_no_such_token_xyz", limit=10)
        assert results == []

    async def test_no_ids_no_keyword_returns_empty(
        self, graph: OntologyGraphFake
    ) -> None:
        assert await retrieve_faults(graph) == []

    def test_fault_entity_id_matches_seed_slug_scheme(self) -> None:
        """fault_entity_id mirrors kg_seed_facts._fault_node_id."""
        assert fault_entity_id("I2C_TIMEOUT") == "fault:i2c_timeout"
        assert fault_entity_id("ERR I2C TIMEOUT") == "fault:err_i2c_timeout"

    def test_extract_keyword_picks_longest_specific_token(self) -> None:
        assert extract_keyword("I2C communication timeout on bus") == "communication"
        assert extract_keyword("error failure fault test") == ""
        assert extract_keyword("") == ""


# ── Shared-ID fusion (pure function) ──────────────────────────────────────


class TestIdFusion:
    def test_same_entity_id_fuses(self) -> None:
        """A vector hit and graph hit with the same entity id merge to 'fused'."""
        fid = "fault:i2c_timeout"
        vector = [{"id": "p1", "score": 0.9, "source": "qdrant",
                   "entity_id": fid, "error_code": "I2C_TIMEOUT"}]
        graph_rows = [{"id": fid, "score": 0.0, "source": "graph",
                       "entity_id": fid, "symptom": "I2C bus failure",
                       "cause": "pull-up", "solution": "add resistor"}]

        fused = reciprocal_rank_fusion(vector, graph_rows)
        assert len(fused) == 1
        assert fused[0]["source"] == "fused"
        # Graph relationship fields merge into the fused entry.
        assert fused[0]["cause"] == "pull-up"
        assert fused[0]["solution"] == "add resistor"
        # RRF score accumulates from both ranked lists.
        assert fused[0]["rrf_score"] == pytest.approx(
            1.0 / (RRF_K + 1) + 1.0 / (RRF_K + 1)
        )

    def test_distinct_ids_do_not_fuse(self) -> None:
        """Hits with different keys stay separate entries."""
        vector = [{"id": "p1", "score": 0.9, "entity_id": "fault:a"}]
        graph_rows = [{"id": "fault:b", "score": 0.0, "entity_id": "fault:b"}]
        fused = reciprocal_rank_fusion(vector, graph_rows)
        assert len(fused) == 2

    def test_vector_hit_without_entity_uses_point_key(self) -> None:
        """A free failure case (no entity) can never falsely fuse with a Fault."""
        key = fusion_key({"id": "uuid-1", "score": 0.5})
        assert key == "point:uuid-1"
        assert key != fusion_key({"id": "fault:x", "entity_id": "fault:x"})


# ── End-to-end HybridRetriever.search ─────────────────────────────────────


class TestHybridSearch:
    async def test_vector_and_graph_fuse_on_error_code(
        self, retriever: HybridRetriever, qdrant: FakeQdrant
    ) -> None:
        """A Qdrant hit carrying error_code I2C_TIMEOUT fuses with the KG Fault."""
        qdrant._points = [
            _Point("point-1", 0.91, {
                "error_code": "I2C_TIMEOUT",
                "fault_symptom": "I2C bus communication failure",
                "root_cause": "missing pull-up",
            }),
        ]
        results = await retriever.search(
            "I2C bus failure", top_k=5, rerank=False, error_code="I2C_TIMEOUT"
        )
        assert results
        top = results[0]
        # The top result is the fused fault: present in BOTH branches.
        assert top["source"] == "fused"
        assert top["entity_id"] == fault_entity_id("I2C_TIMEOUT")
        # Graph-enriched relationship fields are attached.
        assert top["cause"]
        assert top["solution"]

    async def test_error_code_seeds_graph_without_vector_match(
        self, retriever: HybridRetriever, qdrant: FakeQdrant
    ) -> None:
        """With no vector hit, the structured error code still retrieves from KG."""
        results = await retriever.search(
            "some fault", top_k=5, rerank=False, error_code="SPI_MODE_ERR"
        )
        assert results
        assert any(r.get("error_code") == "SPI_MODE_ERR" for r in results)
        assert any(r["source"] == "graph" for r in results)

    async def test_graph_failure_degrades_to_vector_only(
        self, retriever: HybridRetriever, graph: OntologyGraphFake,
        qdrant: FakeQdrant,
    ) -> None:
        """A graph outage logs and returns the surviving vector branch."""
        graph.fail_with = RuntimeError("FalkorDB unreachable")
        qdrant._points = [
            _Point("p1", 0.8, {"error_message": "step failed"}),
        ]
        results = await retriever.search("fault A", top_k=5, rerank=False)
        assert len(results) == 1
        assert results[0]["source"] == "qdrant"

    async def test_vector_failure_degrades_to_graph_only(
        self, retriever: HybridRetriever, qdrant: FakeQdrant
    ) -> None:
        """A Qdrant outage logs and returns the surviving graph branch."""
        qdrant.fail = RuntimeError("Qdrant down")
        results = await retriever.search(
            "SPI clock polarity", top_k=5, rerank=False, error_code="SPI_MODE_ERR"
        )
        assert results
        assert all(r["source"] == "graph" for r in results)
        assert any(r.get("error_code") == "SPI_MODE_ERR" for r in results)

    async def test_both_branches_empty_returns_empty(
        self, retriever: HybridRetriever, qdrant: FakeQdrant
    ) -> None:
        results = await retriever.search(
            "zzz unmatched token", top_k=5, rerank=False
        )
        assert results == []


# ── Legacy join removed (grep-proof) ──────────────────────────────────────


class TestLegacyJoinRemoved:
    def test_no_match_key_or_search_neo4j(self) -> None:
        """The ad-hoc text-prefix join and legacy Cypher branch are gone."""
        assert not hasattr(HybridRetriever, "_match_key")
        assert not hasattr(HybridRetriever, "_search_neo4j")
        assert not hasattr(HybridRetriever, "_extract_keyword")

    def test_constructor_uses_graph_service_param(
        self, graph: OntologyGraphFake, qdrant: FakeQdrant
    ) -> None:
        """The ctor parameter is graph_service (neo4j_service is gone)."""
        r = HybridRetriever(
            embedding_service=FakeEmbedding(),  # type: ignore[arg-type]
            graph_service=graph,  # type: ignore[arg-type]
            qdrant_client=qdrant,
            api_key="",
        )
        assert r._graph_service is graph
        with pytest.raises(TypeError):
            HybridRetriever(
                embedding_service=FakeEmbedding(),  # type: ignore[arg-type]
                neo4j_service=graph,  # type: ignore[call-arg]
                qdrant_client=qdrant,
            )


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
