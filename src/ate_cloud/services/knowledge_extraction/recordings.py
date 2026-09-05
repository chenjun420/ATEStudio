"""Recordings JSONL ingestion (deterministic, task 12).

Reads execution recordings in BOTH formats found in the repo:

* the current :class:`~ate_platform.simulation.recording.RecordingInterceptor`
  format — a ``recording_header`` line plus ``step_started`` /
  ``step_completed`` / ``step_failed`` events carrying ``step_id``;
* the legacy instrument-trace format in ``data/recordings/*.jsonl`` —
  per-call lines with ``resource_id`` / ``action`` and no step lifecycle.

Only step-lifecycle events carry traceability information; they are
aggregated per ``(execution_id, step_id)`` into one
:class:`RecordedStepResult` with an ATML 1636.1 outcome vocabulary
(``Passed`` / ``Failed`` / ``Inconclusive`` — a step that started but never
completed is inconclusive, not a crash).

Tolerance contract: torn tail lines, non-object lines, and lifecycle events
missing a ``step_id`` field are SKIPPED and counted (with a warning logged by
the service) — a malformed recording never raises.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_HEADER_KIND = "recording_header"
_LIFECYCLE_KINDS = frozenset({"step_started", "step_completed", "step_failed"})

#: ATML IEEE 1636.1 outcome vocabulary (ontology UUTResult.outcome one_of).
_OUTCOME_PASSED = "Passed"
_OUTCOME_FAILED = "Failed"
_OUTCOME_INCONCLUSIVE = "Inconclusive"


@dataclass(frozen=True, slots=True)
class RecordedStepResult:
    """One executed step's outcome within a recording session."""

    execution_id: str
    step_id: str
    outcome: str
    error: str | None


def read_recording(path: str | Path) -> tuple[list[RecordedStepResult], int]:
    """Aggregate step outcomes from one recording file.

    Returns ``(results, skipped_events)``. ``skipped_events`` counts lifecycle
    event lines that lacked a usable ``step_id`` (parse/torn/non-object lines
    are tolerated silently, matching RecordingInterceptor.load semantics).
    Never raises for malformed content.
    """
    # First-wins per (execution, step): a failure event overrides a pass;
    # a started-only step stays Inconclusive. Order of events in the file
    # decides the final outcome (completed/failed are terminal).
    outcomes: dict[tuple[str, str], RecordedStepResult] = {}
    skipped = 0

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn tail line: crash-safe prefix, skip silently
        if not isinstance(rec, dict):
            continue

        kind = rec.get("kind")
        if kind == _HEADER_KIND:
            continue
        if kind not in _LIFECYCLE_KINDS:
            continue  # instrument_call / variable_change / legacy trace line

        step_id = rec.get("step_id")
        execution_id = str(rec.get("execution_id") or _session_from_path(path))
        if not isinstance(step_id, str) or not step_id:
            skipped += 1
            logger.warning(
                "Recording %s event %r is missing a 'step_id' field; skipping event",
                path,
                kind,
            )
            continue

        key = (execution_id, step_id)
        outcome = _outcome_for(kind)
        if outcome is not None:
            error = rec.get("error") if kind == "step_failed" else None
            outcomes[key] = RecordedStepResult(
                execution_id=execution_id,
                step_id=step_id,
                outcome=outcome,
                error=error if isinstance(error, str) else None,
            )
        elif key not in outcomes:
            outcomes[key] = RecordedStepResult(
                execution_id=execution_id,
                step_id=step_id,
                outcome=_OUTCOME_INCONCLUSIVE,
                error=None,
            )

    return list(outcomes.values()), skipped


def _outcome_for(kind: str) -> str | None:
    """Map a terminal lifecycle event kind to an ATML outcome (started→None)."""
    if kind == "step_completed":
        return _OUTCOME_PASSED
    if kind == "step_failed":
        return _OUTCOME_FAILED
    return None


def _session_from_path(path: str | Path) -> str:
    """Fallback session id for recordings without an explicit execution_id."""
    return Path(path).stem


__all__ = ["RecordedStepResult", "read_recording"]
