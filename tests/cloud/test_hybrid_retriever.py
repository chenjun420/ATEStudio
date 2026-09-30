"""Tests for the retriever — Qdrant vector search with RRF and re-ranking.

What changed with the knowledge-graph removal
----------------------------------------------
This file used to cover two branches fused by Reciprocal Rank Fusion: Qdrant
semantic search, and ontology-KG traversal seeded from the request error code.
The second branch is gone — it needed a graph backend that was never deployed,
so every call logged ``Graph retrieval failed: Error 111`` and returned nothing.

What survives here is the part still in production:

* ``reciprocal_rank_fusion`` — kept as the seam where a second source would
  join, and covered here because it is the module's only remaining logic worth
  asserting on its own.
* The Qdrant branch and its degradation behaviour.

One behaviour is asserted that did not exist before: ``error_code`` is now
**ignored**. It used to seed graph traversal; if it were quietly repurposed into
a filter or a re-weighting, a fault case sharing a symptom but not an error code
would silently drop out of the results — and a retrieval that quietly drops
relevant cases is indistinguishable from a line with no history.
"""

from __future__ import annotations

from typing import Any

import pytest

from ate_cloud.services.hybrid_fusion import reciprocal_rank_fusion
from ate_cloud.services.hybrid_retriever import HybridRetriever
from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

# ── Fakes ─────────────────────────────────────────────────────────────────


class _Point:
    def __init__(self, point_id: str, score: float, payload: dict[str, Any] | None = None) -> None:
        self.id = point_id
        self.score = score
        self.payload = payload or {}


class _QueryResponse:
    """What ``query_points`` returns — the shape the compat layer normalises to."""

    def __init__(self, points: list[_Point]) -> None:
        self.points = points


class FakeQdrant:
    """Minimal Qdrant client: scripted points, records calls.

    Exposes ``query_points`` and deliberately **not** ``search``: the installed
    client (>=1.12) removed ``search``, and a fake that still had it would have
    let the removed-API regression through (see test_qdrant_client_compat.py).
    """

    def __init__(self, points: list[_Point] | None = None) -> None:
        self._points = points or []
        self.queries: list[dict[str, Any]] = []
        self.fail: Exception | None = None

    def query_points(self, **kwargs: Any) -> _QueryResponse:
        self.queries.append(kwargs)
        if self.fail is not None:
            raise self.fail
        limit = kwargs.get("limit", len(self._points))
        return _QueryResponse(self._points[:limit])


class FakeEmbedding:
    """Embedding service stand-in: deterministic vectors, batch works."""

    def __init__(self, dim: int = 8) -> None:
        self._dim = dim
        self.embedded: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.embedded.append(text)
        return [0.1] * self._dim

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self._dim for _ in texts]


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def qdrant() -> FakeQdrant:
    return FakeQdrant()


@pytest.fixture
def embedding() -> FakeEmbedding:
    return FakeEmbedding()


@pytest.fixture
def retriever(embedding: FakeEmbedding, qdrant: FakeQdrant) -> HybridRetriever:
    return HybridRetriever(
        embedding_service=embedding,  # type: ignore[arg-type]
        qdrant_client=qdrant,
        collection_name="test_fault_cases",
        api_key="",  # no LLM: dictionary-only rewrite (deterministic)
        embedding_dim=8,
    )


def _make(qdrant: FakeQdrant, embedding: FakeEmbedding | None = None) -> HybridRetriever:
    return HybridRetriever(
        embedding_service=embedding or FakeEmbedding(),  # type: ignore[arg-type]
        qdrant_client=qdrant,
        collection_name="test_fault_cases",
        api_key="",
        embedding_dim=8,
    )


def _cases() -> list[_Point]:
    return [
        _Point("c1", 0.91, {"kind": "station_fault_case", "symptom": "输出电压偏低"}),
        _Point("c2", 0.55, {"kind": "station_fault_case", "symptom": "间歇性通信超时"}),
    ]


# ── The Qdrant branch ─────────────────────────────────────────────────────


class TestVectorBranch:
    async def test_hits_come_back_with_their_payload(
        self, qdrant: FakeQdrant
    ) -> None:
        """The payload must survive — it carries the case text.

        A hit reduced to an id cannot be quoted correctly, so the downstream
        writer invents content. That happened: a citation described
        "capacitor ESR degradation" for a case about fixture contact resistance.
        """
        qdrant._points = _cases()
        results = await _make(qdrant).search("output voltage too low", rerank=False)

        assert [r["id"] for r in results] == ["c1", "c2"]
        assert results[0]["symptom"] == "输出电压偏低"
        assert results[0]["source"] == "qdrant"

    async def test_the_query_vector_reaches_qdrant(
        self, qdrant: FakeQdrant, embedding: FakeEmbedding
    ) -> None:
        qdrant._points = _cases()
        await _make(qdrant, embedding).search("voltage low", rerank=False)

        assert len(qdrant.queries) == 1
        assert qdrant.queries[0]["collection_name"] == "test_fault_cases"
        assert qdrant.queries[0]["query"] == [0.1] * 8

    async def test_top_k_is_respected(self, qdrant: FakeQdrant) -> None:
        qdrant._points = _cases()
        results = await _make(qdrant).search("v", top_k=1, rerank=False)

        assert len(results) == 1
        assert qdrant.queries[0]["limit"] == 1

    async def test_the_query_is_rewritten_before_embedding(
        self, qdrant: FakeQdrant, embedding: FakeEmbedding
    ) -> None:
        """Rewriting happens on the path to the vector, so it must be asserted
        there rather than assumed from the module docstring."""
        qdrant._points = _cases()
        await _make(qdrant, embedding).search("ERR_I2C_TIMEOUT", rerank=False)

        assert embedding.embedded, "查询没有被改写就直接送去嵌入"


class TestErrorCodeIsIgnoredNotRepurposed:
    """It used to seed graph traversal. Anything else would be a silent filter."""

    async def test_the_same_results_with_and_without_an_error_code(
        self, qdrant: FakeQdrant
    ) -> None:
        qdrant._points = _cases()
        r = _make(qdrant)

        without = await r.search("contact intermittent", rerank=False)
        with_code = await r.search(
            "contact intermittent", rerank=False, error_code="ERR_UNRELATED"
        )

        assert [x["id"] for x in without] == [x["id"] for x in with_code]


class TestDegradation:
    async def test_qdrant_failure_returns_empty_rather_than_raising(
        self, qdrant: FakeQdrant
    ) -> None:
        """An empty list is the documented signal for total retrieval failure,
        and it must not take the diagnosis endpoint down with it."""
        qdrant.fail = ConnectionError("qdrant unreachable")

        assert await _make(qdrant).search("anything", rerank=False) == []

    async def test_an_open_circuit_breaker_returns_empty(
        self, qdrant: FakeQdrant
    ) -> None:
        """A tripped breaker means repeated failures already; hammering it would
        turn one outage into a self-inflicted one."""
        r = _make(qdrant)
        r._qdrant_breaker = CircuitBreaker(
            failure_threshold=1, timeout=60.0, name="test"
        )
        r._qdrant_breaker._failure_count = 99  # force the open state
        qdrant.fail = ConnectionError("down")

        assert await r.search("anything", rerank=False) == []
        assert isinstance(CircuitBreakerOpenError("test"), CircuitBreakerOpenError)

    async def test_an_empty_index_returns_empty(self, qdrant: FakeQdrant) -> None:
        """Distinct from failure: the collection simply holds no cases yet.

        ``/diagnose/readiness`` reports this as ``has_history: false`` so an
        empty result is legible as a fact about the line rather than a fault.
        """
        assert await _make(qdrant).search("anything", rerank=False) == []


# ── RRF, kept as the seam ─────────────────────────────────────────────────


class TestReciprocalRankFusionStillBehaves:
    """``hybrid_fusion`` is a pure function over ranked lists and is retained.

    With one list it preserves order; with two it merges them on a shared key.
    Both are asserted, because the second is the reason to keep the module.
    """

    def test_a_single_list_passes_through_in_order(self) -> None:
        hits = [
            {"id": "a", "score": 0.9, "source": "qdrant"},
            {"id": "b", "score": 0.5, "source": "qdrant"},
        ]
        fused = reciprocal_rank_fusion(hits, [])

        assert [r["id"] for r in fused] == ["a", "b"]

    def test_same_entity_id_fuses_across_lists(self) -> None:
        vector = [{"id": "p1", "score": 0.9, "source": "qdrant", "entity_id": "fault:x"}]
        graph = [{"id": "n1", "score": 0.8, "source": "graph", "entity_id": "fault:x"}]

        fused = reciprocal_rank_fusion(vector, graph)

        assert len(fused) == 1, "同一实体应合并为一条"
        assert fused[0]["source"] == "fused"

    def test_distinct_ids_do_not_fuse(self) -> None:
        vector = [{"id": "p1", "score": 0.9, "source": "qdrant", "entity_id": "fault:x"}]
        graph = [{"id": "n1", "score": 0.8, "source": "graph", "entity_id": "fault:y"}]

        assert len(reciprocal_rank_fusion(vector, graph)) == 2

    def test_a_hit_without_a_key_uses_its_point_id(self) -> None:
        """Prevents two unrelated point ids from colliding into one fused entry."""
        vector = [{"id": "p1", "score": 0.9, "source": "qdrant"}]
        graph = [{"id": "p2", "score": 0.8, "source": "graph"}]

        assert len(reciprocal_rank_fusion(vector, graph)) == 2


class TestRemovedApiIsGoneFromTheConstructor:
    def test_no_graph_service_parameter(self, embedding: FakeEmbedding) -> None:
        """``graph_service`` was a required ctor argument.

        Keeping it required would have meant every caller in production passing a
        graph that does not exist — the shape that made the whole branch look
        configured.
        """
        assert "graph_service" not in HybridRetriever.__init__.__code__.co_varnames
        assert "graph_service" not in str(HybridRetriever.__init__.__annotations__)
