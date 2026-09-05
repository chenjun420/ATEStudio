"""Diagnosis Service - AI-assisted fault diagnosis via hybrid retrieval + LLM.

Receives a diagnosis request (product type, failed test, error code, log
snippet), retrieves relevant failure cases via HybridRetriever (Qdrant
vector + ontology knowledge-graph fusion), then calls an LLM with the
retrieved context to produce a structured diagnosis (root cause,
confidence, evidence citations, repair steps).

Per AGENTS.md §7: the LLM call is protected by a CircuitBreaker
(failure_threshold=5, timeout=30s). If the LLM is configured but
unreachable, ``CircuitBreakerOpenError`` propagates - no silent
degradation to a stub response.

Persistence (task 15) lives OUTSIDE this service: the API layer writes
each diagnosis to the ``diagnoses`` ORM table and records operator
feedback there (see ``api/v1/diagnose.py`` + ``services/diagnosis_store.py``).
This service stays stateless apart from its LLM client/circuit breaker, so
a single shared instance cached on ``app.state`` serves every request.
Pure prompt/parse helpers are in ``services/diagnosis_prompt.py``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ate_cloud.config import settings
from ate_cloud.services.diagnosis_prompt import (
    MAX_CONTEXT_CASES,
    SYSTEM_PROMPT,
    DiagnosisRequest,
    build_diagnosis_info,
    build_retrieval_only_result,
    parse_llm_response,
)
from ate_cloud.services.hybrid_retriever import HybridRetriever
from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)


class DiagnosisService:
    """AI-assisted fault diagnosis service.

    Pipeline: diagnosis request -> hybrid retrieval (Qdrant + ontology KG) ->
    LLM analysis with retrieved context -> structured diagnosis result.

    The LLM call is protected by a CircuitBreaker. If the LLM is
    unreachable, ``CircuitBreakerOpenError`` propagates to the caller
    (no silent degradation per AGENTS.md §7). With no API key configured,
    the service returns a retrieval-only result (no LLM call).

    Args:
        hybrid_retriever: HybridRetriever for Qdrant + ontology-KG fusion search.
        api_key: OpenAI API key (defaults to ``settings.openai_api_key``).
        model: Chat model name (default ``settings.openai_model``).
    """

    def __init__(
        self,
        hybrid_retriever: HybridRetriever,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self._retriever = hybrid_retriever
        self._api_key = api_key if api_key is not None else settings.openai_api_key
        self._model = model or settings.openai_model
        self._breaker = CircuitBreaker(
            failure_threshold=5,
            timeout=30.0,
            name="llm-diagnosis-service",
        )
        self._llm: Any = None
        self._prompt: Any = None
        self._initialized = False

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Underlying CircuitBreaker instance (for inspection/reset)."""
        return self._breaker

    @property
    def model_name(self) -> str:
        """Configured chat model name (persisted as ``llm_model``)."""
        return self._model

    def _ensure_initialized(self) -> None:
        """Lazily initialize LangChain LLM and prompt template (deferred import).

        Defers ``langchain_openai`` / ``langchain_core`` imports until the
        first diagnosis call, so modules importing this service don't pay
        the LangChain startup cost if the LLM is never used.
        """
        if self._initialized:
            return
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        kwargs: dict[str, Any] = {
            "model": self._model,
            "api_key": SecretStr(self._api_key),
            "temperature": 0,
        }
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        self._llm = ChatOpenAI(**kwargs)
        self._prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("human", "{diagnosis_info}"),
        ])
        self._initialized = True

    async def diagnose(
        self,
        product_type: str,
        failed_test: str,
        error_code: str = "",
        log_snippet: str = "",
    ) -> dict[str, Any]:
        """Diagnose a test failure using hybrid retrieval + LLM analysis.

        Args:
            product_type: Product type identifier (e.g. ``"COMM-DEV-001"``).
            failed_test: Name/description of the failed test.
            error_code: Error code if available (e.g. ``"ERR_I2C_TIMEOUT"``).
            log_snippet: Log fragment from the failed execution.

        Returns:
            Diagnosis dict with keys: ``diagnosis_id``, ``root_cause``,
            ``confidence``, ``evidence_citations``, ``repair_steps``,
            ``retrieved_cases``, ``llm_model`` (None when retrieval-only),
            and ``retrieval_only`` (True when no LLM key is configured).

        Raises:
            CircuitBreakerOpenError: If the LLM circuit breaker is OPEN.
            Exception: Any LLM API error not suppressed by the breaker.
        """
        request = DiagnosisRequest(
            product_type=product_type,
            failed_test=failed_test,
            error_code=error_code,
            log_snippet=log_snippet,
        )

        # 1. Hybrid retrieval - get top-k relevant failure cases. The
        # structured error code is passed through so the graph branch can
        # seed traversal on the exact seed Fault id (shared-ID join).
        query_text = request.to_query_text()
        retrieved_cases = await self._retriever.search(
            query_text,
            top_k=MAX_CONTEXT_CASES,
            rerank=True,
            error_code=request.error_code,
        )

        # 2. Build LLM prompt with retrieved context
        diagnosis_info = build_diagnosis_info(request, retrieved_cases)

        # 3. LLM analysis via CircuitBreaker
        diagnosis_id = str(uuid.uuid4())

        if not self._api_key:
            logger.warning(
                "No OpenAI API key configured; returning retrieval-only diagnosis"
            )
            result = build_retrieval_only_result(diagnosis_id, retrieved_cases)
            result["llm_model"] = None
            result["retrieval_only"] = True
            return result

        self._ensure_initialized()

        async def _do_llm_call() -> str:
            messages = self._prompt.format_messages(diagnosis_info=diagnosis_info)
            response = await self._llm.ainvoke(messages)
            return str(response.content)

        raw = await self._breaker.call(_do_llm_call)
        # CircuitBreaker.call infers T as Coroutine for async fn; runtime is str
        result = parse_llm_response(raw)
        result["diagnosis_id"] = diagnosis_id
        result["retrieved_cases"] = retrieved_cases
        result["llm_model"] = self._model
        result["retrieval_only"] = False
        return result


__all__ = [
    "DiagnosisRequest",
    "DiagnosisService",
    "CircuitBreakerOpenError",
]
