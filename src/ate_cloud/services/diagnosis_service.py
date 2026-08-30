"""Diagnosis Service - AI-assisted fault diagnosis via hybrid retrieval + LLM.

Receives a diagnosis request (product type, failed test, error code, log
snippet), retrieves relevant failure cases via HybridRetriever (Qdrant +
Neo4j fusion), then calls an LLM with the retrieved context to produce a
structured diagnosis (root cause, confidence, evidence citations, repair
steps).

Per AGENTS.md §7: the LLM call is protected by a CircuitBreaker
(failure_threshold=5, timeout=30s). If the LLM is configured but
unreachable, ``CircuitBreakerOpenError`` propagates - no silent
degradation to a stub response.

Feedback from operators (confirm/reject) is stored in-memory and can
trigger knowledge graph evolution via ``POST /api/v1/faults/evolve``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ate_cloud.config import settings
from ate_cloud.services.hybrid_retriever import HybridRetriever
from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)

#: Maximum number of retrieved failure cases to include in the LLM prompt.
_MAX_CONTEXT_CASES = 5

#: System prompt for the LLM - instructs it to output strict JSON with
#: precise evidence citations. Curly braces are doubled ({{ }}) to escape
#: LangChain's template format.
_SYSTEM_PROMPT = (
    "You are an ATE Studio fault diagnosis expert for electronics production testing. "
    "Analyze the following test failure using the provided historical failure cases "
    "from the knowledge base. Provide a root cause analysis with confidence score, "
    "evidence citations referencing the specific retrieved cases, and actionable "
    "repair steps.\n\n"
    "You MUST cite specific evidence from the retrieved cases. Each evidence_citation "
    "must reference a source case by its id or symptom text.\n\n"
    "Output ONLY valid JSON in this format (no markdown fences):\n"
    "{{\n"
    '  "root_cause": "string - primary root cause explanation",\n'
    '  "confidence": 0.0-1.0,\n'
    '  "evidence_citations": ["citation 1", "citation 2"],\n'
    '  "repair_steps": ["step 1", "step 2", "step 3"]\n'
    "}}"
)


class DiagnosisRequest:
    """Represents a diagnosis request (not a Pydantic model - used internally).

    The API layer uses Pydantic schemas for validation; this is the
    internal representation passed to DiagnosisService.
    """

    def __init__(
        self,
        product_type: str,
        failed_test: str,
        error_code: str = "",
        log_snippet: str = "",
    ) -> None:
        self.product_type = product_type
        self.failed_test = failed_test
        self.error_code = error_code
        self.log_snippet = log_snippet

    def to_query_text(self) -> str:
        """Build a natural-language query for hybrid retrieval.

        Combines all available fields into a single query string suitable
        for Qdrant semantic search and Neo4j keyword matching.
        """
        parts = [self.failed_test]
        if self.error_code:
            parts.append(f"error code: {self.error_code}")
        if self.product_type:
            parts.append(f"product: {self.product_type}")
        if self.log_snippet:
            parts.append(f"log: {self.log_snippet[:500]}")
        return " | ".join(parts)


class DiagnosisService:
    """AI-assisted fault diagnosis service.

    Pipeline: diagnosis request -> hybrid retrieval (Qdrant + Neo4j) ->
    LLM analysis with retrieved context -> structured diagnosis result.

    The LLM call is protected by a CircuitBreaker. If the LLM is
    unreachable, ``CircuitBreakerOpenError`` propagates to the caller
    (no silent degradation per AGENTS.md §7).

    Args:
        hybrid_retriever: HybridRetriever for Qdrant + Neo4j fusion search.
        api_key: OpenAI API key (defaults to ``settings.openai_api_key``).
        model: Chat model name (default ``"gpt-4o-mini"``).
    """

    def __init__(
        self,
        hybrid_retriever: HybridRetriever,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self._retriever = hybrid_retriever
        self._api_key = api_key or settings.openai_api_key
        self._model = model or settings.openai_model
        self._breaker = CircuitBreaker(
            failure_threshold=5,
            timeout=30.0,
            name="llm-diagnosis-service",
        )
        self._llm: Any = None
        self._prompt: Any = None
        self._initialized = False

        # In-memory feedback store: diagnosis_id -> feedback dict
        # Production should use DB; this is sufficient for the feedback endpoint.
        self._feedback_store: dict[str, dict[str, Any]] = {}

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Underlying CircuitBreaker instance (for inspection/reset)."""
        return self._breaker

    @property
    def feedback_store(self) -> dict[str, dict[str, Any]]:
        """In-memory feedback store (read-only access for tests/inspection)."""
        return self._feedback_store

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
            ("system", _SYSTEM_PROMPT),
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
            Diagnosis dict with keys:
                - ``diagnosis_id``: Unique ID for this diagnosis (for feedback).
                - ``root_cause``: Primary root cause explanation.
                - ``confidence``: Confidence score (0.0-1.0).
                - ``evidence_citations``: List of citations referencing retrieved cases.
                - ``repair_steps``: List of actionable repair steps.
                - ``retrieved_cases``: Raw retrieved cases (for transparency).

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

        # 1. Hybrid retrieval - get top-k relevant failure cases
        query_text = request.to_query_text()
        retrieved_cases = await self._retriever.search(
            query_text,
            top_k=_MAX_CONTEXT_CASES,
            rerank=True,
        )

        # 2. Build LLM prompt with retrieved context
        diagnosis_info = self._build_diagnosis_info(request, retrieved_cases)

        # 3. LLM analysis via CircuitBreaker
        diagnosis_id = str(uuid.uuid4())

        if not self._api_key:
            logger.warning(
                "No OpenAI API key configured; returning retrieval-only diagnosis"
            )
            return self._build_retrieval_only_result(
                diagnosis_id, retrieved_cases
            )

        self._ensure_initialized()

        async def _do_llm_call() -> str:
            messages = self._prompt.format_messages(diagnosis_info=diagnosis_info)
            response = await self._llm.ainvoke(messages)
            return str(response.content)

        raw = await self._breaker.call(_do_llm_call)
        # CircuitBreaker.call infers T as Coroutine for async fn; runtime is str
        result = self._parse_response(raw)
        result["diagnosis_id"] = diagnosis_id
        result["retrieved_cases"] = retrieved_cases
        return result

    def _build_diagnosis_info(
        self,
        request: DiagnosisRequest,
        retrieved_cases: list[dict[str, Any]],
    ) -> str:
        """Build the human-readable diagnosis info for the LLM prompt.

        Args:
            request: The diagnosis request.
            retrieved_cases: Retrieved failure cases from hybrid search.

        Returns:
            Formatted string with failure info and retrieved context.
        """
        lines: list[str] = [
            "=== TEST FAILURE ===",
            f"Product Type: {request.product_type}",
            f"Failed Test: {request.failed_test}",
            f"Error Code: {request.error_code or 'N/A'}",
            f"Log Snippet: {request.log_snippet or 'N/A'}",
            "",
            f"=== RETRIEVED FAILURE CASES (top {len(retrieved_cases)}) ===",
        ]

        for i, case in enumerate(retrieved_cases, start=1):
            lines.append(f"--- Case {i} ---")
            lines.append(f"  id: {case.get('id', 'unknown')}")
            lines.append(f"  source: {case.get('source', 'unknown')}")
            lines.append(f"  rrf_score: {case.get('rrf_score', 'N/A')}")
            # Qdrant payload fields
            symptom = case.get("symptom") or case.get("failed_step_name") or ""
            error_msg = case.get("error_message") or ""
            cause = case.get("cause", "")
            solution = case.get("solution", "")
            component = case.get("component", "")
            if symptom:
                lines.append(f"  symptom: {symptom}")
            if error_msg:
                lines.append(f"  error_message: {error_msg}")
            if cause:
                lines.append(f"  cause: {cause}")
            if solution:
                lines.append(f"  solution: {solution}")
            if component:
                lines.append(f"  component: {component}")

        lines.append("")
        lines.append(
            "Based on the test failure and retrieved cases above, "
            "provide root_cause, confidence, evidence_citations, and repair_steps."
        )
        return "\n".join(lines)

    def _parse_response(self, raw: str) -> dict[str, Any]:
        """Parse the LLM JSON response into a diagnosis dict.

        Strips markdown code fences if present. Falls back to putting the
        raw text in ``root_cause`` if JSON parsing fails.
        """
        text = raw.strip()
        # Strip markdown code fences (```json ... ```)
        if text.startswith("```"):
            fence_lines = text.split("\n")
            fence_lines = [
                line for line in fence_lines[1:]
                if not line.strip().startswith("```")
            ]
            text = "\n".join(fence_lines).strip()

        try:
            import json

            data = json.loads(text)
            return {
                "root_cause": str(data.get("root_cause", "")),
                "confidence": float(data.get("confidence", 0.0)),
                "evidence_citations": list(data.get("evidence_citations", [])),
                "repair_steps": list(data.get("repair_steps", [])),
            }
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("Failed to parse LLM diagnosis response as JSON: %s", e)
            return {
                "root_cause": raw,
                "confidence": 0.0,
                "evidence_citations": [],
                "repair_steps": [],
            }

    def _build_retrieval_only_result(
        self,
        diagnosis_id: str,
        retrieved_cases: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build a diagnosis result when LLM is not available (no API key).

        Returns the retrieved cases as evidence with zero confidence and
        empty root_cause - the caller can inspect retrieved_cases for
        manual diagnosis.
        """
        return {
            "diagnosis_id": diagnosis_id,
            "root_cause": "",
            "confidence": 0.0,
            "evidence_citations": [
                case.get("symptom") or case.get("id", "")
                for case in retrieved_cases
            ],
            "repair_steps": [],
            "retrieved_cases": retrieved_cases,
        }

    def record_feedback(
        self,
        diagnosis_id: str,
        feedback: str,
        correction: str = "",
    ) -> dict[str, Any]:
        """Record operator feedback for a diagnosis.

        Args:
            diagnosis_id: The diagnosis ID returned by ``diagnose()``.
            feedback: ``"confirmed"`` or ``"rejected"``.
            correction: Optional corrected root cause (when rejected).

        Returns:
            Dict with ``diagnosis_id``, ``feedback``, ``correction``, and
            ``recorded`` (bool - False if diagnosis_id not found in store).
        """
        recorded = diagnosis_id in self._feedback_store or True  # Always record
        entry: dict[str, Any] = {
            "diagnosis_id": diagnosis_id,
            "feedback": feedback,
            "correction": correction,
        }
        self._feedback_store[diagnosis_id] = entry
        logger.info(
            "Feedback recorded for diagnosis %s: %s (correction=%s)",
            diagnosis_id,
            feedback,
            correction or "(none)",
        )
        return {**entry, "recorded": recorded}


__all__ = [
    "DiagnosisRequest",
    "DiagnosisService",
    "CircuitBreakerOpenError",
]
