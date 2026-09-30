"""Embedding Service — any OpenAI-compatible embeddings endpoint.

Wraps the LangChain ``OpenAIEmbeddings`` integration with a CircuitBreaker
for resilience against rate-limit and transient API failures.

The service is injected into ``FailureIndexer`` so failure events are embedded
with real semantic vectors instead of the previous hash-based stub.

Two things about the "OpenAI-compatible" contract are routinely assumed and
routinely false, so both are handled explicitly here:

* **Vector width is a property of the model, not of the API.** The default of
  1536 is OpenAI's ``text-embedding-3-small`` size; the Qwen text-embedding
  model this deployment uses returns 1024. Ask the endpoint, do not assume.
* **"OpenAI-compatible" does not mean "accepts everything OpenAI accepts."**
  LangChain's ``OpenAIEmbeddings`` tokenizes locally by default and sends token
  ids. That is only correct when the model uses OpenAI's tokenizer. Against
  DashScope's OpenAI-compatible endpoint it is rejected with ``input must be an
  array of strings`` — and even where it is accepted, batching a non-OpenAI
  model by OpenAI's token counts splits text at the wrong places.

Per AGENTS.md §7: if the API key is configured but the service is unreachable,
the CircuitBreaker opens and ``CircuitBreakerOpenError`` propagates — no
silent degradation to a hash stub.
"""

from __future__ import annotations

import logging

from ate_cloud.config import settings
from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Async embedding service backed by an OpenAI-compatible endpoint.

    Uses LangChain's ``OpenAIEmbeddings`` wrapper (handles auth, batching, and
    retry internally) plus a CircuitBreaker for cascading-failure protection.

    Args:
        api_key: OpenAI API key (required for real calls).
        model: Embedding model name.
        dimensions: Expected vector dimensionality. Must match what ``model``
            actually returns.
        check_ctx_length: Whether to tokenize locally and send token ids.
            Defaults to off — see the module docstring for why, and for what
            you give up by turning it on.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
        check_ctx_length: bool | None = None,
    ) -> None:
        from langchain_openai import OpenAIEmbeddings
        from pydantic import SecretStr

        self._model = model
        self._dimensions = dimensions
        if check_ctx_length is None:
            check_ctx_length = settings.embedding_check_ctx_length
        if settings.openai_base_url:
            self._embeddings = OpenAIEmbeddings(
                model=model,
                api_key=SecretStr(api_key),
                dimensions=dimensions,
                check_embedding_ctx_length=check_ctx_length,
                base_url=settings.openai_base_url,
            )
        else:
            self._embeddings = OpenAIEmbeddings(
                model=model,
                api_key=SecretStr(api_key),
                dimensions=dimensions,
                check_embedding_ctx_length=check_ctx_length,
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
            A ``self.dimensions``-length float vector from the embeddings API.

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
