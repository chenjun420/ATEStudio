"""Flow planner tests (P2.5).

Focus on the failure modes that produce a *plausible but wrong* sequence,
because those are the ones a review will not catch: a source left at the
previous scenario's voltage still yields a number, and that number gets
compared against a limit and passes.

The last test in this file is the important one — it feeds the generated plan
to the real DSL parser. A planner that emits YAML nothing can read has
produced no plan at all, and that failure only shows up at commissioning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ate_cloud.schemas.aterag_bundle import (
    BundleModel,
    ClauseModel,
    RequirementModel,
    ScenarioModel,
    contract_hash,
)
from ate_cloud.services.flow_planner import (
    load_bindings,
    plan_flow,
)

REPO = Path(__file__).resolve().parents[2]


def _clause(kind: str, role: str, value: dict | None = None) -> ClauseModel:
    return ClauseModel(kind=kind, text=f"{kind} text", role=role, value=value)


def _bundle(reqs: list[RequirementModel]) -> BundleModel:
    return BundleModel(
        bundle_version="1.0",
        contract_hash=contract_hash(),
        product_code="PA601-TEST",
        doc_version="B",
        requirements=reqs,
    )


def _req(
    code: str,
    *,
    inputs: list[ClauseModel],
    outputs: list[ClauseModel],
    n_scen: int = 1,
    rail: str = "",
    binding: str = "tierA",
) -> RequirementModel:
    return RequirementModel(
        requirement_code=code,
        title=code,
        input_conditions=inputs,
        output_conditions=outputs,
        scenarios=[
            ScenarioModel(
                scenario_id=f"{code}#s{i}", seq=i, name=f"场景{i}", rail=rail,
                bindings={"ac_input_tier": binding},
            )
            for i in range(n_scen)
        ],
    )


# ── binding table ───────────────────────────────────────────────────────────


class TestBindingTable:
    def test_covers_every_kind_in_real_data(self) -> None:
        """The table must cover the kinds ATERag actually emits.

        A gap here is invisible in production: the planner omits those steps and
        the report still says PASS.
        """
        bundle_path = Path(r"F:\Workspace\ATERag\rag_storage\exports\studio_bundle.json")
        if not bundle_path.exists():
            pytest.skip("需要 ATERag 导出的真实 bundle")
        import json

        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        # load_bindings() is keyed by (kind, side) — match that, not the
        # (side, kind) order used when reporting.
        table = set(load_bindings())
        needed = {
            (c["kind"], "input")
            for r in bundle["requirements"]
            for c in r["input_conditions"]
        } | {
            (c["kind"], "output")
            for r in bundle["requirements"]
            for c in r["output_conditions"]
        }
        missing = needed - table
        assert not missing, f"这些 kind 无绑定, 对应条件排不出步骤: {sorted(missing)}"

    def test_no_duplicate_bindings(self) -> None:
        """A duplicate would make the planner pick arbitrarily between two plans."""
        load_bindings()  # raises on duplicate

    def test_chamber_is_batch_level(self) -> None:
        """A 30-minute soak charged per unit invents hours of dead time."""
        b = load_bindings()[("temperature", "input")]
        assert b.batch_level is True
        assert b.settle_s > 600


# ── setup coalescing ────────────────────────────────────────────────────────


class TestSetupCoalescing:
    def test_identical_setup_shared_within_segment(self) -> None:
        """Six scenarios at the same excitation set the source once, not six times."""
        reqs = [
            _req(
                f"SR-{i}",
                inputs=[_clause("input_voltage", "input", {"typ": 110.0, "unit": "Vac"})],
                outputs=[_clause("output_voltage", "output")],
            )
            for i in range(6)
        ]
        plan = plan_flow(_bundle(reqs), load_bindings())
        assert len(plan.segments) == 1
        set_inputs = [s for s in plan.segments[0].steps if s.action == "set_ac_input"]
        assert len(set_inputs) == 1
        # every requirement still measured — grouping must not lose coverage
        measures = [s for s in plan.segments[0].steps if s.action == "measure_dc_voltage"]
        assert len(measures) == 6

    def test_different_setup_not_coalesced(self) -> None:
        """Coalescing on the wrong key is the dangerous bug: the source stays at
        the previous scenario's voltage and still produces a passing number."""
        reqs = [
            _req("SR-A", inputs=[_clause("input_voltage", "input", {"typ": 110.0, "unit": "Vac"})],
                 outputs=[_clause("output_voltage", "output")], binding="110V"),
            _req("SR-B", inputs=[_clause("input_voltage", "input", {"typ": 230.0, "unit": "Vac"})],
                 outputs=[_clause("output_voltage", "output")], binding="230V"),
        ]
        plan = plan_flow(_bundle(reqs), load_bindings())
        set_inputs = [s for s in plan.segments[0].steps if s.action == "set_ac_input"] \
            if len(plan.segments) == 1 else [
                s for seg in plan.segments for s in seg.steps if s.action == "set_ac_input"
            ]
        assert len(set_inputs) == 2

    def test_same_load_is_shared_but_different_load_is_not(self) -> None:
        """Coalescing keys on the *value*, not the action.

        Two scenarios both wanting 50% share one load write. Two wanting 50% and
        75% need two — collapsing them would leave the load bank at 50% while
        the 75% case is measured, and that reading looks perfectly plausible.
        """
        same = _req(
            "SR-SAME",
            inputs=[_clause("load", "input", {"percent": 50})],
            outputs=[_clause("output_current", "output")],
            n_scen=2,
        )
        plan = plan_flow(_bundle([same]), load_bindings())
        steps = [s for seg in plan.segments for s in seg.steps]
        assert sum(1 for s in steps if s.action == "set_electronic_load") == 1

        req = RequirementModel(
            requirement_code="SR-DIFF",
            title="SR-DIFF",
            input_conditions=[_clause("load", "input", {"percent": 50})],
            output_conditions=[_clause("output_current", "output")],
            scenarios=[
                ScenarioModel(scenario_id="a", seq=0, bindings={"load_pct": "50"}),
                ScenarioModel(scenario_id="b", seq=1, bindings={"load_pct": "75"}),
            ],
        )
        plan2 = plan_flow(_bundle([req]), load_bindings())
        steps2 = [s for seg in plan2.segments for s in seg.steps]
        # Different scenario bindings -> different segments -> each segment
        # re-applies the load, and the ids differ so the graph stays unambiguous.
        assert sum(1 for s in steps2 if s.action == "set_electronic_load") == 2
        ids = [s.id for s in steps2]
        assert len(ids) == len(set(ids))


# ── ordering & safety ───────────────────────────────────────────────────────


class TestOrdering:
    def test_setup_precedes_measurement(self) -> None:
        """A measurement taken before the source is set reads the wrong state."""
        reqs = [
            _req(
                "SR-1",
                inputs=[_clause("load", "input", {"percent": 50}),
                        _clause("input_voltage", "input", {"typ": 110.0, "unit": "Vac"})],
                outputs=[_clause("output_voltage", "output")],
            )
        ]
        plan = plan_flow(_bundle(reqs), load_bindings())
        actions = [s.action for s in plan.segments[0].steps]
        assert actions.index("set_ac_input") < actions.index("set_electronic_load")
        assert actions.index("set_electronic_load") < actions.index("measure_dc_voltage")

    def test_destructive_segments_come_last(self) -> None:
        """A protection latch left uncleared makes every later measurement wrong.

        So destructive work goes to the end: the damage is bounded to the last
        units of the batch rather than to everything after wherever it landed.
        """
        reqs = [
            _req("SR-OK", inputs=[_clause("input_voltage", "input", {"typ": 110.0})],
                 outputs=[_clause("output_voltage", "output")], binding="a"),
            _req("SR-BAD", inputs=[_clause("fault_stimulus", "input", {"kind": "short"})],
                 outputs=[_clause("protection_action", "output")], binding="b"),
        ]
        plan = plan_flow(_bundle(reqs), load_bindings())
        first_destructive = next(
            i for i, seg in enumerate(plan.segments) if seg.destructive
        )
        assert all(not s.destructive for s in plan.segments[:first_destructive])
        assert plan.segments[-1].destructive

    def test_destructive_segment_is_fenced_by_power_cycle(self) -> None:
        """Recovery before destructive work, or the unit is measured latched."""
        reqs = [
            _req("SR-OK", inputs=[_clause("input_voltage", "input", {"typ": 110.0})],
                 outputs=[_clause("output_voltage", "output")], binding="a"),
            _req("SR-BAD", inputs=[_clause("fault_stimulus", "input", {"kind": "short"})],
                 outputs=[_clause("protection_action", "output")], binding="b"),
        ]
        plan = plan_flow(_bundle(reqs), load_bindings())
        text = plan.to_yaml()
        assert "power_cycle.py" in text
        assert "destructive-segment-fence" in text

    def test_plan_is_deterministic(self) -> None:
        """Same spec in, same plan out — otherwise a review is meaningless."""
        reqs = [
            _req(f"SR-{i}", inputs=[_clause("input_voltage", "input", {"typ": 110.0})],
                 outputs=[_clause("output_voltage", "output")], binding=f"t{i % 3}")
            for i in range(12)
        ]
        b = _bundle(reqs)
        a = plan_flow(b, load_bindings()).to_yaml()
        c = plan_flow(b, load_bindings()).to_yaml()
        assert a == c


# ── honesty about gaps ──────────────────────────────────────────────────────


class TestHonesty:
    def test_unbound_kind_is_reported_not_dropped_silently(self) -> None:
        """A scenario whose condition has no binding must be visible.

        Dropping it produces a plan that looks complete and tests less.
        """
        reqs = [
            _req(
                "SR-X",
                inputs=[ClauseModel(kind="brand_new_kind", text="新条件", role="input")],
                outputs=[_clause("output_voltage", "output")],
            )
        ]
        plan = plan_flow(_bundle(reqs), load_bindings())
        assert plan.unmapped
        assert any("brand_new_kind" in u["missing"] for u in plan.unmapped)
        assert plan.warnings

    def test_empty_plan_is_flagged(self) -> None:
        """A plan with only a clamp and a release looks like coverage and tests
        nothing — that is worse than no plan, so it must be loud."""
        plan = plan_flow(_bundle([]), load_bindings())
        assert plan.step_count == 0
        assert any("禁止上机" in w for w in plan.warnings)

    def test_chamber_time_reported_separately(self) -> None:
        reqs = [
            _req(f"SR-T{i}",
                 inputs=[_clause("temperature", "input", {"typ": 40.0, "unit": "degC"})],
                 outputs=[_clause("output_voltage", "output")], binding=f"t{i}")
            for i in range(12)
        ]
        plan = plan_flow(_bundle(reqs), load_bindings())
        # 12 x 1800s per-unit would be 6 hours of dead time
        assert plan.total_settle_s < 600
        assert plan.batch_settle_s >= 1800
        assert "批次级设置" in plan.to_yaml()


# ── the real parser must accept the output ─────────────────────────────────


def test_generated_plan_parses_with_real_dsl_parser() -> None:
    """End the loop: the plan is only a plan if the platform can read it.

    A generator that emits YAML nothing parses has produced nothing, and that
    failure would otherwise only appear at commissioning — on the line, with a
    batch of boards waiting.
    """
    bundle_path = Path(r"F:\Workspace\ATERag\rag_storage\exports\studio_bundle.json")
    if not bundle_path.exists():
        pytest.skip("需要 ATERag 导出的真实 bundle")
    bundle = BundleModel.model_validate_json(bundle_path.read_text(encoding="utf-8"))
    text = plan_flow(bundle, load_bindings()).to_yaml()

    from ate_platform.dsl.parser import YamlParser

    tmp = REPO / "data" / "_flow_plan_test.yaml"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    try:
        parsed = YamlParser().parse(tmp)
    finally:
        tmp.unlink(missing_ok=True)

    assert parsed.name
    assert parsed.version == "3.2"
    assert len(parsed.steps) > 100
    # every step id must be unique or the dependency graph is ambiguous
    ids = [s.id for s in parsed.steps]
    assert len(ids) == len(set(ids)), "DSL 步骤 id 重复, 依赖图有歧义"
