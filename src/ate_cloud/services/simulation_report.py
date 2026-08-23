"""Simulation report composition service (T41, v41-gap-analysis #41).

Composes the end-of-run report served by
``GET /executions/{run_id}/simulation-report`` from three already-computed
sources — the endpoint never re-derives what T13/T14 already computed:

- **coverage** — :meth:`ate_platform.simulation.coverage.SimulationCoverage.report`
  (T14) over the materialized plan's compiled DAG and the run's recorded
  step events. Degrades to an unavailable section when the sequence cannot
  be materialized/compiled or no recording exists.
- **contention** — :meth:`ate_platform.simulation.contention.ResourceContentionAnalyzer.analyze`
  (T13) fed the ``lock_wait``/``lock_acquire``/``lock_release`` events from
  the run's JSONL recording, when present; empty section otherwise.
- **faults** — the execution's recorded fault records
  (``Execution.result["faults"]``), normalized to canonical display keys.

Every analysis section degrades gracefully to
``{"available": False, "reason": ..., "report": None}`` with the reason
collected into the top-level ``warnings`` list, so an aborted/partial run
still renders a usable report (QA failure scenario).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.config import settings
from ate_cloud.models.execution import Execution
from ate_platform.simulation.contention import ResourceContentionAnalyzer
from ate_platform.simulation.coverage import SimulationCoverage
from ate_platform.simulation.headless_runner import _compiled_step_id
from ate_platform.simulation.recording import RecordingInterceptor

__all__ = ["build_simulation_report"]

# Recording kind → T13 analyzer event type (analyzer also accepts these
# aliases natively via _TYPE_ALIASES; we map explicitly for clarity).
_LOCK_KIND_TO_TYPE = {
    "lock_wait": "wait",
    "lock_acquire": "acquire",
    "lock_release": "release",
}


def _load_recording(run_id: str) -> list[dict[str, Any]]:
    """Load the run's JSONL recording; a missing file yields no events."""
    path = Path(settings.recordings_dir) / f"{run_id}.jsonl"
    if not path.is_file():
        return []
    return RecordingInterceptor.load(path)


async def _coverage_section(
    db: AsyncSession,
    run_id: str,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute the T14 coverage section, degrading on any failure.

    Needs the compiled DAG (``SequenceCompiler`` over the materialized
    YamlPlan); executed ids come from the recording's ``step_started``
    events translated through ``_compiled_step_id`` (T15 loop-expansion
    gotcha: dry-run style ids never occur here, but the translation is a
    no-op for flat ids and keeps the two conventions aligned).
    """
    if not events:
        return {"available": False, "reason": "no recording for this run", "report": None}
    try:
        from ate_cloud.services.plan_materializer import ExecutionPlanMaterializer
        from ate_platform.scheduler.compiler import SequenceCompiler

        plan = await ExecutionPlanMaterializer(db).materialize(run_id)
        cov = SimulationCoverage(SequenceCompiler().compile(plan))
        executed = [
            _compiled_step_id(str(ev["step_id"]))
            for ev in events
            if ev.get("kind") == "step_started" and ev.get("step_id")
        ]
        cov.record(executed_ids=executed)
        return {"available": True, "reason": None, "report": cov.report()}
    except Exception as e:  # noqa: BLE001 — coverage is best-effort enrichment
        return {"available": False, "reason": f"coverage unavailable: {e}", "report": None}


def _contention_section(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the T13 contention section from recorded lock events.

    Recording events carry ``kind``/``t``; the analyzer expects
    ``type``/``ts`` — remap here, then let the analyzer validate the stream
    (ordering violations degrade instead of failing the request).
    """
    lock_events = [
        {
            "type": _LOCK_KIND_TO_TYPE[kind],
            "ts": ev.get("t", 0.0),
            "resource": str(ev.get("resource", "")),
            "owner": str(ev.get("owner", "")),
        }
        for ev in events
        if (kind := ev.get("kind")) in _LOCK_KIND_TO_TYPE
    ]
    if not lock_events:
        return {"available": False, "reason": "no lock events in recording", "report": None}
    try:
        report = ResourceContentionAnalyzer.from_events(lock_events).analyze()
    except ValueError as e:
        return {"available": False, "reason": f"lock event stream invalid: {e}", "report": None}
    return {"available": True, "reason": None, "report": report}


def _normalize_fault_record(raw: Any, idx: int) -> dict[str, Any]:
    """Normalize one fault record to canonical display keys (aliases filled).

    Non-mapping entries degrade to a ``{"detail": ...}`` record rather than
    crashing the whole report.
    """
    rec = dict(raw) if isinstance(raw, Mapping) else {"detail": str(raw)}
    rec.setdefault("fault_id", rec.get("id") or f"fault-{idx}")
    rec.setdefault("type", rec.get("fault_type") or "unknown")
    rec.setdefault("severity", rec.get("level") or "warning")
    rec.setdefault("timestamp", rec.get("ts") or rec.get("time"))
    rec.setdefault("target", rec.get("target_id") or rec.get("link_id"))
    return rec


def _faults_section(execution: Execution) -> dict[str, Any]:
    """Extract the execution's recorded faults (``result["faults"]``)."""
    result = execution.result if isinstance(execution.result, Mapping) else {}
    raw_faults = result.get("faults")
    records = (
        [_normalize_fault_record(f, i) for i, f in enumerate(raw_faults)]
        if isinstance(raw_faults, list)
        else []
    )
    return {"records": records, "total": len(records)}


async def build_simulation_report(
    db: AsyncSession,
    run_id: str,
    execution: Execution,
) -> dict[str, Any]:
    """Assemble the consolidated simulation report for one run.

    Args:
        db: Database session (used to materialize the plan for coverage).
        run_id: The execution run identifier.
        execution: Already-loaded Execution row (existence checked upstream).

    Returns:
        Report envelope with ``run_id`` / ``run_status`` / ``generated_at``
        identity, the three sections, and a ``warnings`` list naming every
        degraded section so clients can render a partial-report banner.
    """
    events = _load_recording(run_id)
    coverage = await _coverage_section(db, run_id, events)
    contention = _contention_section(events)
    faults = _faults_section(execution)

    warnings = [
        f"{label}: {section['reason']}"
        for label, section in (("覆盖率", coverage), ("资源竞争", contention))
        if not section["available"]
    ]

    return {
        "run_id": run_id,
        "run_status": execution.status,
        "generated_at": datetime.now(UTC).isoformat(),
        "warnings": warnings,
        "coverage": coverage,
        "contention": contention,
        "faults": faults,
    }
