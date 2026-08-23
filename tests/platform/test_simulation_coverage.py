"""Tests for SimulationCoverage step+branch coverage reporting (T14, 设计文档 §7.10).

Coverage is pure analysis over (compiled plan, executed-step-ids) inputs —
no scheduler coupling, fully deterministic.
"""

from __future__ import annotations

import json

from ate_platform.scheduler.compiler import SequenceCompiler
from ate_platform.simulation.coverage import SimulationCoverage, merge_reports
from shared.dsl import LoopType, StepType, YamlLoop, YamlPlan, YamlStep

# ---------------------------------------------------------------------------
# Plan builders
# ---------------------------------------------------------------------------


def _step(step_id: str, **kwargs) -> YamlStep:
    return YamlStep(id=step_id, script=f"{step_id}.py", **kwargs)


def _branch(branch_id: str, then: list[str], else_: list[str], condition: str = "${v} > 0") -> YamlStep:
    return YamlStep(
        id=branch_id,
        type=StepType.BRANCH,
        params={"condition": condition, "then": then, "else": else_},
    )


def _compile(*items: YamlStep | YamlLoop) -> list:
    return SequenceCompiler().compile(YamlPlan(name="t", version="3.2", steps=list(items)))


# ---------------------------------------------------------------------------
# Step coverage
# ---------------------------------------------------------------------------


def test_full_coverage_all_steps_executed():
    steps = _compile(_step("a"), _step("b"), _step("c"))
    cov = SimulationCoverage(steps)
    cov.record(executed_ids=["a", "b", "c"])
    report = cov.report()

    assert report["step_coverage"]["percent"] == 100.0
    assert report["step_coverage"]["executed"] == 3
    assert report["step_coverage"]["unexecuted"] == []
    assert report["summary"]["quality"] == "full"


def test_partial_coverage_lists_unexecuted():
    steps = _compile(_step("a"), _step("b"), _step("c"))
    cov = SimulationCoverage(steps)
    cov.record(executed_ids=["a", "c"])
    report = cov.report()

    assert report["step_coverage"]["planned"] == 3
    assert report["step_coverage"]["executed"] == 2
    assert report["step_coverage"]["unexecuted"] == ["b"]
    assert report["step_coverage"]["percent"] == 66.67
    assert report["summary"]["quality"] == "partial"


def test_skipped_precondition_step_excluded_from_numerator():
    """Skipped-by-precondition is planned-but-NOT-covered (QA failure scenario)."""
    steps = _compile(_step("a"), _step("b"), _step("c"))
    cov = SimulationCoverage(steps)
    cov.record(executed_ids=["a", "c"], skipped_ids=["b"])
    report = cov.report()

    covered = set(report["plan"]["all_step_ids"]) - set(report["step_coverage"]["unexecuted"])
    assert "b" not in covered
    assert report["step_coverage"]["skipped"] == ["b"]
    # b stays in the denominator: 2/3 covered.
    assert report["step_coverage"]["percent"] == 66.67


def test_unknown_executed_id_reported_not_counted():
    steps = _compile(_step("a"))
    cov = SimulationCoverage(steps)
    cov.record(executed_ids=["ghost"])
    report = cov.report()

    assert report["step_coverage"]["unknown_executed"] == ["ghost"]
    assert report["step_coverage"]["executed"] == 0
    assert report["step_coverage"]["percent"] == 0.0


def test_empty_plan_yields_zero_percent_without_crash():
    cov = SimulationCoverage([])
    report = cov.report()

    assert report["step_coverage"]["planned"] == 0
    assert report["step_coverage"]["percent"] == 0.0
    assert report["summary"]["quality"] == "empty"


def test_loop_iterations_roll_up_to_source_step():
    loop = YamlLoop(id="lp", loop_type=LoopType.FOR, count=2, steps=[_step("s")])
    steps = _compile(_step("pre"), loop)
    ids = [s.id for s in steps]
    assert ids == ["pre", "s_iter0", "s_iter1"]

    cov = SimulationCoverage(steps)
    cov.record(executed_ids=["pre", "s_iter0"])
    report = cov.report()

    # Expanded ids drive the flat percentage (2/3).
    assert report["step_coverage"]["percent"] == 66.67
    # Roll-up view per original source step id.
    assert report["by_source_step"]["s"] == {"planned": 2, "executed": 1}
    assert report["by_source_step"]["pre"] == {"planned": 1, "executed": 1}


# ---------------------------------------------------------------------------
# Branch coverage
# ---------------------------------------------------------------------------


def _branched_plan():
    return _compile(
        _branch("chk", then=["t1"], else_=["e1"]),
        _step("t1"),
        _step("e1"),
    )


def test_branch_then_side_only_tracked():
    steps = _branched_plan()
    cov = SimulationCoverage(steps)
    cov.record(executed_ids=["chk", "t1"], branch_decisions={"chk": "then"})
    report = cov.report()

    entry = report["branch_coverage"]["branches"]["chk"]
    assert entry["arms_covered"] == ["then"]
    assert entry["decisions"] == ["then"]
    assert report["branch_coverage"]["arms_covered"] == 1
    assert report["branch_coverage"]["arms_total"] == 2
    assert report["branch_coverage"]["percent"] == 50.0


def test_branch_arm_inferred_from_executed_member_without_decision():
    steps = _branched_plan()
    cov = SimulationCoverage(steps)
    cov.record(executed_ids=["e1"])  # no explicit decision recorded
    report = cov.report()

    assert report["branch_coverage"]["branches"]["chk"]["arms_covered"] == ["else"]
    assert report["branch_coverage"]["branches"]["chk"]["decisions"] == []


def test_branch_both_sides_tracked_across_two_runs_merged():
    steps = _branched_plan()
    cov = SimulationCoverage(steps)
    cov.record(executed_ids=["chk", "t1"], branch_decisions={"chk": "then"})
    cov.record(executed_ids=["chk", "e1"], branch_decisions={"chk": "else"})
    report = cov.report()

    entry = report["branch_coverage"]["branches"]["chk"]
    assert entry["arms_covered"] == ["else", "then"]
    assert entry["decisions"] == ["else", "then"]
    assert report["branch_coverage"]["percent"] == 100.0
    assert report["branch_coverage"]["both_sides_seen"] == ["chk"]


def test_merge_reports_union_is_commutative_and_deterministic():
    steps = _branched_plan()
    cov_a = SimulationCoverage(steps)
    cov_a.record(executed_ids=["chk", "t1"], branch_decisions={"chk": "then"})

    cov_b = SimulationCoverage(steps)
    cov_b.record(executed_ids=["e1"], skipped_ids=["t1"], branch_decisions={"chk": "else"})

    merged_ab = merge_reports(cov_a.report(), cov_b.report())
    merged_ba = merge_reports(cov_b.report(), cov_a.report())
    assert merged_ab == merged_ba

    assert merged_ab["branch_coverage"]["branches"]["chk"]["arms_covered"] == ["else", "then"]
    assert merged_ab["branch_coverage"]["percent"] == 100.0
    assert merged_ab["step_coverage"]["skipped"] == ["t1"]
    assert merged_ab["summary"]["quality"] == "full"


def test_branch_with_no_arms_excluded_from_denominator():
    steps = _compile(
        _branch("empty_chk", then=[], else_=[]),
        _branch("real", then=["t1"], else_=["e1"]),
        _step("t1"),
        _step("e1"),
    )
    cov = SimulationCoverage(steps)
    cov.record(executed_ids=["real", "t1"], branch_decisions={"real": "then"})
    report = cov.report()

    assert report["branch_coverage"]["branches"]["empty_chk"]["arms_total"] == 0
    # Only 'real' contributes arms: 1/2, not 1/4.
    assert report["branch_coverage"]["arms_total"] == 2
    assert report["branch_coverage"]["percent"] == 50.0


# ---------------------------------------------------------------------------
# Schema contract (consumed by T15 headless --html-report embedding)
# ---------------------------------------------------------------------------


def test_report_schema_top_level_keys_stable():
    steps = _branched_plan()
    cov = SimulationCoverage(steps)
    cov.record(executed_ids=["chk", "t1"], branch_decisions={"chk": "then"})
    report = cov.report()

    assert set(report) == {"plan", "step_coverage", "branch_coverage", "by_source_step", "summary"}
    assert set(report["step_coverage"]) == {
        "planned", "executed", "skipped", "unexecuted", "unknown_executed", "percent",
    }
    assert set(report["branch_coverage"]) == {
        "branches", "arms_total", "arms_covered", "percent", "both_sides_seen",
    }
    assert set(report["summary"]) == {"step_percent", "branch_percent", "quality"}
    # JSON-safe: everything serializes without error.
    assert isinstance(json.dumps(report), str)


def test_report_is_recomputable_and_does_not_mutate_accumulators():
    steps = _compile(_step("a"), _step("b"))
    cov = SimulationCoverage(steps)
    cov.record(executed_ids=["a"])

    first = cov.report()
    second = cov.report()
    assert first == second

    cov.record(executed_ids=["b"])
    assert cov.report()["step_coverage"]["percent"] == 100.0
