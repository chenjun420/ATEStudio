"""SimulationCoverage — step + branch coverage as sequence-quality metrics.

Implements the coverage contract from 设计文档 §7.10: given the compiled flat
DAG (:class:`~ate_platform.scheduler.compiler.CompiledStep` list from
``SequenceCompiler``) and the executed-step events of one or more simulation
runs, compute

- **step coverage** — executed / planned expanded node ids, and
- **branch coverage** — which branch arms (then/else) were taken per
  ``branch_eval`` (``StepType.BRANCH``) node via its ``then_ids``/``else_ids``.

Pure analysis over ``(plan, executed-step-ids)`` inputs: no scheduler coupling,
no script instrumentation required. Skipped-by-precondition steps are NEVER
counted as covered — they are planned-but-uncovered and reported separately.

Report schema (stable contract; consumed by headless runner report embedding):

.. code-block:: python

    {
      "plan": {"total_steps": int, "total_branches": int,
               "total_branch_arms": int, "all_step_ids": [str, ...]},
      "step_coverage": {
        "planned": int,             # denominator: all expanded nodes
        "executed": int,            # numerator: covered ids (skipped excluded)
        "skipped": [str, ...],      # explicitly skipped (never in numerator)
        "unexecuted": [str, ...],   # sorted planned ids not covered
        "unknown_executed": [str],  # executed ids absent from the plan
        "percent": float,           # round(100 * executed / planned, 2); 0.0 if empty
      },
      "branch_coverage": {
        "branches": {branch_id: {
          "then_ids": [str], "else_ids": [str],
          "decisions": ["else"|"then", ...],   # sorted unique branch_eval outcomes
          "arms_covered": ["else"|"then", ...],# subset of {"then","else"}, sorted
          "arms_total": int,                   # 0..2 (empty arms excluded from %)
        }, ...},
        "arms_total": int, "arms_covered": int,
        "percent": float,           # 0.0 when no branch declares arms
        "both_sides_seen": [str],   # branches with both arms covered
      },
      "by_source_step": {source_step_id: {"planned": int, "executed": int}, ...},
      "summary": {"step_percent": float, "branch_percent": float,
                  "quality": "full"|"partial"|"empty"},
    }

Multiple runs accumulate via repeated :meth:`SimulationCoverage.record` calls;
two finished reports merge losslessly with :func:`merge_reports` (set unions +
recomputed percentages; commutative and deterministic).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from ate_platform.scheduler.compiler import CompiledStep
from shared.dsl import StepType

_ARM_VALUES = ("else", "then")  # sorted order for deterministic output
_ITER_SUFFIX = re.compile(r"(_iter\d+)+$")


def _pct(numerator: int, denominator: int) -> float:
    """Percentage rounded to 2 decimals; 0.0 for an empty denominator."""
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def _sorted_unique(values: Iterable[str]) -> list[str]:
    return sorted(set(values))


class SimulationCoverage:
    """Accumulates executed-step events and reports sequence-quality coverage."""

    def __init__(self, steps: Iterable[CompiledStep]) -> None:
        """Bind the coverage universe to a compiled flat DAG.

        Args:
            steps: Flat :class:`CompiledStep` list from ``SequenceCompiler.compile``.
                BRANCH nodes contribute their ``then_ids``/``else_ids`` arms;
                every node contributes one planned id.
        """
        self._steps = list(steps)
        self._planned_ids = [step.id for step in self._steps]
        self._planned_set = set(self._planned_ids)
        self._branches = [step for step in self._steps if step.type == StepType.BRANCH]
        self._covered: set[str] = set()
        self._skipped: set[str] = set()
        self._unknown: set[str] = set()
        self._decisions: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------

    def record(
        self,
        executed_ids: Iterable[str] = (),
        *,
        skipped_ids: Iterable[str] = (),
        branch_decisions: Mapping[str, str] | None = None,
    ) -> None:
        """Accumulate one run's execution events.

        Args:
            executed_ids: Expanded step ids that actually ran (terminal or any
                non-skipped outcome). Ids absent from the plan are flagged as
                ``unknown_executed`` instead of counted.
            skipped_ids: Steps skipped by precondition/skip_if — recorded for
                reporting but never counted as covered.
            branch_decisions: Mapping of BRANCH node id → ``"then"``/``"else"``
                per branch_eval event. Decisions mark the chosen arm covered
                even when its entry step did not run (e.g. empty arm).
        """
        for step_id in executed_ids:
            if step_id in self._planned_set:
                self._covered.add(step_id)
            else:
                self._unknown.add(step_id)
        for step_id in skipped_ids:
            if step_id in self._planned_set:
                self._skipped.add(step_id)
                self._covered.discard(step_id)
        for branch_id, decision in (branch_decisions or {}).items():
            if decision in _ARM_VALUES:
                self._decisions.setdefault(branch_id, set()).add(decision)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """Build the coverage report dict (schema in module docstring)."""
        return self._build_report(self._covered, self._skipped, self._unknown, self._decisions)

    # ------------------------------------------------------------------
    # Shared report construction (also used by merge_reports)
    # ------------------------------------------------------------------

    def _build_report(
        self,
        covered: set[str],
        skipped: set[str],
        unknown: set[str],
        decisions: Mapping[str, set[str]],
    ) -> dict[str, Any]:
        unexecuted = sorted(self._planned_set - covered)

        by_source: dict[str, dict[str, int]] = {}
        for step in self._steps:
            bucket = by_source.setdefault(step.source_step_id or step.id, {"planned": 0, "executed": 0})
            bucket["planned"] += 1
            if step.id in covered:
                bucket["executed"] += 1

        branches_block: dict[str, dict[str, Any]] = {}
        arms_total = arms_covered = 0
        both_sides: list[str] = []
        for branch in self._branches:
            then_ids, else_ids = list(branch.then_ids), list(branch.else_ids)
            explicit = {d for d in decisions.get(branch.id, ()) if d in _ARM_VALUES}
            seen = set(explicit)
            if not seen.isdisjoint(then_ids) or any(step_id in covered for step_id in then_ids):
                seen.add("then")
            if not seen.isdisjoint(else_ids) or any(step_id in covered for step_id in else_ids):
                seen.add("else")
            count = len(seen)
            arms_total += len(then_ids) + len(else_ids)
            arms_covered += count
            if then_ids and else_ids and count == 2:
                both_sides.append(branch.id)
            branches_block[branch.id] = {
                "then_ids": then_ids,
                "else_ids": else_ids,
                "decisions": sorted(explicit),
                "arms_covered": sorted(seen),
                "arms_total": len(then_ids) + len(else_ids),
            }

        step_pct = _pct(len(covered & self._planned_set), len(self._planned_ids))
        # No declared arms anywhere → branch coverage is vacuously complete.
        branch_pct = _pct(arms_covered, arms_total) if arms_total else 100.0
        branch_full = arms_total == 0 or arms_covered == arms_total
        quality = (
            "empty"
            if not self._planned_ids
            else ("full" if step_pct == 100.0 and branch_full else "partial")
        )

        return {
            "plan": {
                "total_steps": len(self._planned_ids),
                "total_branches": len(self._branches),
                "total_branch_arms": sum(len(b.then_ids) + len(b.else_ids) for b in self._branches),
                "all_step_ids": list(self._planned_ids),
            },
            "step_coverage": {
                "planned": len(self._planned_ids),
                "executed": len(covered & self._planned_set),
                "skipped": sorted(skipped),
                "unexecuted": unexecuted,
                "unknown_executed": sorted(unknown),
                "percent": step_pct,
            },
            "branch_coverage": {
                "branches": branches_block,
                "arms_total": arms_total,
                "arms_covered": arms_covered,
                "percent": branch_pct,
                "both_sides_seen": both_sides,
            },
            "by_source_step": by_source,
            "summary": {
                "step_percent": step_pct,
                "branch_percent": branch_pct,
                "quality": quality,
            },
        }


def merge_reports(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, Any]:
    """Merge two finished coverage reports over the same compiled plan.

    Lossless set-union merge (commutative, deterministic): covered ids are
    ``planned - unexecuted``, skipped/unknown union, branch decisions union.
    Percentages and quality recompute from the merged sets.

    Raises:
        ValueError: If the two reports describe different plans.
    """
    if first["plan"]["all_step_ids"] != second["plan"]["all_step_ids"]:
        msg = "merge_reports requires both reports to cover the same compiled plan"
        raise ValueError(msg)

    # Reconstruct a probe instance from the first report's plan universe so the
    # shared _build_report path recomputes every derived field identically.
    probe = SimulationCoverage(_PlanView(first))
    covered_a = set(first["plan"]["all_step_ids"]) - set(first["step_coverage"]["unexecuted"])
    covered_b = set(second["plan"]["all_step_ids"]) - set(second["step_coverage"]["unexecuted"])
    decisions: dict[str, set[str]] = {}
    for report in (first, second):
        for branch_id, entry in report["branch_coverage"]["branches"].items():
            decisions.setdefault(branch_id, set()).update(entry["decisions"])
    return probe._build_report(
        covered_a | covered_b,
        set(first["step_coverage"]["skipped"]) | set(second["step_coverage"]["skipped"]),
        set(first["step_coverage"]["unknown_executed"]) | set(second["step_coverage"]["unknown_executed"]),
        decisions,
    )


class _PlanView:
    """Minimal CompiledStep-like view rebuilt from a report's plan block.

    Lets :meth:`merge_reports` reuse :meth:`SimulationCoverage._build_report`
    without keeping mutable accumulators inside serialized reports.
    """

    def __init__(self, report: Mapping[str, Any]) -> None:
        self._branches = [
            _BranchView(branch_id, entry)
            for branch_id, entry in report["branch_coverage"]["branches"].items()
        ]
        # BRANCH nodes appear in all_step_ids AND the branches block; yield
        # each exactly once (as its branch view) to mirror the compiled list.
        branch_ids = {branch.id for branch in self._branches}
        self._node_ids = [sid for sid in report["plan"]["all_step_ids"] if sid not in branch_ids]

    def __iter__(self) -> Iterator[CompiledStep]:
        nodes: list[CompiledStep] = [
            _NodeView(step_id) for step_id in self._node_ids
        ]
        nodes.extend(self._branches)
        return iter(nodes)


class _NodeView(CompiledStep):
    """CompiledStep stand-in carrying only id/source_step_id/type."""

    def __init__(self, step_id: str) -> None:
        super().__init__(id=step_id)
        self.source_step_id = _ITER_SUFFIX.sub("", step_id)


class _BranchView(CompiledStep):
    """CompiledStep stand-in for a BRANCH node rebuilt from a report entry."""

    def __init__(self, branch_id: str, entry: Mapping[str, Any]) -> None:
        super().__init__(
            id=branch_id,
            type=StepType.BRANCH,
            then_ids=list(entry["then_ids"]),
            else_ids=list(entry["else_ids"]),
            source_step_id=branch_id,
        )
