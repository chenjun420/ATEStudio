"""Pure prompt-building / response-parsing helpers for diagnosis (task 15).

Split out of ``diagnosis_service.py`` to keep that module under the pure-LOC
ceiling. Everything here is a PURE function (or the lightweight internal
``DiagnosisRequest`` value type): no I/O, no LLM, no DB — unit-testable
directly. The LLM-facing JSON contract lives here so the service and any
caller share one source of truth.
"""

from __future__ import annotations

import json
from typing import Any

#: Maximum number of retrieved failure cases to include in the LLM prompt.
MAX_CONTEXT_CASES = 5

#: System prompt for the LLM - instructs it to output strict JSON with
#: precise evidence citations. Curly braces are doubled ({{ }}) to escape
#: LangChain's template format.
SYSTEM_PROMPT = (
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
    """Represents a diagnosis request (internal value type, not a Pydantic model).

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
        for Qdrant semantic search and ontology knowledge-graph traversal.
        """
        parts = [self.failed_test]
        if self.error_code:
            parts.append(f"error code: {self.error_code}")
        if self.product_type:
            parts.append(f"product: {self.product_type}")
        if self.log_snippet:
            parts.append(f"log: {self.log_snippet[:500]}")
        return " | ".join(parts)


def build_diagnosis_info(
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


def parse_llm_response(raw: str) -> dict[str, Any]:
    """Parse the LLM JSON response into a diagnosis dict.

    Strips markdown code fences if present. Falls back to putting the
    raw text in ``root_cause`` if JSON parsing fails.
    """
    text = raw.strip()
    # Strip markdown code fences (```json ... ```)
    if text.startswith("```"):
        fence_lines = text.split("\n")
        fence_lines = [
            line for line in fence_lines[1:] if not line.strip().startswith("```")
        ]
        text = "\n".join(fence_lines).strip()

    try:
        data = json.loads(text)
        return {
            "root_cause": str(data.get("root_cause", "")),
            "confidence": float(data.get("confidence", 0.0)),
            "evidence_citations": list(data.get("evidence_citations", [])),
            "repair_steps": list(data.get("repair_steps", [])),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {
            "root_cause": raw,
            "confidence": 0.0,
            "evidence_citations": [],
            "repair_steps": [],
        }


def build_retrieval_only_result(
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
            case.get("symptom") or case.get("id", "") for case in retrieved_cases
        ],
        "repair_steps": [],
        "retrieved_cases": retrieved_cases,
    }


__all__ = [
    "MAX_CONTEXT_CASES",
    "SYSTEM_PROMPT",
    "DiagnosisRequest",
    "build_diagnosis_info",
    "parse_llm_response",
    "build_retrieval_only_result",
]
