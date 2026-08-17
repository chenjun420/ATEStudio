"""Tests for HybridRetriever — Qdrant + Neo4j RRF fusion with query rewriting.

All tests use mocked Qdrant client, Neo4j graph service, and OpenAI LLM.
No real service calls are made — tests run without API keys or running services.

Test coverage:
  - Query rewriting: dictionary expansion, LLM augmentation, fallback on breaker open
  - Qdrant search: vector similarity, breaker protection
  - Neo4j search: relationship reasoning, keyword extraction, breaker protection
  - RRF fusion: known rank lists, deduplication, score computation
  - Re-ranking: semantic similarity, fallback on embedding failure
  - End-to-end search: parallel retrieval, both sources present
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ate_cloud.services.embedding_service import EmbeddingService
from ate_cloud.services.hybrid_retriever import HybridRetriever
from ate_platform.common.circuit_breaker import CircuitBreakerOpenError, CircuitState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_qdrant_client() -> MagicMock:
    """Mock Qdrant client with configurable search results.

    Returns MagicMock whose ``search`` returns a list of mock ScoredPoint
    objects with ``id``, ``score``, and ``payload`` attributes.
    """
    client = MagicMock()

    def make_point(
        point_id: str,
        score: float,
        payload: dict[str, Any] | None = None,
    ) -> MagicMock:
        point = MagicMock()
        point.id = point_id
        point.score = score
        point.payload = payload or {}
        return point

    client._make_point = make_point  # type: ignore[attr-defined]
    client.search.return_value = []
    return client


@pytest.fixture
def mock_embedding_service() -> MagicMock:
    """Mock EmbeddingService with async embed/embed_batch.

    Returns a MagicMock (spec=EmbeddingService) whose ``embed`` and
    ``embed_batch`` are AsyncMocks returning configurable vectors.
    """
    service = MagicMock(spec=EmbeddingService)
    service.embed = AsyncMock(return_value=[0.1] * 1536)
    service.embed_batch = AsyncMock(return_value=[[0.1] * 1536])
    service.dimensions = 1536
    return service


@pytest.fixture
def mock_neo4j_service() -> MagicMock:
    """Mock Neo4jGraphService with async query.

    Returns a MagicMock whose ``query`` is an AsyncMock returning
    configurable Cypher results.
    """
    from ate_cloud.services.neo4j_graph_service import Neo4jGraphService
    from ate_platform.common.circuit_breaker import CircuitBreaker

    service = MagicMock(spec=Neo4jGraphService)
    service.query = AsyncMock(return_value=[])
    # CircuitBreaker mock for property access
    breaker = CircuitBreaker(failure_threshold=5, timeout=30.0, name="mock-neo4j")
    service.circuit_breaker = breaker
    return service


@pytest.fixture
def retriever(
    mock_qdrant_client: MagicMock,
    mock_embedding_service: MagicMock,
    mock_neo4j_service: MagicMock,
) -> HybridRetriever:
    """Create a HybridRetriever with all dependencies mocked."""
    return HybridRetriever(
        embedding_service=mock_embedding_service,  # type: ignore[arg-type]
        neo4j_service=mock_neo4j_service,  # type: ignore[arg-type]
        qdrant_client=mock_qdrant_client,
        collection_name="test_fault_cases",
        api_key="test-api-key",
        embedding_dim=1536,
    )


# ---------------------------------------------------------------------------
# Tests: Query Rewriting (Golden-Retriever pattern)
# ---------------------------------------------------------------------------


class TestQueryRewriting:
    """Tests for _rewrite_query — dictionary expansion + LLM augmentation."""

    async def test_dictionary_expands_known_jargon(
        self, retriever: HybridRetriever
    ) -> None:
        """Known domain abbreviations are expanded via the dictionary."""
        # No API key to skip LLM — test dictionary-only path
        retriever._api_key = ""  # type: ignore[attr-defined]
        result = await retriever._rewrite_query("I2C bus failure on PCB")
        assert "I2C" in result
        assert "Inter-Integrated Circuit" in result
        assert "PCB" in result
        assert "Printed Circuit Board" in result

    async def test_dictionary_no_jargon_returns_original(
        self, retriever: HybridRetriever
    ) -> None:
        """Query without domain jargon returns original text (no expansion)."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        result = await retriever._rewrite_query("power supply voltage drop")
        assert result == "power supply voltage drop"

    async def test_dictionary_case_insensitive_match(
        self, retriever: HybridRetriever
    ) -> None:
        """Dictionary matching is case-insensitive (i2c matches I2C)."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        result = await retriever._rewrite_query("i2c communication error")
        assert "Inter-Integrated Circuit" in result

    async def test_dictionary_word_boundary_no_substring(
        self, retriever: HybridRetriever
    ) -> None:
        """Dictionary matching respects word boundaries (no substring matches).

        'SPI' should NOT match inside 'SPIDER' or 'ASPIRIN'.
        """
        retriever._api_key = ""  # type: ignore[attr-defined]
        result = await retriever._rewrite_query("SPIDER web detection")
        # SPIDER is not in the dictionary and SPI should not match as substring
        assert "Serial Peripheral Interface" not in result

    async def test_llm_augmentation_called_with_api_key(
        self, retriever: HybridRetriever
    ) -> None:
        """LLM is called for augmentation when API key is present."""
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content="augmented query"))
        mock_prompt = MagicMock()
        mock_prompt.format_messages.return_value = [MagicMock()]

        retriever._llm = mock_llm
        retriever._prompt = mock_prompt
        retriever._initialized = True

        result = await retriever._rewrite_query("SPI clock error")
        assert result == "augmented query"
        mock_llm.ainvoke.assert_called_once()

    async def test_llm_fallback_on_breaker_open(
        self, retriever: HybridRetriever
    ) -> None:
        """When LLM breaker is open, dictionary-expanded query is returned."""
        # Force breaker open
        breaker = retriever.llm_circuit_breaker
        for _ in range(5):
            await breaker._on_failure()  # type: ignore[attr-defined]
        assert breaker.state == CircuitState.OPEN

        # Even with API key, should fall back to dictionary
        retriever._api_key = "test-key"  # type: ignore[attr-defined]
        result = await retriever._rewrite_query("I2C timeout")
        assert "Inter-Integrated Circuit" in result
        assert "I2C" in result

    async def test_llm_fallback_on_api_error(
        self, retriever: HybridRetriever
    ) -> None:
        """When LLM API raises an error, dictionary-expanded query is returned."""
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("API timeout"))
        mock_prompt = MagicMock()
        mock_prompt.format_messages.return_value = [MagicMock()]

        retriever._llm = mock_llm
        retriever._prompt = mock_prompt
        retriever._initialized = True

        result = await retriever._rewrite_query("BGA solder failure")
        assert "Ball Grid Array" in result
        assert "BGA" in result

    async def test_llm_empty_response_falls_back(
        self, retriever: HybridRetriever
    ) -> None:
        """Empty LLM response falls back to dictionary-expanded query."""
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content="  "))
        mock_prompt = MagicMock()
        mock_prompt.format_messages.return_value = [MagicMock()]

        retriever._llm = mock_llm
        retriever._prompt = mock_prompt
        retriever._initialized = True

        result = await retriever._rewrite_query("ESD damage on PCB")
        assert "Electrostatic Discharge" in result
        assert "Printed Circuit Board" in result

    async def test_multiple_jargon_terms_expanded(
        self, retriever: HybridRetriever
    ) -> None:
        """Multiple domain terms in one query are all expanded."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        result = await retriever._rewrite_query("SPI communication with ADC on PCB failed")
        assert "Serial Peripheral Interface" in result
        assert "Analog-to-Digital Converter" in result
        assert "Printed Circuit Board" in result

    async def test_lookup_domain_terms_via_retriever(
        self, retriever: HybridRetriever
    ) -> None:
        """_lookup_domain_terms correctly identifies domain terms."""
        terms = retriever._lookup_domain_terms("UART and BER test failure")
        assert len(terms) == 2
        abbreviations = {abbr for abbr, _ in terms}
        assert "UART" in abbreviations
        assert "BER" in abbreviations
        # Verify expansions
        expansions = dict(terms)
        assert "Universal Asynchronous" in expansions["UART"]
        assert "Bit Error Rate" in expansions["BER"]


# ---------------------------------------------------------------------------
# Tests: Qdrant Semantic Search
# ---------------------------------------------------------------------------


class TestQdrantSearch:
    """Tests for _search_qdrant — vector similarity search."""

    async def test_returns_results_with_source_tag(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
    ) -> None:
        """Qdrant results include source='qdrant' and payload fields."""
        mock_qdrant_client.search.return_value = [
            mock_qdrant_client._make_point(  # type: ignore[attr-defined]
                "point-1",
                0.95,
                {"failed_step_name": "RF Calibration", "error_message": "VISA timeout"},
            ),
        ]

        results = await retriever._search_qdrant([0.1] * 1536, top_k=5)

        assert len(results) == 1
        assert results[0]["source"] == "qdrant"
        assert results[0]["id"] == "point-1"
        assert results[0]["score"] == 0.95
        assert results[0]["failed_step_name"] == "RF Calibration"
        assert results[0]["error_message"] == "VISA timeout"

    async def test_empty_results_returned(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
    ) -> None:
        """Empty Qdrant results produce an empty list."""
        mock_qdrant_client.search.return_value = []
        results = await retriever._search_qdrant([0.1] * 1536, top_k=5)
        assert results == []

    async def test_qdrant_receives_correct_params(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
    ) -> None:
        """Qdrant search is called with collection name, vector, and limit."""
        mock_qdrant_client.search.return_value = []
        await retriever._search_qdrant([0.5] * 1536, top_k=10)

        mock_qdrant_client.search.assert_called_once_with(
            collection_name="test_fault_cases",
            query_vector=[0.5] * 1536,
            limit=10,
            with_payload=True,
        )

    async def test_qdrant_breaker_opens_after_failures(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
    ) -> None:
        """Qdrant circuit breaker opens after 5 consecutive failures."""
        mock_qdrant_client.search.side_effect = RuntimeError("Qdrant unreachable")
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await retriever._search_qdrant([0.1] * 1536, top_k=5)
        assert retriever.qdrant_circuit_breaker.state == CircuitState.OPEN

    async def test_qdrant_breaker_rejects_when_open(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
    ) -> None:
        """When breaker is open, Qdrant search raises CircuitBreakerOpenError."""
        mock_qdrant_client.search.side_effect = RuntimeError("Qdrant error")
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await retriever._search_qdrant([0.1] * 1536, top_k=5)
        with pytest.raises(CircuitBreakerOpenError):
            await retriever._search_qdrant([0.1] * 1536, top_k=5)

    async def test_multiple_results_preserve_order(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
    ) -> None:
        """Multiple Qdrant results preserve their rank order (by score desc)."""
        mock_qdrant_client.search.return_value = [
            mock_qdrant_client._make_point("p1", 0.9, {"error_message": "first"}),  # type: ignore[attr-defined]
            mock_qdrant_client._make_point("p2", 0.7, {"error_message": "second"}),  # type: ignore[attr-defined]
            mock_qdrant_client._make_point("p3", 0.5, {"error_message": "third"}),  # type: ignore[attr-defined]
        ]
        results = await retriever._search_qdrant([0.1] * 1536, top_k=3)
        assert len(results) == 3
        assert results[0]["score"] > results[1]["score"] > results[2]["score"]


# ---------------------------------------------------------------------------
# Tests: Neo4j Relationship Search
# ---------------------------------------------------------------------------


class TestNeo4jSearch:
    """Tests for _search_neo4j — FMEA graph relationship reasoning."""

    async def test_returns_results_with_relationship_paths(
        self,
        retriever: HybridRetriever,
        mock_neo4j_service: MagicMock,
    ) -> None:
        """Neo4j results include symptom, cause, solution, component fields."""
        mock_neo4j_service.query.return_value = [
            {
                "symptom": "I2C bus failure",
                "cause": "Pull-up resistor too large",
                "solution": "Reduce pull-up to 4.7k",
                "component": "I2C bus",
            },
        ]

        results = await retriever._search_neo4j("I2C bus failure", top_k=5)

        assert len(results) == 1
        assert results[0]["source"] == "neo4j"
        assert results[0]["symptom"] == "I2C bus failure"
        assert results[0]["cause"] == "Pull-up resistor too large"
        assert results[0]["solution"] == "Reduce pull-up to 4.7k"
        assert results[0]["component"] == "I2C bus"
        assert results[0]["score"] == 0.0  # Neo4j has no similarity score

    async def test_empty_results_returned(
        self,
        retriever: HybridRetriever,
        mock_neo4j_service: MagicMock,
    ) -> None:
        """Empty Neo4j results produce an empty list."""
        mock_neo4j_service.query.return_value = []
        results = await retriever._search_neo4j("nonexistent fault", top_k=5)
        assert results == []

    async def test_cypher_uses_has_cause_relationship(
        self,
        retriever: HybridRetriever,
        mock_neo4j_service: MagicMock,
    ) -> None:
        """The Cypher query traverses the HAS_CAUSE relationship."""
        mock_neo4j_service.query.return_value = []
        await retriever._search_neo4j("I2C error", top_k=5)

        call_args = mock_neo4j_service.query.call_args
        cypher: str = call_args[0][0]
        assert "HAS_CAUSE" in cypher
        assert "FaultSymptom" in cypher
        assert "Cause" in cypher
        assert "CONTAINS" in cypher

    async def test_keyword_extracted_from_query(
        self,
        retriever: HybridRetriever,
        mock_neo4j_service: MagicMock,
    ) -> None:
        """The longest non-stopword token is passed as the Cypher keyword."""
        mock_neo4j_service.query.return_value = []
        await retriever._search_neo4j("calibration failure on oscilloscope", top_k=5)

        call_args = mock_neo4j_service.query.call_args
        params: dict[str, Any] = call_args[0][1]
        # "oscilloscope" is the longest non-stopword token
        assert params["keyword"] == "oscilloscope"
        assert params["limit"] == 5

    async def test_multiple_results_get_unique_ids(
        self,
        retriever: HybridRetriever,
        mock_neo4j_service: MagicMock,
    ) -> None:
        """Multiple Neo4j results get unique IDs (neo4j-0, neo4j-1, ...)."""
        mock_neo4j_service.query.return_value = [
            {"symptom": "fault A", "cause": "cause A", "solution": "", "component": ""},
            {"symptom": "fault B", "cause": "cause B", "solution": "", "component": ""},
        ]
        results = await retriever._search_neo4j("fault", top_k=5)
        assert len(results) == 2
        assert results[0]["id"] == "neo4j-0"
        assert results[1]["id"] == "neo4j-1"

    async def test_neo4j_breaker_propagates_from_service(
        self,
        retriever: HybridRetriever,
        mock_neo4j_service: MagicMock,
    ) -> None:
        """Neo4j search failure propagates the error (breaker is in Neo4jGraphService)."""
        mock_neo4j_service.query.side_effect = RuntimeError("Neo4j connection lost")
        with pytest.raises(RuntimeError, match="Neo4j connection lost"):
            await retriever._search_neo4j("I2C error", top_k=5)

    async def test_optional_match_fields_present(
        self,
        retriever: HybridRetriever,
        mock_neo4j_service: MagicMock,
    ) -> None:
        """Cypher includes OPTIONAL MATCH for solution and component."""
        mock_neo4j_service.query.return_value = []
        await retriever._search_neo4j("test", top_k=5)

        cypher: str = mock_neo4j_service.query.call_args[0][0]
        assert "OPTIONAL MATCH" in cypher
        assert "HAS_SOLUTION" in cypher
        assert "AFFECTS_COMPONENT" in cypher


# ---------------------------------------------------------------------------
# Tests: Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


class TestRRFFusion:
    """Tests for _reciprocal_rank_fusion — RRF formula and deduplication."""

    def test_rrf_score_formula(
        self, retriever: HybridRetriever
    ) -> None:
        """RRF score = sum(1/(k + rank)) for each list the document appears in."""
        k = 60
        results_a = [
            {"id": "a1", "symptom": "fault one", "score": 0.9, "source": "qdrant"},
            {"id": "a2", "symptom": "fault two", "score": 0.8, "source": "qdrant"},
        ]
        results_b = [
            {"id": "b1", "symptom": "fault one", "cause": "cause one", "source": "neo4j"},
            {"id": "b2", "symptom": "fault three", "cause": "cause three", "source": "neo4j"},
        ]

        fused = retriever._reciprocal_rank_fusion(results_a, results_b, k=k)

        # "fault one" appears at rank 1 in both lists:
        #   score = 1/(60+1) + 1/(60+1) = 2/61
        # "fault two" appears at rank 2 in list A only:
        #   score = 1/(60+2) = 1/62
        # "fault three" appears at rank 2 in list B only:
        #   score = 1/(60+2) = 1/62
        assert len(fused) == 3

        # Fused entry for "fault one" should have highest score
        fault_one = next(r for r in fused if "one" in r.get("symptom", ""))
        assert abs(fault_one["rrf_score"] - (2.0 / 61.0)) < 1e-10
        assert fault_one["source"] == "fused"
        assert fault_one["qdrant_score"] == 0.9
        assert fault_one["cause"] == "cause one"

        # fault two and fault three should have equal scores
        fault_two = next(r for r in fused if "two" in r.get("symptom", ""))
        fault_three = next(r for r in fused if "three" in r.get("symptom", ""))
        assert abs(fault_two["rrf_score"] - (1.0 / 62.0)) < 1e-10
        assert abs(fault_three["rrf_score"] - (1.0 / 62.0)) < 1e-10

    def test_rrf_sorted_descending(
        self, retriever: HybridRetriever
    ) -> None:
        """Fused results are sorted by RRF score descending."""
        results_a = [
            {"id": "a1", "symptom": "doc1", "score": 0.9, "source": "qdrant"},
            {"id": "a2", "symptom": "doc2", "score": 0.8, "source": "qdrant"},
            {"id": "a3", "symptom": "doc3", "score": 0.7, "source": "qdrant"},
        ]
        results_b: list[dict[str, Any]] = []

        fused = retriever._reciprocal_rank_fusion(results_a, results_b, k=60)

        assert len(fused) == 3
        assert fused[0]["rrf_score"] > fused[1]["rrf_score"] > fused[2]["rrf_score"]

    def test_rrf_empty_inputs(
        self, retriever: HybridRetriever
    ) -> None:
        """Empty input lists produce an empty fused result."""
        assert retriever._reciprocal_rank_fusion([], [], k=60) == []

    def test_rrf_single_list(
        self, retriever: HybridRetriever
    ) -> None:
        """RRF with one empty list returns the other list with RRF scores."""
        results_a = [
            {"id": "a1", "symptom": "doc1", "score": 0.9, "source": "qdrant"},
            {"id": "a2", "symptom": "doc2", "score": 0.8, "source": "qdrant"},
        ]
        fused = retriever._reciprocal_rank_fusion(results_a, [], k=60)
        assert len(fused) == 2
        assert fused[0]["rrf_score"] == 1.0 / 61.0  # rank 1
        assert fused[1]["rrf_score"] == 1.0 / 62.0  # rank 2

    def test_rrf_deduplication_merges_fields(
        self, retriever: HybridRetriever
    ) -> None:
        """Documents with matching symptom text are merged into one fused entry."""
        results_a = [
            {
                "id": "qdrant-1",
                "symptom": "I2C bus failure",
                "score": 0.95,
                "source": "qdrant",
                "error_message": "I2C bus failure on channel 1",
            },
        ]
        results_b = [
            {
                "id": "neo4j-0",
                "symptom": "I2C bus failure",
                "cause": "Pull-up too large",
                "solution": "Reduce to 4.7k",
                "component": "I2C bus",
                "source": "neo4j",
            },
        ]

        fused = retriever._reciprocal_rank_fusion(results_a, results_b, k=60)

        assert len(fused) == 1
        entry = fused[0]
        assert entry["source"] == "fused"
        assert entry["qdrant_score"] == 0.95
        assert entry["cause"] == "Pull-up too large"
        assert entry["solution"] == "Reduce to 4.7k"
        assert entry["component"] == "I2C bus"
        assert entry["error_message"] == "I2C bus failure on channel 1"
        # RRF score = 1/61 + 1/61 = 2/61
        assert abs(entry["rrf_score"] - (2.0 / 61.0)) < 1e-10

    def test_rrf_k_parameter_affects_scores(
        self, retriever: HybridRetriever
    ) -> None:
        """Different k values produce different RRF scores."""
        results_a = [{"id": "a1", "symptom": "doc1", "score": 0.9, "source": "qdrant"}]
        results_b = [{"id": "b1", "symptom": "doc1", "cause": "c1", "source": "neo4j"}]

        fused_k60 = retriever._reciprocal_rank_fusion(results_a, results_b, k=60)
        fused_k1 = retriever._reciprocal_rank_fusion(results_a, results_b, k=1)

        # With k=60: score = 2/61 ≈ 0.0328
        # With k=1:  score = 2/2  = 1.0
        assert abs(fused_k60[0]["rrf_score"] - (2.0 / 61.0)) < 1e-10
        assert abs(fused_k1[0]["rrf_score"] - 1.0) < 1e-10

    def test_rrf_no_dedup_when_symptoms_differ(
        self, retriever: HybridRetriever
    ) -> None:
        """Results with different symptoms are not merged (remain separate)."""
        results_a = [{"id": "a1", "symptom": "fault alpha", "score": 0.9, "source": "qdrant"}]
        results_b = [{"id": "b1", "symptom": "fault beta", "cause": "c1", "source": "neo4j"}]

        fused = retriever._reciprocal_rank_fusion(results_a, results_b, k=60)
        assert len(fused) == 2

    def test_match_key_uses_symptom_first(
        self, retriever: HybridRetriever
    ) -> None:
        """_match_key prioritizes symptom field for deduplication."""
        result = {"symptom": "I2C failure", "error_message": "other", "id": "x"}
        key = retriever._match_key(result)
        assert key == "i2c failure"  # lowercased, first 50 chars

    def test_match_key_falls_back_to_error_message(
        self, retriever: HybridRetriever
    ) -> None:
        """_match_key falls back to error_message when symptom is absent."""
        result = {"error_message": "VISA timeout", "id": "x"}
        key = retriever._match_key(result)
        assert key == "visa timeout"

    def test_match_key_falls_back_to_id(
        self, retriever: HybridRetriever
    ) -> None:
        """_match_key falls back to id when no text fields are present."""
        result = {"id": "unique-123"}
        key = retriever._match_key(result)
        assert key == "unique-123"


# ---------------------------------------------------------------------------
# Tests: Re-ranking
# ---------------------------------------------------------------------------


class TestRerank:
    """Tests for _rerank — semantic similarity re-ranking."""

    async def test_rerank_adds_rerank_score(
        self,
        retriever: HybridRetriever,
        mock_embedding_service: MagicMock,
    ) -> None:
        """Re-ranking adds a rerank_score field to each result."""
        results = [
            {"id": "r1", "symptom": "fault A", "rrf_score": 0.03, "source": "qdrant"},
            {"id": "r2", "symptom": "fault B", "rrf_score": 0.02, "source": "neo4j"},
        ]
        # Mock embeddings: query close to r2, far from r1
        mock_embedding_service.embed.return_value = [1.0] * 4
        mock_embedding_service.embed_batch.return_value = [
            [0.0] * 4,  # r1: dissimilar
            [1.0] * 4,  # r2: similar
        ]

        reranked = await retriever._rerank(results, "query")

        assert len(reranked) == 2
        assert "rerank_score" in reranked[0]
        assert "rerank_score" in reranked[1]
        # r2 should be ranked first (higher similarity)
        assert reranked[0]["symptom"] == "fault B"
        assert reranked[1]["symptom"] == "fault A"

    async def test_rerank_empty_results(
        self, retriever: HybridRetriever
    ) -> None:
        """Re-ranking empty results returns empty list."""
        result = await retriever._rerank([], "query")
        assert result == []

    async def test_rerank_fallback_on_embedding_failure(
        self,
        retriever: HybridRetriever,
        mock_embedding_service: MagicMock,
    ) -> None:
        """When embedding service fails, results are returned unchanged."""
        results = [
            {"id": "r1", "symptom": "fault A", "rrf_score": 0.03, "source": "qdrant"},
        ]
        mock_embedding_service.embed.side_effect = RuntimeError("Embedding API down")

        reranked = await retriever._rerank(results, "query")
        assert len(reranked) == 1
        assert "rerank_score" not in reranked[0]

    async def test_rerank_fallback_on_breaker_open(
        self,
        retriever: HybridRetriever,
        mock_embedding_service: MagicMock,
    ) -> None:
        """When embedding breaker is open, results are returned unchanged."""
        results = [
            {"id": "r1", "symptom": "fault A", "rrf_score": 0.03, "source": "qdrant"},
        ]
        mock_embedding_service.embed.side_effect = CircuitBreakerOpenError("breaker open")

        reranked = await retriever._rerank(results, "query")
        assert len(reranked) == 1
        assert "rerank_score" not in reranked[0]

    async def test_rerank_preserves_rrf_as_tiebreaker(
        self,
        retriever: HybridRetriever,
        mock_embedding_service: MagicMock,
    ) -> None:
        """Equal rerank_score uses rrf_score as tiebreaker."""
        results = [
            {"id": "r1", "symptom": "fault A", "rrf_score": 0.01, "source": "qdrant"},
            {"id": "r2", "symptom": "fault B", "rrf_score": 0.05, "source": "neo4j"},
        ]
        # Equal embeddings -> equal similarity
        mock_embedding_service.embed.return_value = [1.0] * 4
        mock_embedding_service.embed_batch.return_value = [
            [0.5] * 4,
            [0.5] * 4,
        ]

        reranked = await retriever._rerank(results, "query")
        # Same similarity, so r2 (higher rrf_score) should be first
        assert reranked[0]["symptom"] == "fault B"
        assert reranked[1]["symptom"] == "fault A"

    def test_cosine_similarity_identical_vectors(
        self, retriever: HybridRetriever
    ) -> None:
        """Cosine similarity of identical vectors is 1.0."""
        vec = [1.0, 0.5, 0.3, 0.8]
        sim = retriever._cosine_similarity(vec, vec)
        assert abs(sim - 1.0) < 1e-10

    def test_cosine_similarity_orthogonal_vectors(
        self, retriever: HybridRetriever
    ) -> None:
        """Cosine similarity of orthogonal vectors is 0.0."""
        sim = retriever._cosine_similarity([1.0, 0.0], [0.0, 1.0])
        assert abs(sim - 0.0) < 1e-10

    def test_cosine_similarity_zero_vector(
        self, retriever: HybridRetriever
    ) -> None:
        """Cosine similarity with a zero vector is 0.0."""
        sim = retriever._cosine_similarity([0.0, 0.0], [1.0, 1.0])
        assert sim == 0.0

    def test_cosine_similarity_empty_vectors(
        self, retriever: HybridRetriever
    ) -> None:
        """Cosine similarity of empty vectors is 0.0."""
        assert retriever._cosine_similarity([], []) == 0.0

    def test_result_text_concatenates_fields(
        self, retriever: HybridRetriever
    ) -> None:
        """_result_text concatenates available text fields."""
        result = {
            "symptom": "I2C failure",
            "cause": "pull-up too large",
            "solution": "reduce resistor",
        }
        text = retriever._result_text(result)
        assert "I2C failure" in text
        assert "pull-up too large" in text
        assert "reduce resistor" in text

    def test_result_text_empty_result(
        self, retriever: HybridRetriever
    ) -> None:
        """_result_text returns id when no text fields are present."""
        result = {"id": "test-123"}
        text = retriever._result_text(result)
        assert text == "test-123"


# ---------------------------------------------------------------------------
# Tests: Keyword Extraction
# ---------------------------------------------------------------------------


class TestKeywordExtraction:
    """Tests for _extract_keyword — Neo4j search keyword extraction."""

    def test_extracts_longest_non_stopword(
        self, retriever: HybridRetriever
    ) -> None:
        """Extracts the longest non-stopword token from the query."""
        keyword = retriever._extract_keyword("the calibration oscilloscope failure")
        assert keyword == "oscilloscope"

    def test_empty_query_returns_empty(
        self, retriever: HybridRetriever
    ) -> None:
        """Empty query returns empty string."""
        assert retriever._extract_keyword("") == ""
        assert retriever._extract_keyword("   ") == ""

    def test_all_stopwords_returns_first_token(
        self, retriever: HybridRetriever
    ) -> None:
        """When all tokens are stop words, returns first token lowercased."""
        keyword = retriever._extract_keyword("the failure error")
        # "failure" and "error" are in stop words, "the" is a stop word
        # Falls back to first token
        assert keyword == "the"

    def test_preserves_alphanumeric_tokens(
        self, retriever: HybridRetriever
    ) -> None:
        """Tokens with mixed alphanumeric characters are preserved."""
        keyword = retriever._extract_keyword("I2C bus voltage3 drop")
        # "voltage3" is longer than "bus"
        assert keyword == "voltage3"


# ---------------------------------------------------------------------------
# Tests: End-to-End Search
# ---------------------------------------------------------------------------


class TestEndToEndSearch:
    """Tests for the full search() pipeline — parallel retrieval + RRF + rerank."""

    async def test_search_returns_fused_results_from_both_sources(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
        mock_neo4j_service: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        """search() returns results from both Qdrant and Neo4j."""
        # Disable LLM to use dictionary-only path
        retriever._api_key = ""  # type: ignore[attr-defined]

        # Mock Qdrant results
        mock_qdrant_client.search.return_value = [
            mock_qdrant_client._make_point(  # type: ignore[attr-defined]
                "q1", 0.9, {"symptom": "I2C bus failure", "error_message": "I2C bus failure on ch1"}
            ),
        ]
        # Mock Neo4j results
        mock_neo4j_service.query.return_value = [
            {
                "symptom": "SPI clock error",
                "cause": "CPOL mismatch",
                "solution": "Fix clock polarity",
                "component": "SPI bus",
            },
        ]
        # Mock embeddings for rerank
        mock_embedding_service.embed.return_value = [0.5] * 4
        mock_embedding_service.embed_batch.return_value = [[0.5] * 4, [0.5] * 4]

        results = await retriever.search("I2C bus failure", top_k=10, rerank=True)

        assert len(results) == 2
        sources = {r["source"] for r in results}
        assert "qdrant" in sources or "fused" in sources
        assert "neo4j" in sources or "fused" in sources

    async def test_search_with_fused_entry_merges_both_sources(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
        mock_neo4j_service: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        """When Qdrant and Neo4j match the same symptom, result is fused."""
        retriever._api_key = ""  # type: ignore[attr-defined]

        mock_qdrant_client.search.return_value = [
            mock_qdrant_client._make_point(  # type: ignore[attr-defined]
                "q1", 0.95, {"symptom": "I2C bus failure", "error_message": "I2C timeout"}
            ),
        ]
        mock_neo4j_service.query.return_value = [
            {
                "symptom": "I2C bus failure",
                "cause": "Pull-up too large",
                "solution": "Reduce to 4.7k",
                "component": "I2C bus",
            },
        ]
        mock_embedding_service.embed.return_value = [0.5] * 4
        mock_embedding_service.embed_batch.return_value = [[0.5] * 4]

        results = await retriever.search("I2C bus failure", top_k=10, rerank=True)

        assert len(results) == 1
        entry = results[0]
        assert entry["source"] == "fused"
        assert entry["qdrant_score"] == 0.95
        assert entry["cause"] == "Pull-up too large"
        assert entry["solution"] == "Reduce to 4.7k"
        assert entry["component"] == "I2C bus"

    async def test_search_qdrant_failure_returns_neo4j_only(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
        mock_neo4j_service: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        """When Qdrant fails, Neo4j results are still returned."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        mock_qdrant_client.search.side_effect = RuntimeError("Qdrant down")
        mock_neo4j_service.query.return_value = [
            {
                "symptom": "I2C failure",
                "cause": "pull-up issue",
                "solution": "fix resistor",
                "component": "I2C",
            },
        ]
        mock_embedding_service.embed.return_value = [0.5] * 4
        mock_embedding_service.embed_batch.return_value = [[0.5] * 4]

        results = await retriever.search("I2C failure", top_k=10, rerank=True)

        assert len(results) == 1
        assert results[0]["source"] == "neo4j"
        assert results[0]["symptom"] == "I2C failure"

    async def test_search_neo4j_failure_returns_qdrant_only(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
        mock_neo4j_service: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        """When Neo4j fails, Qdrant results are still returned."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        mock_qdrant_client.search.return_value = [
            mock_qdrant_client._make_point("q1", 0.9, {"symptom": "fault A"}),  # type: ignore[attr-defined]
        ]
        mock_neo4j_service.query.side_effect = RuntimeError("Neo4j down")
        mock_embedding_service.embed.return_value = [0.5] * 4
        mock_embedding_service.embed_batch.return_value = [[0.5] * 4]

        results = await retriever.search("fault A", top_k=10, rerank=True)

        assert len(results) == 1
        assert results[0]["source"] == "qdrant"

    async def test_search_both_fail_returns_empty(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
        mock_neo4j_service: MagicMock,
    ) -> None:
        """When both Qdrant and Neo4j fail, search returns empty list."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        mock_qdrant_client.search.side_effect = RuntimeError("Qdrant down")
        mock_neo4j_service.query.side_effect = RuntimeError("Neo4j down")

        results = await retriever.search("query", top_k=10, rerank=False)
        assert results == []

    async def test_search_rerank_disabled(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
        mock_neo4j_service: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        """When rerank=False, embedding service is not called for reranking."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        mock_qdrant_client.search.return_value = [
            mock_qdrant_client._make_point("q1", 0.9, {"symptom": "fault A"}),  # type: ignore[attr-defined]
        ]
        mock_neo4j_service.query.return_value = []

        results = await retriever.search("fault A", top_k=10, rerank=False)

        assert len(results) == 1
        # embed_batch should not be called (rerank skipped)
        mock_embedding_service.embed_batch.assert_not_called()

    async def test_search_top_k_limits_results(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
        mock_neo4j_service: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        """search() respects top_k limit on final results."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        mock_qdrant_client.search.return_value = [
            mock_qdrant_client._make_point(f"q{i}", 0.9 - i * 0.1, {"symptom": f"fault {i}"})  # type: ignore[attr-defined]
            for i in range(5)
        ]
        mock_neo4j_service.query.return_value = [
            {"symptom": f"neo fault {i}", "cause": f"cause {i}", "solution": "", "component": ""}
            for i in range(5)
        ]
        mock_embedding_service.embed.return_value = [0.5] * 4
        mock_embedding_service.embed_batch.return_value = [[0.5] * 4] * 10

        results = await retriever.search("fault", top_k=3, rerank=True)
        assert len(results) == 3

    async def test_search_query_rewriting_applied(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
        mock_neo4j_service: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        """search() applies query rewriting before embedding."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        mock_qdrant_client.search.return_value = []
        mock_neo4j_service.query.return_value = []

        await retriever.search("I2C bus error on PCB", top_k=5, rerank=False)

        # The embedding service should receive the rewritten query
        # (original + dictionary expansion), not the raw query
        embed_call_arg = mock_embedding_service.embed.call_args[0][0]
        assert "I2C" in embed_call_arg
        assert "Inter-Integrated Circuit" in embed_call_arg

    async def test_search_with_llm_rewriting(
        self,
        retriever: HybridRetriever,
        mock_qdrant_client: MagicMock,
        mock_neo4j_service: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        """search() uses LLM-rewritten query when LLM is available."""
        # Set up LLM mock
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content="rewritten I2C query"))
        mock_prompt = MagicMock()
        mock_prompt.format_messages.return_value = [MagicMock()]

        retriever._llm = mock_llm
        retriever._prompt = mock_prompt
        retriever._initialized = True
        retriever._api_key = "test-key"  # type: ignore[attr-defined]

        mock_qdrant_client.search.return_value = []
        mock_neo4j_service.query.return_value = []

        await retriever.search("I2C error", top_k=5, rerank=False)

        # LLM should have been called
        mock_llm.ainvoke.assert_called_once()
        # Embedding should use the LLM-rewritten query
        embed_call_arg = mock_embedding_service.embed.call_args[0][0]
        assert "rewritten I2C query" in embed_call_arg or embed_call_arg == "rewritten I2C query"


# ---------------------------------------------------------------------------
# Tests: Circuit Breaker Properties
# ---------------------------------------------------------------------------


class TestCircuitBreakerProperties:
    """Tests for CircuitBreaker exposure and state."""

    def test_qdrant_breaker_starts_closed(
        self, retriever: HybridRetriever
    ) -> None:
        """Qdrant circuit breaker starts in CLOSED state."""
        assert retriever.qdrant_circuit_breaker.state == CircuitState.CLOSED
        assert retriever.qdrant_circuit_breaker.failure_count == 0

    def test_llm_breaker_starts_closed(
        self, retriever: HybridRetriever
    ) -> None:
        """LLM circuit breaker starts in CLOSED state."""
        assert retriever.llm_circuit_breaker.state == CircuitState.CLOSED
        assert retriever.llm_circuit_breaker.failure_count == 0

    def test_qdrant_breaker_name(
        self, retriever: HybridRetriever
    ) -> None:
        """Qdrant breaker has a descriptive name."""
        assert "qdrant" in retriever.qdrant_circuit_breaker._name.lower()  # type: ignore[attr-defined]

    def test_llm_breaker_name(
        self, retriever: HybridRetriever
    ) -> None:
        """LLM breaker has a descriptive name."""
        assert "llm" in retriever.llm_circuit_breaker._name.lower()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tests: Domain Dictionary Coverage
# ---------------------------------------------------------------------------


class TestDomainDictionary:
    """Tests for the domain dictionary content (Golden-Retriever pattern)."""

    async def test_dictionary_contains_i2c(
        self, retriever: HybridRetriever
    ) -> None:
        """Dictionary includes I2C expansion."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        result = await retriever._rewrite_query("I2C error")
        assert "Inter-Integrated Circuit" in result

    async def test_dictionary_contains_spi(
        self, retriever: HybridRetriever
    ) -> None:
        """Dictionary includes SPI expansion."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        result = await retriever._rewrite_query("SPI clock error")
        assert "Serial Peripheral Interface" in result

    async def test_dictionary_contains_esd(
        self, retriever: HybridRetriever
    ) -> None:
        """Dictionary includes ESD expansion."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        result = await retriever._rewrite_query("ESD damage")
        assert "Electrostatic Discharge" in result

    async def test_dictionary_contains_bga(
        self, retriever: HybridRetriever
    ) -> None:
        """Dictionary includes BGA expansion."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        result = await retriever._rewrite_query("BGA solder joint")
        assert "Ball Grid Array" in result

    async def test_dictionary_contains_ber(
        self, retriever: HybridRetriever
    ) -> None:
        """Dictionary includes BER expansion."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        result = await retriever._rewrite_query("BER above threshold")
        assert "Bit Error Rate" in result

    async def test_dictionary_contains_pcb(
        self, retriever: HybridRetriever
    ) -> None:
        """Dictionary includes PCB expansion."""
        retriever._api_key = ""  # type: ignore[attr-defined]
        result = await retriever._rewrite_query("PCB trace damage")
        assert "Printed Circuit Board" in result

    def test_dictionary_has_30_plus_entries(self) -> None:
        """Domain dictionary has at least 30 entries for electronics testing."""
        from ate_cloud.services.hybrid_retriever import _DOMAIN_DICTIONARY

        assert len(_DOMAIN_DICTIONARY) >= 30
