"""Tests for KGEvolution — knowledge graph self-evolution.

All tests use mocked Neo4jGraphService and EmbeddingService — no real
Neo4j or OpenAI API calls are made. Tests cover:
- Synonym detection (cosine similarity threshold)
- Entity creation (MERGE Cypher, same schema as KGSeeder)
- Stale edge degradation (weight reduction, floor 0.1)
- process_feedback flow (novel vs. synonym)
- 3x same fault → no duplicates
- Different fault → new nodes created
- POST /api/v1/faults/evolve API endpoint
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ate_cloud.api.v1.faults import _get_embedding_service, _get_graph_service
from ate_cloud.api.v1.faults import router as faults_router
from ate_cloud.services.embedding_service import EmbeddingService
from ate_cloud.services.kg_evolution import KGEvolution
from ate_cloud.services.neo4j_graph_service import Neo4jGraphService

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_graph_service() -> MagicMock:
    """A MagicMock simulating Neo4jGraphService with async query/write."""
    service = MagicMock(spec=Neo4jGraphService)
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
def kg_evolution(
    mock_graph_service: MagicMock,
    mock_embedding_service: MagicMock,
) -> KGEvolution:
    """Create a KGEvolution with mocked dependencies."""
    return KGEvolution(mock_graph_service, mock_embedding_service)


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
) -> AsyncGenerator[FastAPI, None]:
    """Create a FastAPI app with faults_router and mocked services.

    Overrides both _get_graph_service and _get_embedding_service
    dependencies to return the mocked services.
    """
    from ate_cloud.main import create_app

    app = create_app()
    app.dependency_overrides[_get_graph_service] = lambda: mock_graph_service
    app.dependency_overrides[_get_embedding_service] = lambda: mock_embedding_service
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


# ── Synonym Detection Tests ───────────────────────────────────────────────


class TestCheckSynonym:
    """Tests for KGEvolution._check_synonym method."""

    async def test_no_existing_embeddings_returns_false(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """When no FaultSymptom embeddings exist, synonym check returns False."""
        mock_graph_service.query.return_value = []
        result = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is False

    async def test_identical_embedding_returns_true(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Identical embedding (similarity=1.0) triggers synonym detection."""
        mock_graph_service.query.return_value = [
            {"embedding": [1.0, 0.0, 0.0]},
        ]
        result = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is True

    async def test_high_similarity_returns_true(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Similarity >= 0.85 triggers synonym detection."""
        mock_graph_service.query.return_value = [
            {"embedding": [0.99, 0.01, 0.0]},
        ]
        result = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is True

    async def test_low_similarity_returns_false(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Similarity < 0.85 does not trigger synonym detection."""
        mock_graph_service.query.return_value = [
            {"embedding": [0.0, 1.0, 0.0]},
        ]
        result = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is False

    async def test_max_similarity_across_multiple(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Returns True if ANY existing embedding exceeds threshold."""
        mock_graph_service.query.return_value = [
            {"embedding": [0.0, 1.0, 0.0]},  # low similarity
            {"embedding": [0.99, 0.01, 0.0]},  # high similarity
            {"embedding": [0.0, 0.0, 1.0]},  # low similarity
        ]
        result = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is True

    async def test_all_below_threshold_returns_false(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Returns False when all existing embeddings are below threshold."""
        mock_graph_service.query.return_value = [
            {"embedding": [0.0, 1.0, 0.0]},
            {"embedding": [0.0, 0.0, 1.0]},
        ]
        result = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is False

    async def test_custom_threshold(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Custom threshold is respected."""
        mock_graph_service.query.return_value = [
            {"embedding": [0.8, 0.6, 0.0]},  # similarity ~0.8
        ]
        # Default threshold 0.85 → False
        result_default = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result_default is False
        # Lower threshold 0.7 → True
        result_lower = await kg_evolution._check_synonym([1.0, 0.0, 0.0], threshold=0.7)
        assert result_lower is True

    async def test_skips_null_embeddings(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """Null embeddings in results are skipped gracefully."""
        mock_graph_service.query.return_value = [
            {"embedding": None},
            {"embedding": [1.0, 0.0, 0.0]},
        ]
        result = await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        assert result is True

    async def test_uses_synonym_query_cypher(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
    ) -> None:
        """_check_synonym queries with FaultSymptom embedding pattern."""
        mock_graph_service.query.return_value = []
        await kg_evolution._check_synonym([1.0, 0.0, 0.0])
        call_args = mock_graph_service.query.call_args
        cypher: str = call_args[0][0]
        assert "FaultSymptom" in cypher
        assert "embedding" in cypher


# ── Entity Creation Tests ─────────────────────────────────────────────────


class TestCreateFaultEntities:
    """Tests for KGEvolution._create_fault_entities method."""

    async def test_creates_entities_and_returns_counts(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """_create_fault_entities writes MERGE Cypher and returns counts."""
        feedback_with_embedding = {**feedback, "_embedding": [1.0, 0.0, 0.0]}
        result = await kg_evolution._create_fault_entities(feedback_with_embedding)

        assert result["nodes_created"] == 4
        assert result["edges_created"] == 3
        mock_graph_service.write.assert_called_once()

    async def test_cypher_contains_all_node_types(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """MERGE Cypher creates FaultSymptom, Cause, ErrorCode, Product nodes."""
        feedback_with_embedding = {**feedback, "_embedding": [1.0, 0.0, 0.0]}
        await kg_evolution._create_fault_entities(feedback_with_embedding)

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
        feedback_with_embedding = {**feedback, "_embedding": [1.0, 0.0, 0.0]}
        await kg_evolution._create_fault_entities(feedback_with_embedding)

        cypher: str = mock_graph_service.write.call_args[0][0]
        assert "HAS_CAUSE" in cypher
        assert "TRIGGERS_ERROR_CODE" in cypher
        assert "OCCURS_IN_PRODUCT" in cypher

    async def test_cypher_uses_merge_not_create(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Cypher uses MERGE (idempotent), not raw CREATE."""
        feedback_with_embedding = {**feedback, "_embedding": [1.0, 0.0, 0.0]}
        await kg_evolution._create_fault_entities(feedback_with_embedding)

        cypher: str = mock_graph_service.write.call_args[0][0]
        assert "MERGE" in cypher
        # Should not use bare CREATE (ON CREATE SET is OK)
        lines = cypher.strip().split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("CREATE") and "ON CREATE" not in stripped:
                pytest.fail(f"Found bare CREATE (not MERGE): {stripped}")

    async def test_cypher_stores_embedding(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Cypher stores the embedding on the FaultSymptom node."""
        embedding = [1.0, 0.0, 0.0]
        feedback_with_embedding = {**feedback, "_embedding": embedding}
        await kg_evolution._create_fault_entities(feedback_with_embedding)

        cypher: str = mock_graph_service.write.call_args[0][0]
        params: dict[str, Any] = mock_graph_service.write.call_args[0][1]
        assert "s.embedding = $embedding" in cypher
        assert params["embedding"] == embedding

    async def test_cypher_sets_weight_and_last_accessed(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """HAS_CAUSE edge gets weight=1.0 on create and last_accessed."""
        feedback_with_embedding = {**feedback, "_embedding": [1.0, 0.0, 0.0]}
        await kg_evolution._create_fault_entities(feedback_with_embedding)

        cypher: str = mock_graph_service.write.call_args[0][0]
        assert "r.weight = 1.0" in cypher
        assert "last_accessed" in cypher
        assert "timestamp()" in cypher

    async def test_embeds_on_the_fly_if_no_embedding(
        self,
        kg_evolution: KGEvolution,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """_create_fault_entities embeds if _embedding key is missing."""
        mock_embedding_service.embed.return_value = [0.5, 0.5, 0.0]
        # No _embedding in feedback — should call embed
        await kg_evolution._create_fault_entities(feedback)
        mock_embedding_service.embed.assert_called_once_with(feedback["fault_symptom"])

    async def test_passes_correct_params(
        self, kg_evolution: KGEvolution, mock_graph_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Write params contain all feedback fields and embedding."""
        embedding = [1.0, 0.0, 0.0]
        feedback_with_embedding = {**feedback, "_embedding": embedding}
        await kg_evolution._create_fault_entities(feedback_with_embedding)

        params: dict[str, Any] = mock_graph_service.write.call_args[0][1]
        assert params["fault_symptom"] == feedback["fault_symptom"]
        assert params["root_cause"] == feedback["root_cause"]
        assert params["error_code"] == feedback["error_code"]
        assert params["product_type"] == feedback["product_type"]
        assert params["embedding"] == embedding


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
        # No existing embeddings → not a synonym
        mock_graph_service.query.return_value = []
        mock_graph_service.write.return_value = [{"degraded": 0}]

        result = await kg_evolution.process_feedback(feedback)

        assert result["action"] == "created"
        assert result["nodes_created"] == 4
        assert result["edges_created"] == 3

    async def test_synonym_fault_skips_creation(
        self,
        kg_evolution: KGEvolution,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Synonym detected → action='skipped', nodes/edges = 0."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        # Existing embedding identical to incoming → similarity = 1.0
        mock_graph_service.query.return_value = [{"embedding": [1.0, 0.0, 0.0]}]
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
        mock_graph_service.query.return_value = []
        mock_graph_service.write.return_value = [{"degraded": 0}]

        await kg_evolution.process_feedback(feedback)

        mock_embedding_service.embed.assert_called_once_with(feedback["fault_symptom"])

    async def test_calls_check_synonym(
        self,
        kg_evolution: KGEvolution,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """process_feedback calls _check_synonym with the embedding."""
        mock_embedding_service.embed.return_value = [0.5, 0.5, 0.0]
        mock_graph_service.query.return_value = []
        mock_graph_service.write.return_value = [{"degraded": 0}]

        await kg_evolution.process_feedback(feedback)

        # query is called once for synonym check
        mock_graph_service.query.assert_called_once()

    async def test_calls_degrade_on_novel(
        self,
        kg_evolution: KGEvolution,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """process_feedback calls _degrade_stale_edges on novel fault."""
        mock_graph_service.query.return_value = []
        mock_graph_service.write.return_value = [{"degraded": 2}]

        await kg_evolution.process_feedback(feedback)

        # write called twice: once for create, once for degrade
        assert mock_graph_service.write.call_count == 2

    async def test_calls_degrade_on_synonym(
        self,
        kg_evolution: KGEvolution,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """process_feedback calls _degrade_stale_edges even when skipping."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        mock_graph_service.query.return_value = [{"embedding": [1.0, 0.0, 0.0]}]
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
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Submit same fault 3 times: first creates, second/third skip."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]

        # First: no existing embeddings → create
        # Second: existing embedding (identical) → synonym → skip
        # Third: existing embedding (identical) → synonym → skip
        mock_graph_service.query.side_effect = [
            [],  # 1st check: no embeddings
            [{"embedding": [1.0, 0.0, 0.0]}],  # 2nd check: synonym found
            [{"embedding": [1.0, 0.0, 0.0]}],  # 3rd check: synonym found
        ]
        mock_graph_service.write.return_value = [{"degraded": 0}]

        # First submission — novel fault
        result1 = await kg_evolution.process_feedback(feedback)
        assert result1["action"] == "created"
        assert result1["nodes_created"] == 4
        assert result1["edges_created"] == 3

        # Second submission — synonym detected
        result2 = await kg_evolution.process_feedback(feedback)
        assert result2["action"] == "skipped"
        assert result2["nodes_created"] == 0
        assert result2["edges_created"] == 0

        # Third submission — synonym detected
        result3 = await kg_evolution.process_feedback(feedback)
        assert result3["action"] == "skipped"
        assert result3["nodes_created"] == 0
        assert result3["edges_created"] == 0

        # Query called 3 times (one synonym check per submission)
        assert mock_graph_service.query.call_count == 3

    async def test_only_one_create_write_for_three_submissions(
        self,
        kg_evolution: KGEvolution,
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """Only the first submission triggers a MERGE write (create entities)."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        mock_graph_service.query.side_effect = [
            [],
            [{"embedding": [1.0, 0.0, 0.0]}],
            [{"embedding": [1.0, 0.0, 0.0]}],
        ]
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
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
        different_feedback: dict[str, str],
    ) -> None:
        """Different fault (low similarity) → new entities created."""
        # First fault embedding
        embedding1 = [1.0, 0.0, 0.0]
        # Second fault embedding (orthogonal)
        embedding2 = [0.0, 1.0, 0.0]

        mock_embedding_service.embed.side_effect = [embedding1, embedding2]

        # 1st check: no existing → create
        # 2nd check: existing but orthogonal → not synonym → create
        mock_graph_service.query.side_effect = [
            [],
            [{"embedding": embedding1}],
        ]
        mock_graph_service.write.return_value = [{"degraded": 0}]

        # First fault — novel
        result1 = await kg_evolution.process_feedback(feedback)
        assert result1["action"] == "created"
        assert result1["nodes_created"] == 4

        # Second fault — also novel (different embedding)
        result2 = await kg_evolution.process_feedback(different_feedback)
        assert result2["action"] == "created"
        assert result2["nodes_created"] == 4

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
        mock_graph_service.query.side_effect = [
            [],
            [{"embedding": [1.0, 0.0, 0.0]}],
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
        mock_graph_service.query.return_value = []
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
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
        feedback: dict[str, str],
    ) -> None:
        """_degrade_stale_edges runs even when entity creation is skipped."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        mock_graph_service.query.return_value = [{"embedding": [1.0, 0.0, 0.0]}]
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
        mock_graph_service.query.return_value = []
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
        mock_graph_service: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        """POST /faults/evolve returns action='skipped' for synonym."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        mock_graph_service.query.return_value = [{"embedding": [1.0, 0.0, 0.0]}]
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
        """POST /faults/evolve returns 503 when circuit breaker is open."""
        from ate_platform.common.circuit_breaker import CircuitBreakerOpenError

        mock_graph_service.query.side_effect = CircuitBreakerOpenError("circuit open")

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

    async def test_evolve_returns_502_on_neo4j_error(
        self,
        app_with_evolve: FastAPI,
        mock_graph_service: MagicMock,
    ) -> None:
        """POST /faults/evolve returns 502 on Neo4j operation failure."""
        mock_graph_service.query.side_effect = RuntimeError("Neo4j connection lost")

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
        """3x same fault via API: first creates, rest skip (no duplicates)."""
        mock_embedding_service.embed.return_value = [1.0, 0.0, 0.0]
        mock_graph_service.query.side_effect = [
            [],
            [{"embedding": [1.0, 0.0, 0.0]}],
            [{"embedding": [1.0, 0.0, 0.0]}],
        ]
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
