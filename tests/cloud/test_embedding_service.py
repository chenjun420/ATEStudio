"""Test EmbeddingService — OpenAI text-embedding-3-small via LangChain OpenAIEmbeddings.

Mock-based tests always run (no API key required). Real API tests are skipped
unless ``OPENAI_API_KEY`` is set and ``ATE_DEV_MODE=true``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ate_cloud.services.embedding_service import EmbeddingService
from ate_platform.common.circuit_breaker import CircuitBreakerOpenError, CircuitState

# Real API tests require both an API key and dev mode
_HAS_API_KEY = bool(os.environ.get("OPENAI_API_KEY"))
_DEV_MODE = os.environ.get("ATE_DEV_MODE", "").lower() in ("true", "1", "yes")
_REAL_API = _HAS_API_KEY and _DEV_MODE

real_api = pytest.mark.skipif(not _REAL_API, reason="Requires OPENAI_API_KEY + ATE_DEV_MODE=true")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_openai_embeddings() -> MagicMock:
    """Patch langchain_openai.OpenAIEmbeddings with a controllable mock.

    The mock's ``aembed_query`` and ``aembed_documents`` are AsyncMocks so
    callers can ``await`` them. Returns the mock instance (not the patcher);
    the patch is active for the test duration.
    """
    instance = MagicMock()
    instance.aembed_query = AsyncMock(return_value=[0.1] * 1536)
    instance.aembed_documents = AsyncMock(return_value=[[0.1] * 1536, [0.2] * 1536])
    with patch("langchain_openai.OpenAIEmbeddings", return_value=instance) as patched:
        patched._mock_instance = instance  # type: ignore[attr-defined]
        yield instance


@pytest.fixture
def embedding_service(mock_openai_embeddings: MagicMock) -> EmbeddingService:
    """Create an EmbeddingService with a mocked OpenAIEmbeddings backend."""
    return EmbeddingService(
        api_key="test-key",
        model="text-embedding-3-small",
        dimensions=1536,
    )


# ---------------------------------------------------------------------------
# Tests: basic embed (mocked)
# ---------------------------------------------------------------------------


class TestEmbed:
    """Tests for single-text embedding via EmbeddingService.embed()."""

    @pytest.mark.asyncio
    async def test_embed_returns_float_list(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """embed() returns a list of floats of the configured dimension."""
        mock_openai_embeddings.aembed_query.return_value = [0.5] * 1536
        result = await embedding_service.embed("VISA timeout on DMM_CH1")
        assert isinstance(result, list)
        assert len(result) == 1536
        assert all(isinstance(v, float) for v in result)

    @pytest.mark.asyncio
    async def test_embed_calls_aembed_query(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """embed() delegates to OpenAIEmbeddings.aembed_query."""
        await embedding_service.embed("test text")
        mock_openai_embeddings.aembed_query.assert_called_once_with("test text")

    @pytest.mark.asyncio
    async def test_embed_empty_text_returns_zeros(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """Empty text returns zero vector without calling the API."""
        result = await embedding_service.embed("")
        assert result == [0.0] * 1536
        mock_openai_embeddings.aembed_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_embed_whitespace_text_returns_zeros(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """Whitespace-only text returns zero vector without calling the API."""
        result = await embedding_service.embed("   \n\t  ")
        assert result == [0.0] * 1536
        mock_openai_embeddings.aembed_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_embed_not_hash_pattern(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """Real embedding vectors are NOT hash-based (which only produce ±1.0)."""
        # A real embedding has varied float values, not just +1.0/-1.0
        mock_openai_embeddings.aembed_query.return_value = [
            0.023, -0.041, 0.087, 0.012, -0.099, 0.055, 0.001, -0.073,
        ] + [0.01 * (i % 7) for i in range(1528)]
        result = await embedding_service.embed("some failure text")
        # Hash stub produces only ±1.0; real vectors have continuous values
        unique_vals = set(result)
        assert len(unique_vals) > 2, "Embedding should have more than 2 unique values (not hash ±1.0)"
        assert not all(v in (1.0, -1.0, 0.0) for v in result), "Should not be hash-based ±1.0 pattern"


# ---------------------------------------------------------------------------
# Tests: batch embed (mocked)
# ---------------------------------------------------------------------------


class TestEmbedBatch:
    """Tests for batch embedding via EmbeddingService.embed_batch()."""

    @pytest.mark.asyncio
    async def test_batch_returns_vector_list(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """embed_batch() returns one vector per input text."""
        mock_openai_embeddings.aembed_documents.return_value = [[0.1] * 1536, [0.2] * 1536]
        result = await embedding_service.embed_batch(["text one", "text two"])
        assert len(result) == 2
        assert len(result[0]) == 1536
        assert len(result[1]) == 1536

    @pytest.mark.asyncio
    async def test_batch_calls_aembed_documents(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """embed_batch() delegates to OpenAIEmbeddings.aembed_documents."""
        mock_openai_embeddings.aembed_documents.return_value = [[0.1] * 1536]
        await embedding_service.embed_batch(["single"])
        mock_openai_embeddings.aembed_documents.assert_called_once_with(["single"])

    @pytest.mark.asyncio
    async def test_batch_empty_list_returns_empty(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """Empty input list returns empty list without API call."""
        result = await embedding_service.embed_batch([])
        assert result == []
        mock_openai_embeddings.aembed_documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_all_empty_strings_returns_zeros(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """All-empty input returns zero vectors without API call."""
        result = await embedding_service.embed_batch(["", "  ", "\n"])
        assert len(result) == 3
        assert all(v == [0.0] * 1536 for v in result)
        mock_openai_embeddings.aembed_documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_embed_many_alias(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """embed_many() is an alias for embed_batch()."""
        mock_openai_embeddings.aembed_documents.return_value = [[0.1] * 1536]
        result = await embedding_service.embed_many(["alias test"])
        assert len(result) == 1
        assert len(result[0]) == 1536


# ---------------------------------------------------------------------------
# Tests: CircuitBreaker integration
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    """Tests for CircuitBreaker resilience on API failures."""

    @pytest.mark.asyncio
    async def test_breaker_starts_closed(
        self, embedding_service: EmbeddingService
    ) -> None:
        """CircuitBreaker starts in CLOSED state."""
        assert embedding_service.circuit_breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_breaker_opens_after_threshold(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """Breaker opens after 5 consecutive failures."""
        mock_openai_embeddings.aembed_query.side_effect = RuntimeError("API rate limit")
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await embedding_service.embed("text")
        assert embedding_service.circuit_breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_breaker_rejects_when_open(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """When OPEN, embed() raises CircuitBreakerOpenError without API call."""
        mock_openai_embeddings.aembed_query.side_effect = RuntimeError("API error")
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await embedding_service.embed("text")
        # Now breaker is OPEN — next call should be rejected
        with pytest.raises(CircuitBreakerOpenError):
            await embedding_service.embed("text")
        # API should NOT be called when breaker is open
        assert mock_openai_embeddings.aembed_query.call_count == 5

    @pytest.mark.asyncio
    async def test_breaker_resets_on_success_after_timeout(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """After timeout, a successful probe call closes the breaker."""
        # Force the breaker to OPEN by triggering 5 failures
        mock_openai_embeddings.aembed_query.side_effect = RuntimeError("API error")
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await embedding_service.embed("text")
        assert embedding_service.circuit_breaker.state == CircuitState.OPEN

        # Simulate timeout elapsed — manually reset to HALF_OPEN via internal state
        breaker = embedding_service.circuit_breaker
        breaker._state = CircuitState.HALF_OPEN  # type: ignore[attr-defined]
        breaker._failure_count = 0  # type: ignore[attr-defined]

        # Next successful call should close the breaker
        mock_openai_embeddings.aembed_query.side_effect = None
        mock_openai_embeddings.aembed_query.return_value = [0.1] * 1536
        result = await embedding_service.embed("recovery text")
        assert embedding_service.circuit_breaker.state == CircuitState.CLOSED
        assert len(result) == 1536

    @pytest.mark.asyncio
    async def test_breaker_success_resets_failure_count(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """A successful call resets the failure count in CLOSED state."""
        # 3 failures (below threshold of 5)
        mock_openai_embeddings.aembed_query.side_effect = RuntimeError("transient")
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await embedding_service.embed("text")
        assert embedding_service.circuit_breaker.failure_count == 3
        assert embedding_service.circuit_breaker.state == CircuitState.CLOSED

        # Success resets count
        mock_openai_embeddings.aembed_query.side_effect = None
        mock_openai_embeddings.aembed_query.return_value = [0.1] * 1536
        await embedding_service.embed("recovery")
        assert embedding_service.circuit_breaker.failure_count == 0


# ---------------------------------------------------------------------------
# Tests: properties
# ---------------------------------------------------------------------------


class TestProperties:
    """Tests for EmbeddingService properties."""

    def test_dimensions_property(self, embedding_service: EmbeddingService) -> None:
        """dimensions property returns configured dimensionality."""
        assert embedding_service.dimensions == 1536

    def test_model_name_property(self, embedding_service: EmbeddingService) -> None:
        """model_name property returns configured model."""
        assert embedding_service.model_name == "text-embedding-3-small"

    def test_circuit_breaker_property(self, embedding_service: EmbeddingService) -> None:
        """circuit_breaker property exposes the breaker instance."""
        from ate_platform.common.circuit_breaker import CircuitBreaker

        assert isinstance(embedding_service.circuit_breaker, CircuitBreaker)


# ---------------------------------------------------------------------------
# Tests: hash-stub replacement verification
# ---------------------------------------------------------------------------


class TestHashStubReplacement:
    """Verify the embedding service produces vectors distinct from the old hash stub."""

    @staticmethod
    def _hash_stub_vector(text: str, dim: int = 1536) -> list[float]:
        """Reproduce the old hash-based stub for comparison."""
        seed = hashlib.sha256(text.encode()).digest()
        vec = [0.0] * dim
        for i in range(min(dim, len(seed) * 8)):
            byte_idx = i // 8
            bit_idx = i % 8
            vec[i] = 1.0 if (seed[byte_idx] >> bit_idx) & 1 else -1.0
        return vec

    @pytest.mark.asyncio
    async def test_real_vector_differs_from_hash(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """Service embedding is NOT equal to the old hash-stub vector."""
        text = "VISA timeout on instrument DMM_CH1"
        mock_openai_embeddings.aembed_query.return_value = [0.023 * (i % 10) for i in range(1536)]
        real_vec = await embedding_service.embed(text)
        hash_vec = self._hash_stub_vector(text)
        assert real_vec != hash_vec, "Real embedding must differ from hash stub"

    @pytest.mark.asyncio
    async def test_real_vector_has_continuous_values(
        self, embedding_service: EmbeddingService, mock_openai_embeddings: MagicMock
    ) -> None:
        """Real vectors have continuous float values, hash stub only has ±1.0."""
        mock_openai_embeddings.aembed_query.return_value = [0.001 * (i + 1) for i in range(1536)]
        real_vec = await embedding_service.embed("test")
        # Hash stub values are only in {1.0, -1.0, 0.0}
        hash_like = all(v in (1.0, -1.0, 0.0) for v in real_vec)
        assert not hash_like, "Vector should have continuous values, not just ±1.0/0.0"


# ---------------------------------------------------------------------------
# Tests: real OpenAI API (skipped without API key + dev mode)
# ---------------------------------------------------------------------------


class TestRealAPI:
    """Real OpenAI API integration tests.

    These only run when ``OPENAI_API_KEY`` is set AND ``ATE_DEV_MODE=true``.
    They make actual API calls to verify the end-to-end embedding pipeline.
    """

    @real_api
    @pytest.mark.asyncio
    async def test_real_embed_returns_1536_dim(self) -> None:
        """Real API call returns a 1536-dimensional float vector."""
        service = EmbeddingService(
            api_key=os.environ["OPENAI_API_KEY"],
            model="text-embedding-3-small",
            dimensions=1536,
        )
        vec = await service.embed("VISA timeout on instrument DMM_CH1")
        assert len(vec) == 1536
        assert all(isinstance(v, float) for v in vec)
        # Real embeddings have non-zero norm
        norm = sum(v * v for v in vec) ** 0.5
        assert norm > 0.01, "Real embedding should have non-trivial norm"

    @real_api
    @pytest.mark.asyncio
    async def test_real_embed_batch(self) -> None:
        """Real batch API call returns correct number of vectors."""
        service = EmbeddingService(
            api_key=os.environ["OPENAI_API_KEY"],
            model="text-embedding-3-small",
            dimensions=1536,
        )
        texts = ["power supply failure", "I2C communication error", "capacitor degradation"]
        vectors = await service.embed_batch(texts)
        assert len(vectors) == 3
        for v in vectors:
            assert len(v) == 1536

    @real_api
    @pytest.mark.asyncio
    async def test_real_vector_not_hash(self) -> None:
        """Real API vector differs from hash stub and has continuous values."""
        service = EmbeddingService(
            api_key=os.environ["OPENAI_API_KEY"],
            model="text-embedding-3-small",
            dimensions=1536,
        )
        text = "RF calibration failure: VISA timeout"
        real_vec = await service.embed(text)

        # Must differ from hash stub
        seed = hashlib.sha256(text.encode()).digest()
        hash_vec = [0.0] * 1536
        for i in range(min(1536, len(seed) * 8)):
            byte_idx = i // 8
            bit_idx = i % 8
            hash_vec[i] = 1.0 if (seed[byte_idx] >> bit_idx) & 1 else -1.0
        assert real_vec != hash_vec

        # Must have continuous values (not just ±1.0)
        assert not all(v in (1.0, -1.0, 0.0) for v in real_vec)

    @real_api
    @pytest.mark.asyncio
    async def test_real_semantic_similarity(self) -> None:
        """Semantically similar texts produce closer vectors than dissimilar ones."""
        service = EmbeddingService(
            api_key=os.environ["OPENAI_API_KEY"],
            model="text-embedding-3-small",
            dimensions=1536,
        )
        vecs = await service.embed_batch([
            "VISA timeout on instrument DMM_CH1",
            "GPIB communication timeout on multimeter",
            "The weather is sunny today",
        ])
        # Cosine similarity (vectors are normalized by OpenAI, but compute anyway)
        def cosine(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b, strict=True))
            na = sum(x * x for x in a) ** 0.5
            nb = sum(y * y for y in b) ** 0.5
            return dot / (na * nb) if na > 0 and nb > 0 else 0.0

        sim_related = cosine(vecs[0], vecs[1])
        sim_unrelated = cosine(vecs[0], vecs[2])
        assert sim_related > sim_unrelated, "Related failures should be more similar than weather text"


# ---------------------------------------------------------------------------
# Tests: FailureIndexer integration with EmbeddingService
# ---------------------------------------------------------------------------


class TestFailureIndexerIntegration:
    """Verify FailureIndexer uses EmbeddingService to store real vectors."""

    @pytest.mark.asyncio
    async def test_index_failure_stores_real_vector(
        self, mock_openai_embeddings: MagicMock
    ) -> None:
        """index_failure() stores a real embedding vector in Qdrant (not hash)."""
        from qdrant_client.http.models import PointStruct

        from ate_cloud.services.failure_indexer import FailureIndexer
        from shared.events import Event, EventType

        mock_qdrant = MagicMock()
        mock_qdrant.get_collections.return_value = MagicMock(collections=[])
        upserted: list[PointStruct] = []
        mock_qdrant.upsert.side_effect = lambda **kw: upserted.extend(kw.get("points", []))

        # Real-like embedding (continuous values, 1536-dim)
        mock_openai_embeddings.aembed_query.return_value = [0.001 * (i % 100) for i in range(1536)]

        service = EmbeddingService(api_key="test", model="text-embedding-3-small", dimensions=1536)
        indexer = FailureIndexer(
            qdrant_client=mock_qdrant,
            embedding_service=service,
            embedding_dim=1536,
        )

        event = Event(
            type=EventType.STEP_FAILED,
            data={
                "step_id": "rf_cal",
                "failed_step_name": "RF Calibration",
                "error_message": "VISA timeout on DMM_CH1",
                "run_id": "run-001",
            },
        )
        indexer.index_failure(event)
        # Allow background task to complete
        await asyncio.sleep(0.2)

        assert len(upserted) == 1
        vector = upserted[0].vector
        assert len(vector) == 1536
        # Verify it's NOT a hash pattern (hash only has ±1.0)
        assert not all(v in (1.0, -1.0, 0.0) for v in vector), "Stored vector must be real, not hash"

    @pytest.mark.asyncio
    async def test_search_uses_embedding_service(
        self, mock_openai_embeddings: MagicMock
    ) -> None:
        """search_similar_failures embeds query via EmbeddingService."""
        from ate_cloud.services.failure_indexer import FailureIndexer

        mock_qdrant = MagicMock()
        mock_qdrant.search.return_value = []
        mock_openai_embeddings.aembed_query.return_value = [0.5] * 1536

        service = EmbeddingService(api_key="test", model="text-embedding-3-small", dimensions=1536)
        indexer = FailureIndexer(
            qdrant_client=mock_qdrant,
            embedding_service=service,
            embedding_dim=1536,
        )

        results = await indexer.search_similar_failures("VISA timeout")
        assert results == []
        mock_openai_embeddings.aembed_query.assert_called_once_with("VISA timeout")
        # Qdrant search must receive the real vector
        search_kwargs = mock_qdrant.search.call_args[1]
        assert search_kwargs["query_vector"] == [0.5] * 1536
