"""Embedding Service — OpenAI text-embedding-3-small via LangChain OpenAIEmbeddings.

Wraps the LangChain ``OpenAIEmbeddings`` integration with a CircuitBreaker
for resilience against rate-limit and transient API failures.

All methods are async and return real 1536-dim float vectors. The service is
injected into ``FailureIndexer`` so failure events are embedded with real
semantic vectors instead of the previous hash-based stub.

Per AGENTS.md §7: if the API key is configured but the service is unreachable,
the CircuitBreaker opens and ``CircuitBreakerOpenError`` propagates — no
silent degradation to a hash stub.
"""

from __future__ import annotations

import logging

from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError
from ate_cloud.config import settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Async embedding service backed by OpenAI ``text-embedding-3-small``.

    Uses LangChain's ``OpenAIEmbeddings`` wrapper (handles auth, batching, and
    retry internally) plus a CircuitBreaker for cascading-failure protection.

    Args:
        api_key: OpenAI API key (required for real calls).
        model: Embedding model name (default ``text-embedding-3-small``).
        dimensions: Expected vector dimensionality (1536 for 3-small).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
    ) -> None:
        from langchain_openai import OpenAIEmbeddings
        from pydantic import SecretStr

        self._model = model
        self._dimensions = dimensions
        if settings.openai_base_url:
            self._embeddings = OpenAIEmbeddings(
                model=model,
                api_key=SecretStr(api_key),
                dimensions=dimensions,
                base_url=settings.openai_base_url,
            )
        else:
            self._embeddings = OpenAIEmbeddings(
                model=model,
                api_key=SecretStr(api_key),
                dimensions=dimensions,
            )
        self._breaker = CircuitBreaker(
            failure_threshold=5,
            timeout=30.0,
            name="openai-embedding",
        )

    @property
    def dimensions(self) -> int:
        """Configured embedding vector dimensionality."""
        return self._dimensions

    @property
    def model_name(self) -> str:
        """Configured embedding model name."""
        return self._model

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Underlying CircuitBreaker instance (for inspection/reset)."""
        return self._breaker

    async def embed(self, text: str) -> list[float]:
        """Embed a single text into a real float vector.

        Args:
            text: Input text to embed.

        Returns:
            1536-dim float vector from the OpenAI embeddings API.

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN after repeated failures.
            Exception: Any API error not suppressed by the breaker.
        """
        if not text.strip():
            return [0.0] * self._dimensions

        async def _do_embed() -> list[float]:
            return await self._embeddings.aembed_query(text)

        return await self._breaker.call(_do_embed)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts in a single API call for efficiency.

        Empty strings in ``texts`` produce zero vectors without an API call.

        Args:
            texts: List of input texts.

        Returns:
            List of embedding vectors, one per input text.

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN.
            Exception: Any API error not suppressed by the breaker.
        """
        if not texts:
            return []
        # Fast path: all empty -> zero vectors, no API call
        if all(not t.strip() for t in texts):
            return [[0.0] * self._dimensions for _ in texts]

        async def _do_batch() -> list[list[float]]:
            return await self._embeddings.aembed_documents(texts)

        return await self._breaker.call(_do_batch)

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Alias for :meth:`embed_batch` (DeepAgents-compatible naming)."""
        return await self.embed_batch(texts)


__all__ = ["EmbeddingService", "CircuitBreakerOpenError"]
