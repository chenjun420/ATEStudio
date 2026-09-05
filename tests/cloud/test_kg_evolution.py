"""Tests for KGEvolution — knowledge graph self-evolution.

All tests use a mocked GraphService and EmbeddingService plus an in-memory
fake Qdrant client (real COSINE nearest-neighbor, zero external services) —
no FalkorDB, Qdrant server, or OpenAI API calls are made. Tests cover:
- Synonym detection via Qdrant nearest-neighbor (COSINE score threshold)
- Graceful degrade when Qdrant is unavailable (graph entities still written)
- Symptom vector upsert into Qdrant (vectors never written to graph nodes)
- Entity creation (MERGE Cypher, same schema as KGSeeder)
- Stale edge degradation (weight reduction, floor 0.1)
- process_feedback flow (novel vs. synonym)
- 3x same fault → no duplicates
- Different fault → new nodes created
- POST /api/v1/faults/evolve API endpoint
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ate_cloud.api.v1.faults import _get_embedding_service, _get_graph_service
from ate_cloud.api.v1.faults import router as faults_router
from ate_cloud.services.embedding_service import EmbeddingService
from ate_cloud.services.falkordb_graph_service import FalkorDBGraphService
from ate_cloud.services.kg_evolution import KGEvolution

SYMPTOM_COLLECTION = "ate_fault_symptoms"


# ── In-memory fake Qdrant client ──────────────────────────────────────────


class _ScoredPoint:
    """Minimal stand-in for qdrant_client scored search results."""

    def __init__(self, point_id: str, score: float, payload: Any = None) -> None:
        self.id = point_id
        self.score = score
        self.payload = payload


class FakeQdrantClient:
    """In-memory Qdrant client with real COSINE nearest-neighbor search.

    Tracks created collections and upserted points; ``search`` computes
    cosine similarity via KGEvolution._cosine_similarity so synonym
    semantics match production. Set ``raise_on`` to a method name to
    simulate Qdrant being DOWN for that call.
    """

    def __init__(self) -> None:
        self._points: dict[str, list[Any]] = {}
        self.created_collections: list[str] = []
        self.collection_dims: dict[str, int] = {}
        self.search_calls: list[dict[str, Any]] = []
        self.raise_on: str | None = None

    def get_collections(self) -> Any:
        if self.raise_on == "get_collections":
            raise ConnectionError("qdrant down")
        result = MagicMock()
        result.collections = [SimpleNamespace(name=n) for n in self._points]
        return result

    def create_collection(
        self, collection_name: str, vectors_config: Any = None, **_: Any
    ) -> None:
        if self.raise_on == "create_collection":
            raise ConnectionError("qdrant down")
        self._points.setdefault(collection_name, [])
        self.created_collections.append(collection_name)
        if vectors_config is not None:
            self.collection_dims[collection_name] = int(vectors_config.size)

    def upsert(self, collection_name: str, points: list[Any]) -> None:
        if self.raise_on == "upsert":
            raise ConnectionError("qdrant down")
        store = self._points.setdefault(collection_name, [])
        for point in points:
            store[:] = [p for p in store if p.id != point.id]
            store.append(point)

    def search(
        self,
        collection_name: str,
        query_vector: list[float],
        limit: int = 5,
        with_payload: bool = False,  # noqa: FBT001
        **_: Any,
    ) -> list[_ScoredPoint]:
        if self.raise_on == "search":
            raise ConnectionError("qdrant down")
        self.search_calls.append(
            {"collection": collection_name, "vector": query_vector, "limit": limit}
        )
        store = self._points.get(collection_name, [])
        scored = [
            (KGEvolution._cosine_similarity(query_vector, list(p.vector)), p)
            for p in store
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            _ScoredPoint(
                p.id,
                score,
                p.payload if with_payload else None,
            )
            for score, p in scored[:limit]
        ]

    def seed(
        self,
        vector: list[float],
        point_id: str = "seed-1",
        collection: str = SYMPTOM_COLLECTION,
    ) -> None:
        """Seed a symptom vector directly (bypasses upsert failure injection)."""
        self._points.setdefault(collection, []).append(
            SimpleNamespace(id=point_id, vector=vector, payload={})
        )

    def point_count(self, collection: str = SYMPTOM_COLLECTION) -> int:
        return len(self._points.get(collection, []))


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_graph_service() -> MagicMock:
    """A MagicMock simulating the GraphService with async query/write."""
    service = MagicMock(spec=FalkorDBGraphService)
    service.query = AsyncMock(return_value=[])
    service.write = AsyncMock(return_value=[])
    return service


@pytest.fixture
def mock_embedding_service() -> MagicMock:
    """A MagicMock simulating EmbeddingService with async embed."""
    service = MagicMock(spec=EmbeddingService)
    service.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
    return service


@pytest.fixture
def fake_qdrant() -> FakeQdrantClient:
    """In-memory Qdrant client with real COSINE NN search."""
    return FakeQdrantClient()


@pytest.fixture
def kg_evolution(
    mock_graph_service: MagicMock,
    mock_embedding_service: MagicMock,
    fake_qdrant: FakeQdrantClient,
) -> KGEvolution:
    """Create a KGEvolution with mocked graph/embedding + fake Qdrant."""
    return KGEvolution(
        mock_graph_service,
        mock_embedding_service,
        qdrant_client=fake_qdrant,
        embedding_dim=3,
    )


@pytest.fixture
def feedback() -> dict[str, str]:
    """Standard diagnosis feedback dict."""
    return {
        "fault_symptom": "I2C bus communication failure",
        "root_cause": "Pull-up resistor too large",
        "error_code": "I2C_TIMEOUT",
        "product_type": "Communication Module",
    }


@pytest.fixture
def different_feedback() -> dict[str, str]:
    """A different diagnosis feedback (novel fault)."""
    return {
        "fault_symptom": "Power rail short circuit on 3.3V",
        "root_cause": "PCB inner layer short",
        "error_code": "PWR_SHORT",
        "product_type": "Server Board",
    }


@pytest.fixture
async def app_with_evolve(
    mock_graph_service: MagicMock,
    mock_embedding_service: MagicMock,
    fake_qdrant: FakeQdrantClient,
) -> AsyncGenerator[FastAPI, None]:
    """Create a FastAPI app with faults_router and mocked services.

    Overrides both _get_graph_service and _get_embedding_service
    dependencies to return the mocked services, and wires the in-memory
    fake Qdrant client onto app.state (as the lifespan would in prod).
    """
    from ate_cloud.main import create_app

    app = create_app()
    app.dependency_overrides[_get_graph_service] = lambda: mock_graph_service
    app.dependency_overrides[_get_embedding_service] = lambda: mock_embedding_service
    app.state.qdrant_client = fake_qdrant
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def evolve_client(app_with_evolve: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client targeting the faults evolve API."""
    transport = ASGITransport(app=app_with_evolve)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Cosine Similarity Tests ───────────────────────────────────────────────


class TestCosineSimilarity:
    """Tests for KGEvolution._cosine_similarity static method."""

    def test_identical_vectors(self) -> None:
        """Identical vectors have cosine similarity = 1.0."""
        a = [1.0, 2.0, 3.0]
        b = [1.0, 2.0, 3.0]
        assert KGEvolution._cosine_similarity(a, b) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        """Orthogonal vectors have cosine similarity = 0.0."""
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert KGEvolution._cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self) -> None:
        """Opposite vectors have cosine similarity = -1.0."""
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert KGEvolution._cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_zero_vector_returns_zero(self) -> None:
        """Zero vector returns 0.0 (avoids division by zero)."""
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        assert KGEvolution._cosine_similarity(a, b) == 0.0

    def test_empty_vectors_returns_zero(self) -> None:
        """Empty vectors return 0.0."""
        assert KGEvolution._cosine_similarity([], []) == 0.0

    def test_high_similarity_above_threshold(self) -> None:
        """Vectors with high similarity (>0.85) are near-identical."""
        a = [1.0, 0.5, 0.1]
        b = [0.98, 0.49, 0.15]
        sim = KGEvolution._cosine_similarity(a, b)
        assert sim > 0.85

    def test_low_similarity_below_threshold(self) -> None:
        """Vectors with low similarity (<0.85) are distinct."""
        a = [1.0, 0.0, 0.0]
        b = [0.3, 0.9, 0.2]
        sim = KGEvolution._cosine_similarity(a, b)
        assert sim < 0.85

    def test_different_length_vectors(self) -> None:
        """Vectors of different lengths compare up to the shorter."""
        a = [1.0, 2.0, 3.0]
        b = [1.0, 2.0]
        # Compares first 2 elements: dot=5, |a|=sqrt(5), |b|=sqrt(5)
        assert KGEvolution._cosine_similarity(a, b) == pytest.approx(1.0)


# ── Synonym Detection Tests (Qdrant nearest-neighbor) ─────────────────────


class TestCheckSynonym:
    """Tests for KGEvolution._check_synonym (Qdrant NN backed)."""

    async def test_no_existing_vectors_returns_false(
        self, kg_evolution: KGEvolution, fake_qdrant: FakeQdrantClient,
    ) -> None:
        """Empty symptom collection → synonym check returns False."""
        result = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is False

    async def test_identical_vector_returns_true(
        self, kg_evolution: KGEvolution, fake_qdrant: FakeQdrantClient,
    ) -> None:
        """Identical vector (COSINE=1.0) triggers synonym detection."""
        fake_qdrant.seed([1.0, 0.0, 0.0])
        result = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is True

    async def test_high_similarity_returns_true(
        self, kg_evolution: KGEvolution, fake_qdrant: FakeQdrantClient,
    ) -> None:
        """NN score >= 0.85 triggers synonym detection."""
        fake_qdrant.seed([0.99, 0.01, 0.0])
        result = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is True

    async def test_low_similarity_returns_false(
        self, kg_evolution: KGEvolution, fake_qdrant: FakeQdrantClient,
    ) -> None:
        """NN score < 0.85 does not trigger synonym detection."""
        fake_qdrant.seed([0.0, 1.0, 0.0])
        result = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is False

    async def test_max_similarity_across_multiple(
        self, kg_evolution: KGEvolution, fake_qdrant: FakeQdrantClient,
    ) -> None:
        """Returns True if ANY stored vector exceeds threshold."""
        fake_qdrant.seed([0.0, 1.0, 0.0], point_id="low-1")
        fake_qdrant.seed([0.99, 0.01, 0.0], point_id="high")
        fake_qdrant.seed([0.0, 0.0, 1.0], point_id="low-2")
        result = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is True

    async def test_all_below_threshold_returns_false(
        self, kg_evolution: KGEvolution, fake_qdrant: FakeQdrantClient,
    ) -> None:
        """Returns False when all stored vectors are below threshold."""
        fake_qdrant.seed([0.0, 1.0, 0.0], point_id="a")
        fake_qdrant.seed([0.0, 0.0, 1.0], point_id="b")
        result = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is False

    async def test_custom_threshold(
        self, kg_evolution: KGEvolution, fake_qdrant: FakeQdrantClient,
    ) -> None:
        """Custom threshold is respected."""
        fake_qdrant.seed([0.8, 0.6, 0.0])  # cosine similarity = 0.8
        # Default threshold 0.85 → False
        result_default = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result_default is False
        # Lower threshold 0.7 → True
        result_lower = await kg_evolution._check_synonym([1.0, 0.0, 0.0], threshold=0.7)
        assert result_lower is True

    async def test_searches_symptom_collection(
        self, kg_evolution: KGEvolution, fake_qdrant: FakeQdrantClient,
    ) -> None:
        """_check_synonym runs a Qdrant search against the symptom collection."""
        await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert len(fake_qdrant.search_calls) == 1
        call = fake_qdrant.search_calls[0]
        assert call["collection"] == SYMPTOM_COLLECTION
        assert call["vector"] == [1.0, 0.0, 0.0]
        assert call["limit"] == 1

    async def test_no_graph_query_for_synonym(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Synonym detection never reads embeddings from the graph."""
        await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        mock_graph_service.query.assert_not_called()


# ── Qdrant degrade tests ──────────────────────────────────────────────────


class TestQdrantDegrade:
    """Synonym detection degrades gracefully when Qdrant is unavailable."""

    async def test_no_qdrant_client_returns_false(
        self,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        """With qdrant_client=None, _check_synonym returns False (novel)."""
        evolution = KGEvolution(mock_graph_service, mock_embedding_service)
        result = await evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is False

    async def test_search_error_returns_false(
        self,
        kg_evolution: KGEvolution,
        fake_qdrant: FakeQdrantClient,
    ) -> None:
        """Qdrant search failure → False (dedup degraded, no exception)."""
        fake_qdrant.raise_on = "search"
        result = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is False

    async def test_qdrant_down_graph_entities_still_written(
        self,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Qdrant absent: process_feedback still creates graph entities."""
        evolution = KGEvolution(mock_graph_service, mock_embedding_service)
        result = await evolution.process_feedback(feedback)
        assert result["action"] == "created"
        assert result["nodes_created"] == 4
        # One MERGE write (create) + one degrade write
        assert mock_graph_service.write.call_count == 2
        merge_calls = [
            c for c in mock_graph_service.write.call_args_list
            if "MERGE" in c[0][0]
        ]
        assert len(merge_calls) == 1

    async def test_upsert_error_still_creates_entities(
        self,
        kg_evolution: KGEvolution,
        fake_qdrant: FakeQdrantClient,
        mock_graph_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Vector upsert failure does not block graph entity creation."""
        fake_qdrant.raise_on = "upsert"
        result = await kg_evolution.process_feedback(feedback)
        assert result["action"] == "created"
        assert result["nodes_created"] == 4

    async def test_create_collection_error_still_creates_entities(
        self,
        kg_evolution: KGEvolution,
        fake_qdrant: FakeQdrantClient,
        feedback: dict[str, str],
    ) -> None:
        """Collection creation failure degrades both dedup and indexing."""
        fake_qdrant.raise_on = "create_collection"
        result = await kg_evolution.process_feedback(feedback)
        assert result["action"] == "created"


# ── Entity Creation Tests ─────────────────────────────────────────────────


class TestCreateFaultEntities:
    """Tests for KGEvolution._create_fault_entities method."""

    async def test_creates_entities_and_returns_counts(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """_create_fault_entities writes MERGE Cypher and returns counts."""
        result = await kg_evolution._create_fault_entities(feedback)

        assert result["nodes_created"] == 4
        assert result["edges_created"] == 3
        mock_graph_service.write.assert_called_once()

    async def test_cypher_contains_all_node_types(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """MERGE Cypher creates FaultSymptom, Cause, ErrorCode, Product nodes."""
        await kg_evolution._create_fault_entities(feedback)

        cypher: str = mock_graph_service.write.call_args[0][0]
        assert "FaultSymptom" in cypher
        assert ":Cause" in cypher
        assert "ErrorCode" in cypher
        assert ":Product" in cypher

    async def test_cypher_contains_all_relationships(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """MERGE Cypher creates HAS_CAUSE, TRIGGERS_ERROR_CODE, OCCURS_IN_PRODUCT."""
        await kg_evolution._create_fault_entities(feedback)

        cypher: str = mock_graph_service.write.call_args[0][0]
        assert "HAS_CAUSE" in cypher
        assert "TRIGGERS_ERROR_CODE" in cypher
        assert "OCCURS_IN_PRODUCT" in cypher

    async def test_cypher_uses_merge_not_create(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Cypher uses MERGE (idempotent), not raw CREATE."""
        await kg_evolution._create_fault_entities(feedback)

        cypher: str = mock_graph_service.write.call_args[0][0]
        assert "MERGE" in cypher
        # Should not use bare CREATE (ON CREATE SET is OK)
        lines = cypher.strip().split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("CREATE") and "ON CREATE" not in stripped:
                pytest.fail(f"Found bare CREATE (not MERGE): {stripped}")

    async def test_cypher_does_not_store_embedding(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Graph Cypher never sets an embedding array on a node."""
        await kg_evolution._create_fault_entities(feedback)

        cypher: str = mock_graph_service.write.call_args[0][0]
        params: dict[str, Any] = mock_graph_service.write.call_args[0][1]
        assert "embedding" not in cypher.lower()
        assert "embedding" not in params

    async def test_cypher_sets_weight_and_last_accessed(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """HAS_CAUSE edge gets weight=1.0 on create and last_accessed."""
        await kg_evolution._create_fault_entities(feedback)

        cypher: str = mock_graph_service.write.call_args[0][0]
        assert "r.weight = 1.0" in cypher
        assert "last_accessed" in cypher
        assert "timestamp()" in cypher

    async def test_passes_correct_params(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Write params contain all feedback fields (and no embedding)."""
        await kg_evolution._create_fault_entities(feedback)

        params: dict[str, Any] = mock_graph_service.write.call_args[0][1]
        assert params["fault_symptom"] == feedback["fault_symptom"]
        assert params["root_cause"] == feedback["root_cause"]
        assert params["error_code"] == feedback["error_code"]
        assert params["product_type"] == feedback["product_type"]
        assert "embedding" not in params


# ── Symptom vector indexing (Qdrant) ──────────────────────────────────────


class TestSymptomVectorIndexing:
    """Novel symptoms are upserted to Qdrant; synonyms are not."""

    async def test_novel_fault_upserts_vector(
        self,
        kg_evolution: KGEvolution,
        fake_qdrant: FakeQdrantClient,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Novel fault → exactly one symptom vector upserted to Qdrant."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        await kg_evolution.process_feedback(feedback)

        assert fake_qdrant.point_count() == 1
        assert SYMPTOM_COLLECTION in fake_qdrant.created_collections

    async def test_synonym_does_not_upsert(
        self,
        kg_evolution: KGEvolution,
        fake_qdrant: FakeQdrantClient,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Synonym fault → no new vector upserted."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        fake_qdrant.seed([1.0, 0.0, 0.0])
        await kg_evolution.process_feedback(feedback)

        # Only the pre-seeded point remains (no upsert on skip path).
        assert fake_qdrant.point_count() == 1

    async def test_repeated_same_symptom_upserts_once(
        self,
        kg_evolution: KGEvolution,
        fake_qdrant: FakeQdrantClient,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Deterministic point id: re-indexing the same symptom is an idempotent upsert."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        kg_evolution._vectors.index_symptom(feedback, [1.0, 0.0, 0.0])
        kg_evolution._vectors.index_symptom(feedback, [1.0, 0.0, 0.0])
        assert fake_qdrant.point_count() == 1

    async def test_collection_dimension_follows_settings(
        self,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        fake_qdrant: FakeQdrantClient,
        feedback: dict[str, str],
    ) -> None:
        """Collection VectorParams size comes from settings.embedding_dimensions."""
        from ate_cloud.config import settings

        evolution = KGEvolution(
            mock_graph_service,
            mock_embedding_service,
            qdrant_client=fake_qdrant,
        )
        evolution._vectors.index_symptom(feedback, [1.0, 0.0, 0.0])
        assert fake_qdrant.collection_dims[SYMPTOM_COLLECTION] == settings.embedding_dimensions

    async def test_payload_carries_feedback_fields(
        self,
        kg_evolution: KGEvolution,
        fake_qdrant: FakeQdrantClient,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Upserted point payload carries symptom/cause/error/product fields."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        await kg_evolution.process_feedback(feedback)

        point = fake_qdrant._points[SYMPTOM_COLLECTION][0]
        assert point.payload["fault_symptom"] == feedback["fault_symptom"]
        assert point.payload["root_cause"] == feedback["root_cause"]
        assert point.payload["error_code"] == feedback["error_code"]
        assert point.payload["product_type"] == feedback["product_type"]


# ── Edge Degradation Tests ────────────────────────────────────────────────


class TestDegradeStaleEdges:
    """Tests for KGEvolution._degrade_stale_edges method."""

    async def test_returns_zero_when_no_stale_edges(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Returns 0 when no edges are degraded."""
        mock_graph_service.write.return_value = [{"degraded": 0}]
        result = await kg_evolution._degrade_stale_edges()
        assert result == 0

    async def test_returns_count_of_degraded_edges(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Returns the count of degraded edges from Cypher result."""
        mock_graph_service.write.return_value = [{"degraded": 5}]
        result = await kg_evolution._degrade_stale_edges()
        assert result == 5

    async def test_returns_zero_on_empty_result(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Returns 0 when Cypher returns empty list."""
        mock_graph_service.write.return_value = []
        result = await kg_evolution._degrade_stale_edges()
        assert result == 0

    async def test_cypher_targets_has_cause_edges(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Degradation Cypher targets HAS_CAUSE relationships."""
        mock_graph_service.write.return_value = [{"degraded": 0}]
        await kg_evolution._degrade_stale_edges()

        cypher: str = mock_graph_service.write.call_args[0][0]
        assert "HAS_CAUSE" in cypher

    async def test_cypher_checks_weight_threshold(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Degradation Cypher only targets edges with weight >= 0.2."""
        mock_graph_service.write.return_value = [{"degraded": 0}]
        await kg_evolution._degrade_stale_edges()

        cypher: str = mock_graph_service.write.call_args[0][0]
        params: dict[str, Any] = mock_graph_service.write.call_args[0][1]
        assert "weight" in cypher
        assert "min_weight" in params
        assert params["min_weight"] == 0.2

    async def test_cypher_checks_last_accessed_staleness(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Degradation Cypher checks last_accessed against 30-day threshold."""
        mock_graph_service.write.return_value = [{"degraded": 0}]
        await kg_evolution._degrade_stale_edges()

        cypher: str = mock_graph_service.write.call_args[0][0]
        params: dict[str, Any] = mock_graph_service.write.call_args[0][1]
        assert "last_accessed" in cypher
        assert "stale_threshold" in params
        # 30 days in milliseconds
        assert params["stale_threshold"] == 30 * 24 * 60 * 60 * 1000

    async def test_cypher_reduces_weight_by_decrement(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Degradation Cypher reduces weight by 0.1 (floor maintained by WHERE)."""
        mock_graph_service.write.return_value = [{"degraded": 0}]
        await kg_evolution._degrade_stale_edges()

        cypher: str = mock_graph_service.write.call_args[0][0]
        params: dict[str, Any] = mock_graph_service.write.call_args[0][1]
        assert "r.weight = r.weight - $decrement" in cypher
        assert params["decrement"] == 0.1

    async def test_cypher_does_not_delete_edges(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Degradation Cypher uses SET (not DELETE) — edges are degraded, not removed."""
        mock_graph_service.write.return_value = [{"degraded": 0}]
        await kg_evolution._degrade_stale_edges()

        cypher: str = mock_graph_service.write.call_args[0][0]
        assert "DELETE" not in cypher.upper()
        assert "SET" in cypher


# ── process_feedback Tests ────────────────────────────────────────────────


class TestProcessFeedback:
    """Tests for KGEvolution.process_feedback orchestration."""

    async def test_novel_fault_creates_entities(
        self,
        kg_evolution: KGEvolution,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Novel fault (no synonym) → action='created', nodes/edges > 0."""
        # Empty fake Qdrant → no NN hit → not a synonym
        mock_graph_service.write.return_value = [{"degraded": 0}]

        result = await kg_evolution.process_feedback(feedback)

        assert result["action"] == "created"
        assert result["nodes_created"] == 4
        assert result["edges_created"] == 3

    async def test_synonym_fault_skips_creation(
        self,
        kg_evolution: KGEvolution,
        fake_qdrant: FakeQdrantClient,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Synonym detected via Qdrant NN → action='skipped', nodes/edges = 0."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        # Existing vector identical to incoming → NN score = 1.0
        fake_qdrant.seed([1.0, 0.0, 0.0])
        mock_graph_service.write.return_value = [{"degraded": 0}]

        result = await kg_evolution.process_feedback(feedback)

        assert result["action"] == "skipped"
        assert result["nodes_created"] == 0
        assert result["edges_created"] == 0

    async def test_calls_embed_for_symptom(
        self,
        kg_evolution: KGEvolution,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """process_feedback embeds the fault_symptom text."""
        mock_graph_service.write.return_value = [{"degraded": 0}]

        await kg_evolution.process_feedback(feedback)

        mock_embedding_service.embed.assert_called_once_with(feedback["fault_symptom"])

    async def test_synonym_check_uses_qdrant_not_graph(
        self,
        kg_evolution: KGEvolution,
        fake_qdrant: FakeQdrantClient,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """process_feedback runs synonym NN on Qdrant and never queries graph embeddings."""
        mock_embedding_service.embed.return_value = [0.5, 0.5, 0.0]
        mock_graph_service.write.return_value = [{"degraded": 0}]

        await kg_evolution.process_feedback(feedback)

        assert len(fake_qdrant.search_calls) == 1
        assert fake_qdrant.search_calls[0]["collection"] == SYMPTOM_COLLECTION
        mock_graph_service.query.assert_not_called()

    async def test_calls_degrade_on_novel(
        self,
        kg_evolution: KGEvolution,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """process_feedback calls _degrade_stale_edges on novel fault."""
        mock_graph_service.write.return_value = [{"degraded": 2}]

        await kg_evolution.process_feedback(feedback)

        # write called twice: once for create, once for degrade
        assert mock_graph_service.write.call_count == 2

    async def test_calls_degrade_on_synonym(
        self,
        kg_evolution: KGEvolution,
        fake_qdrant: FakeQdrantClient,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """process_feedback calls _degrade_stale_edges even when skipping."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        fake_qdrant.seed([1.0, 0.0, 0.0])
        mock_graph_service.write.return_value = [{"degraded": 0}]

        await kg_evolution.process_feedback(feedback)

        # write called once: only degrade (no create)
        assert mock_graph_service.write.call_count == 1


# ── 3x Same Fault → No Duplicates ─────────────────────────────────────────


class TestThreeTimesSameFault:
    """Test: submit same fault 3 times → only first creates, rest skipped."""

    async def test_three_submissions_same_fault(
        self,
        kg_evolution: KGEvolution,
        fake_qdrant: FakeQdrantClient,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Submit same fault 3 times: first creates, second/third skip (Qdrant NN)."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        mock_graph_service.write.return_value = [{"degraded": 0}]

        # First submission — novel fault (empty Qdrant)
        result1 = await kg_evolution.process_feedback(feedback)
        assert result1["action"] == "created"
        assert result1["nodes_created"] == 4
        assert result1["edges_created"] == 3

        # Second submission — NN hit on the upserted vector → synonym → skip
        result2 = await kg_evolution.process_feedback(feedback)
        assert result2["action"] == "skipped"
        assert result2["nodes_created"] == 0
        assert result2["edges_created"] == 0

        # Third submission — synonym detected
        result3 = await kg_evolution.process_feedback(feedback)
        assert result3["action"] == "skipped"
        assert result3["nodes_created"] == 0
        assert result3["edges_created"] == 0

        # Exactly one symptom vector stored (deterministic upsert id).
        assert fake_qdrant.point_count() == 1

    async def test_only_one_create_write_for_three_submissions(
        self,
        kg_evolution: KGEvolution,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Only the first submission triggers a MERGE write (create entities)."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        mock_graph_service.write.return_value = [{"degraded": 0}]

        await kg_evolution.process_feedback(feedback)
        await kg_evolution.process_feedback(feedback)
        await kg_evolution.process_feedback(feedback)

        # Write calls: 1st = create + degrade (2), 2nd = degrade (1), 3rd = degrade (1)
        # Total = 4 write calls, but only 1 contains MERGE
        all_calls = mock_graph_service.write.call_args_list
        merge_count = sum(1 for c in all_calls if "MERGE" in c[0][0])
        assert merge_count == 1  # Only first submission creates entities


# ── Different Fault → New Nodes Created ───────────────────────────────────


class TestDifferentFaultCreatesNodes:
    """Test: submit different fault → new nodes created."""

    async def test_different_fault_creates_new_entities(
        self,
        kg_evolution: KGEvolution,
        fake_qdrant: FakeQdrantClient,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
        different_feedback: dict[str, str],
    ) -> None:
        """Different fault (orthogonal vector) → new entities created."""
        # First fault embedding
        embedding1 = [1.0, 0.0, 0.0]
        # Second fault embedding (orthogonal — NN score 0.0)
        embedding2 = [0.0, 1.0, 0.0]

        mock_embedding_service.embed.side_effect = [embedding1, embedding2]
        mock_graph_service.write.return_value = [{"degraded": 0}]

        # First fault — novel
        result1 = await kg_evolution.process_feedback(feedback)
        assert result1["action"] == "created"
        assert result1["nodes_created"] == 4

        # Second fault — also novel (orthogonal vector, below threshold)
        result2 = await kg_evolution.process_feedback(different_feedback)
        assert result2["action"] == "created"
        assert result2["nodes_created"] == 4

        # Two distinct symptom vectors stored.
        assert fake_qdrant.point_count() == 2

    async def test_both_create_calls_have_merge(
        self,
        kg_evolution: KGEvolution,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
        different_feedback: dict[str, str],
    ) -> None:
        """Both different faults trigger MERGE writes."""
        mock_embedding_service.embed.side_effect = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ]
        mock_graph_service.write.return_value = [{"degraded": 0}]

        await kg_evolution.process_feedback(feedback)
        await kg_evolution.process_feedback(different_feedback)

        all_calls = mock_graph_service.write.call_args_list
        merge_count = sum(1 for c in all_calls if "MERGE" in c[0][0])
        assert merge_count == 2  # Both create entities


# ── Edge Degradation in process_feedback ──────────────────────────────────


class TestEdgeDegradationInFlow:
    """Test: edge degradation reduces weight on old edges."""

    async def test_degradation_runs_on_novel_fault(
        self,
        kg_evolution: KGEvolution,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """_degrade_stale_edges is called after entity creation."""
        # First write = create, second write = degrade (returns 3 degraded)
        mock_graph_service.write.side_effect = [
            [],  # create MERGE returns nothing
            [{"degraded": 3}],  # degrade returns count
        ]

        await kg_evolution.process_feedback(feedback)

        # Second write call should be the degrade Cypher
        degrade_call = mock_graph_service.write.call_args_list[1]
        cypher: str = degrade_call[0][0]
        assert "HAS_CAUSE" in cypher
        assert "weight" in cypher

    async def test_degradation_runs_on_synonym_skip(
        self,
        kg_evolution: KGEvolution,
        fake_qdrant: FakeQdrantClient,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """_degrade_stale_edges runs even when entity creation is skipped."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        fake_qdrant.seed([1.0, 0.0, 0.0])
        mock_graph_service.write.return_value = [{"degraded": 5}]

        await kg_evolution.process_feedback(feedback)

        # Only one write call (degrade, no create)
        assert mock_graph_service.write.call_count == 1
        cypher: str = mock_graph_service.write.call_args[0][0]
        assert "HAS_CAUSE" in cypher


# ── API Endpoint Tests ────────────────────────────────────────────────────


class TestEvolveEndpoint:
    """Tests for POST /api/v1/faults/evolve endpoint."""

    async def test_evolve_returns_created_on_novel(
        self,
        evolve_client: AsyncClient,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        """POST /faults/evolve returns action='created' for novel fault."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        mock_graph_service.write.return_value = [{"degraded": 0}]

        response = await evolve_client.post("/api/v1/faults/evolve", json={
            "fault_symptom": "I2C bus failure",
            "root_cause": "Pull-up resistor too large",
            "error_code": "I2C_TIMEOUT",
            "product_type": "Communication Module",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "created"
        assert data["nodes_created"] == 4
        assert data["edges_created"] == 3

    async def test_evolve_returns_skipped_on_synonym(
        self,
        evolve_client: AsyncClient,
        fake_qdrant: FakeQdrantClient,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        """POST /faults/evolve returns action='skipped' for Qdrant NN synonym."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        # Pre-seed the symptom collection with the identical vector.
        fake_qdrant.seed([1.0, 0.0, 0.0])
        mock_graph_service.write.return_value = [{"degraded": 0}]

        response = await evolve_client.post("/api/v1/faults/evolve", json={
            "fault_symptom": "I2C bus failure",
            "root_cause": "Pull-up resistor too large",
            "error_code": "I2C_TIMEOUT",
            "product_type": "Communication Module",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "skipped"
        assert data["nodes_created"] == 0
        assert data["edges_created"] == 0

    async def test_evolve_returns_503_on_circuit_open(
        self,
        app_with_evolve: FastAPI,
        mock_graph_service: MagicMock,
    ) -> None:
        """POST /faults/evolve returns 503 when the graph circuit breaker is open."""
        from ate_platform.common.circuit_breaker import CircuitBreakerOpenError

        # Graph write (entity creation) raises when the circuit is OPEN.
        mock_graph_service.write.side_effect = CircuitBreakerOpenError("circuit open")

        transport = ASGITransport(app=app_with_evolve)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post("/api/v1/faults/evolve", json={
                "fault_symptom": "test",
                "root_cause": "test",
                "error_code": "TEST",
                "product_type": "Test",
            })
            assert response.status_code == 503
            assert "circuit breaker" in response.json()["detail"].lower()

    async def test_evolve_returns_502_on_graph_error(
        self,
        app_with_evolve: FastAPI,
        mock_graph_service: MagicMock,
    ) -> None:
        """POST /faults/evolve returns 502 on a graph operation failure."""
        mock_graph_service.write.side_effect = RuntimeError("FalkorDB connection lost")

        transport = ASGITransport(app=app_with_evolve)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post("/api/v1/faults/evolve", json={
                "fault_symptom": "test",
                "root_cause": "test",
                "error_code": "TEST",
                "product_type": "Test",
            })
            assert response.status_code == 502
            assert "evolve fault graph" in response.json()["detail"].lower()

    async def test_evolve_validates_request_body(
        self,
        evolve_client: AsyncClient,
    ) -> None:
        """POST /faults/evolve returns 422 for missing required fields."""
        response = await evolve_client.post("/api/v1/faults/evolve", json={
            "fault_symptom": "test",
            # Missing root_cause, error_code, product_type
        })
        assert response.status_code == 422

    async def test_evolve_3x_same_fault_via_api(
        self,
        evolve_client: AsyncClient,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        """3x same fault via API: first creates, rest skip via Qdrant NN dedup."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        mock_graph_service.write.return_value = [{"degraded": 0}]

        payload = {
            "fault_symptom": "I2C bus failure",
            "root_cause": "Pull-up too large",
            "error_code": "I2C_TIMEOUT",
            "product_type": "Comm Module",
        }

        r1 = await evolve_client.post("/api/v1/faults/evolve", json=payload)
        assert r1.status_code == 200
        assert r1.json()["action"] == "created"

        r2 = await evolve_client.post("/api/v1/faults/evolve", json=payload)
        assert r2.status_code == 200
        assert r2.json()["action"] == "skipped"

        r3 = await evolve_client.post("/api/v1/faults/evolve", json=payload)
        assert r3.status_code == 200
        assert r3.json()["action"] == "skipped"


# ── Router Registration Tests ─────────────────────────────────────────────


class TestEvolveRouterRegistration:
    """Tests verifying the evolve endpoint is properly registered."""

    def test_faults_router_has_evolve_endpoint(self) -> None:
        """The faults router has a POST /evolve route."""
        routes = {r.path: r for r in faults_router.routes}
        assert "/faults/evolve" in routes
        evolve_route = routes["/faults/evolve"]
        assert "POST" in evolve_route.methods  # type: ignore[union-attr]

    def test_seed_endpoint_still_exists(self) -> None:
        """The existing /seed endpoint is not removed."""
        routes = {r.path: r for r in faults_router.routes}
        assert "/faults/seed" in routes
