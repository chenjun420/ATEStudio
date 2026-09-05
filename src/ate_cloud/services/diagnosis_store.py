"""DB persistence for AI diagnoses and operator feedback (task 15).

Thin SQLAlchemy 2 data-access layer over the ``diagnoses`` ORM model
(``models/knowledge.py``). Kept separate from the API route so the mapping
between a :class:`DiagnosisService` result and a persisted row is testable
directly with an in-memory SQLite session.

- :func:`persist_diagnosis` inserts a row (symptom/conclusion/summary/model),
  linking it to an execution run / edge session when supplied.
- :func:`record_feedback` updates ``helpful`` / ``feedback_note`` on an
  existing row and returns ``None`` when the id is unknown (the route maps
  ``None`` to 404).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.knowledge import Diagnosis

logger = logging.getLogger(__name__)

#: Operator feedback string -> persisted ``helpful`` flag.
HELPFUL_BY_FEEDBACK: dict[str, bool] = {
    "confirmed": True,
    "rejected": False,
}


def build_symptom(
    *,
    failed_test: str,
    error_code: str = "",
    log_snippet: str = "",
    product_type: str = "",
) -> str:
    """Assemble the persisted symptom text from the diagnosis request fields."""
    parts = [f"failed_test: {failed_test}"]
    if product_type:
        parts.append(f"product: {product_type}")
    if error_code:
        parts.append(f"error_code: {error_code}")
    if log_snippet:
        parts.append(f"log: {log_snippet[:500]}")
    return " | ".join(parts)


def build_context_summary(retrieved_cases: list[dict[str, Any]]) -> str:
    """Summarize retrieved RAG/KG context for the ``context_summary`` column."""
    if not retrieved_cases:
        return "no retrieved cases"
    lines = [f"{len(retrieved_cases)} retrieved case(s):"]
    for case in retrieved_cases:
        case_id = str(case.get("id", "unknown"))
        source = str(case.get("source", "unknown"))
        symptom = case.get("symptom") or case.get("failed_step_name") or ""
        lines.append(f"- [{source}] {case_id}: {symptom}".rstrip())
    return "\n".join(lines)


async def persist_diagnosis(
    db: AsyncSession,
    *,
    diagnosis_id: str,
    symptom: str,
    result: dict[str, Any],
    run_id: str | None = None,
    session_id: str | None = None,
) -> Diagnosis:
    """Insert a persisted ``Diagnosis`` row for a completed diagnosis.

    Args:
        db: Async database session (commit owned by the caller/dependency).
        diagnosis_id: The uuid returned in the diagnosis result.
        symptom: Assembled symptom text (see :func:`build_symptom`).
        result: The DiagnosisService result dict.
        run_id: Optional execution run id (FK -> executions.id).
        session_id: Optional edge/NATS session reference.

    Returns:
        The persisted ORM row (flushed, so its columns are readable).
    """
    root_cause = str(result.get("root_cause") or "")
    row = Diagnosis(
        id=diagnosis_id,
        run_id=run_id or None,
        session_id=session_id or None,
        symptom=symptom,
        conclusion=root_cause or None,
        context_summary=build_context_summary(
            list(result.get("retrieved_cases") or [])
        ),
        llm_model=result.get("llm_model"),
    )
    db.add(row)
    await db.flush()
    logger.info("Persisted diagnosis %s (run=%s)", diagnosis_id, run_id or "(none)")
    return row


async def record_feedback(
    db: AsyncSession,
    *,
    diagnosis_id: str,
    helpful: bool,
    note: str = "",
) -> Diagnosis | None:
    """Update operator feedback on a persisted diagnosis.

    Args:
        db: Async database session (commit owned by the caller/dependency).
        diagnosis_id: The diagnosis id to update.
        helpful: ``True`` for confirmed, ``False`` for rejected.
        note: Optional correction / note ('' stores NULL).

    Returns:
        The updated row, or ``None`` when no diagnosis has that id (route
        maps this to 404).
    """
    from sqlalchemy import select

    row = (
        await db.execute(select(Diagnosis).where(Diagnosis.id == diagnosis_id))
    ).scalar_one_or_none()
    if row is None:
        return None
    row.helpful = helpful
    row.feedback_note = note or None
    await db.flush()
    logger.info(
        "Feedback on diagnosis %s: helpful=%s note=%s",
        diagnosis_id,
        helpful,
        note or "(none)",
    )
    return row


__all__ = [
    "HELPFUL_BY_FEEDBACK",
    "build_symptom",
    "build_context_summary",
    "persist_diagnosis",
    "record_feedback",
]
