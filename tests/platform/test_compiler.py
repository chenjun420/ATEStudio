"""Tests for SequenceCompiler flat-DAG expansion (设计文档 §6.3).

Covers:
- Loop expansion (FOR count) with ``{child}_iter{n}`` ids and per-iteration edges
- Branch flattening to a single branch_eval node carrying then_ids/else_ids
- Subsequence inlining with dotted ``{parent}.{child}`` prefixes
- depends_on remapping to expanded ids (incl. container terminal resolution)
- CircularDependencyError for self-dependent / circular plans
- Real-surface compile of tests/fixtures/plan_v32_production.yaml
"""

from pathlib import Path

import pytest

from ate_platform.dsl.parser import YamlParser
from ate_platform.scheduler.compiler import (
    CircularDependencyError,
    CompiledStep,
    SequenceCompiler,
)
from shared.dsl import LoopType, StepType, YamlLoop, YamlPlan, YamlStep

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def make_plan(*items: YamlStep | YamlLoop) -> YamlPlan:
    """Build a minimal valid YamlPlan from steps/loops."""
    return YamlPlan(name="t", version="3.2", scope={"name": "testing"}, steps=list(items))


def script_step(step_id: str, depends_on: list[str] | None = None, **kwargs) -> YamlStep:
    """Build a plain script step with sensible defaults."""
    return YamlStep(id=step_id, script=f"{step_id}.py", depends_on=depends_on or [], **kwargs)


def by_id(steps: list[CompiledStep]) -> dict[str, CompiledStep]:
    return {s.id: s for s in steps}


class TestLeafExpansion:
    """Plain script/action/barrier/fixture_control steps pass through 1:1."""

    def test_flat_plan_preserves_order_and_fields(self) -> None:
        plan = make_plan(
            YamlStep(
                id="a",
                type=StepType.ACTION,
                script="a.py",
                params={"v": 1},
                timeout=10,
                retry=2,
                on_failure="continue",
                uut_affinity="any",
            ),
            script_step("b", depends_on=["a"]),
        )

        compiled = SequenceCompiler().compile(plan)

        assert [s.id for s in compiled] == ["a", "b"]
        a = compiled[0]
        assert a.type == StepType.ACTION
        assert a.script == "a.py"
        assert a.params == {"v": 1}
        assert a.timeout == 10
        assert a.retry == 2
        assert a.on_failure == "continue"
        assert a.uut_affinity == "any"
        assert a.source_step_id == "a"
        assert a.iteration is None

    def test_barrier_and_fixture_control_fields_carried(self) -> None:
        plan = make_plan(
            YamlStep(id="bar", type=StepType.BARRIER, barrier_name="all_powered_on"),
            YamlStep(
                id="fc",
                type=StepType.FIXTURE_CONTROL,
                action="clamp",
                fixture_id="fx1",
            ),
        )

        compiled = by_id(SequenceCompiler().compile(plan))

        assert compiled["bar"].barrier_name == "all_powered_on"
        assert compiled["fc"].action == "clamp"
        assert compiled["fc"].fixture_id == "fx1"

    def test_depends_on_forward_reference_resolves(self) -> None:
        plan = make_plan(script_step("a", depends_on=["b"]), script_step("b"))

        compiled = by_id(SequenceCompiler().compile(plan))

        assert compiled["a"].depends_on == ["b"]

    def test_compile_is_deterministic_across_runs(self) -> None:
        loop = YamlLoop(
            id="lp",
            loop_type=LoopType.FOR,
            count=2,
            steps=[script_step("x")],
        )
        plan = make_plan(loop, script_step("after", depends_on=["lp"]))

        first = [s.id for s in SequenceCompiler().compile(plan)]
        second = [s.id for s in SequenceCompiler().compile(plan)]

        assert first == second


class TestLoopExpansion:
    """FOR loops expand count times; child ids get ``_iter{n}`` suffixes."""

    def test_loop_count_expands_iterations_in_order(self) -> None:
        loop = YamlLoop(
            id="lp", loop_type=LoopType.FOR,
            count=3, steps=[script_step("x"), script_step("y")],
        )
        plan = make_plan(loop)

        compiled = SequenceCompiler().compile(plan)

        assert [s.id for s in compiled] == [
            "x_iter0", "y_iter0", "x_iter1", "y_iter1", "x_iter2", "y_iter2",
        ]

    def test_expanded_nodes_carry_iteration_and_source(self) -> None:
        loop = YamlLoop(
            id="lp", loop_type=LoopType.FOR,
            count=2, steps=[script_step("x")],
        )
        plan = make_plan(loop)

        compiled = SequenceCompiler().compile(plan)

        assert compiled[1].id == "x_iter1"
        assert compiled[1].iteration == 1
        assert compiled[1].source_step_id == "x"

    def test_internal_dep_remapped_within_same_iteration(self) -> None:
        loop = YamlLoop(
            id="lp", loop_type=LoopType.FOR,
            count=3, steps=[script_step("set"), script_step("measure", depends_on=["set"])],
        )
        plan = make_plan(loop)

        compiled = by_id(SequenceCompiler().compile(plan))

        assert compiled["measure_iter2"].depends_on == ["set_iter2"]

    def test_loop_deps_inherited_by_dependency_free_children(self) -> None:
        loop = YamlLoop(
            id="lp", loop_type=LoopType.FOR,
            count=2, depends_on=["power"], steps=[script_step("x"), script_step("y", depends_on=["x"])],
        )
        plan = make_plan(script_step("power"), loop)

        compiled = by_id(SequenceCompiler().compile(plan))

        assert compiled["x_iter0"].depends_on == ["power"]
        assert compiled["y_iter0"].depends_on == ["x_iter0"]
        assert compiled["x_iter1"].depends_on == ["power"]

    def test_edge_to_loop_resolves_to_terminal_node(self) -> None:
        loop = YamlLoop(
            id="lp", loop_type=LoopType.FOR,
            count=2, steps=[script_step("x"), script_step("y")],
        )
        plan = make_plan(loop, script_step("after", depends_on=["lp"]))

        compiled = by_id(SequenceCompiler().compile(plan))

        assert compiled["after"].depends_on == ["y_iter1"]

    def test_nested_loop_suffix_composition(self) -> None:
        inner = YamlLoop(
            id="in", loop_type=LoopType.FOR,
            count=2, steps=[script_step("x")],
        )
        outer = YamlLoop(
            id="out", loop_type=LoopType.FOR,
            count=2, steps=[inner],
        )
        plan = make_plan(outer)

        compiled = SequenceCompiler().compile(plan)

        assert [s.id for s in compiled] == [
            "x_iter0_iter0", "x_iter0_iter1", "x_iter1_iter0", "x_iter1_iter1",
        ]
        # iteration reflects the innermost enclosing loop index
        assert compiled[3].iteration == 1
        assert compiled[3].source_step_id == "x"

    def test_while_loop_deferred_as_single_loop_node(self) -> None:
        loop = YamlLoop(
            id="wl", loop_type=LoopType.WHILE,
            condition="${scope.counter} < 5", steps=[script_step("x")],
        )
        plan = make_plan(loop, script_step("after", depends_on=["wl"]))

        compiled = by_id(SequenceCompiler().compile(plan))

        assert list(compiled) == ["wl", "after"]
        assert compiled["wl"].type == StepType.LOOP
        assert compiled["wl"].condition == "${scope.counter} < 5"
        assert compiled["after"].depends_on == ["wl"]

    def test_for_loop_iterator_binding_recorded_per_iteration(self) -> None:
        loop = YamlLoop(
            id="lp", loop_type=LoopType.FOR,
            count=2, iterator_var="i", steps=[script_step("x")],
        )
        plan = make_plan(loop)

        compiler = SequenceCompiler()
        compiler.compile(plan)

        placeholders = [(b.step_id, b.placeholder, b.iteration) for b in compiler.iterator_bindings]
        assert ("x_iter0", "${i}", 0) in placeholders
        assert ("x_iter1", "${i}", 1) in placeholders

    def test_foreach_loop_deferred_with_placeholder_bindings(self) -> None:
        loop = YamlLoop(
            id="fl", loop_type=LoopType.FOREACH,
            collection="${scope.items}", iterator_var="item",
            steps=[YamlStep(id="x", script="x.py", params={"target": "${item}"})],
        )
        plan = make_plan(loop)

        compiler = SequenceCompiler()
        compiled = by_id(compiler.compile(plan))

        # Collection length is unknown without resolving values — defer as one node.
        assert list(compiled) == ["fl"]
        assert compiled["fl"].type == StepType.LOOP
        binding = next(b for b in compiler.iterator_bindings if b.placeholder == "${item}")
        assert binding.step_id == "fl"
        assert binding.source == "${scope.items}"
        assert binding.iteration is None


class TestBranchFlattening:
    """Branch containers emit one branch_eval node carrying then/else ids."""

    def make_branch_plan(self) -> YamlPlan:
        branch = YamlStep(
            id="chk",
            type=StepType.BRANCH,
            params={
                "condition": "${scope.mode} == 'fast'",
                "then": ["fast_path"],
                "else": ["slow_path"],
            },
        )
        return make_plan(script_step("fast_path"), script_step("slow_path"), branch)

    def test_branch_emits_single_eval_node(self) -> None:
        compiled = by_id(SequenceCompiler().compile(self.make_branch_plan()))

        assert sorted(compiled) == ["chk", "fast_path", "slow_path"]
        assert compiled["chk"].type == StepType.BRANCH

    def test_branch_condition_promoted_from_params(self) -> None:
        compiled = by_id(SequenceCompiler().compile(self.make_branch_plan()))

        assert compiled["chk"].condition == "${scope.mode} == 'fast'"
        assert "condition" not in compiled["chk"].params
        assert "then" not in compiled["chk"].params
        assert "else" not in compiled["chk"].params

    def test_branch_then_else_ids_carried(self) -> None:
        compiled = by_id(SequenceCompiler().compile(self.make_branch_plan()))

        assert compiled["chk"].then_ids == ["fast_path"]
        assert compiled["chk"].else_ids == ["slow_path"]

    def test_branch_target_inside_loop_resolves_to_last_iteration(self) -> None:
        loop = YamlLoop(
            id="lp", loop_type=LoopType.FOR,
            count=2, steps=[script_step("retry_once")],
        )
        branch = YamlStep(
            id="chk", type=StepType.BRANCH,
            params={"condition": "True", "then": ["retry_once"], "else": []},
        )
        plan = make_plan(loop, branch)

        compiled = by_id(SequenceCompiler().compile(plan))

        assert compiled["chk"].then_ids == ["retry_once_iter1"]


class TestSubsequenceInlining:
    """Subsequence children inline with dotted ``{parent}.{child}`` prefixes."""

    def test_children_prefixed_and_container_dissolved(self) -> None:
        sub = YamlStep(
            id="seq", type=StepType.SUBSEQUENCE,
            params={"steps": [script_step("a"), script_step("b")]},
        )
        plan = make_plan(sub)

        compiled = SequenceCompiler().compile(plan)

        assert [s.id for s in compiled] == ["seq.a", "seq.b"]
        assert all(s.type == StepType.SCRIPT for s in compiled)
        assert compiled[0].source_step_id == "a"

    def test_edge_to_subsequence_resolves_to_terminal_child(self) -> None:
        sub = YamlStep(
            id="seq", type=StepType.SUBSEQUENCE,
            params={"steps": [script_step("a"), script_step("b")]},
        )
        plan = make_plan(sub, script_step("after", depends_on=["seq"]))

        compiled = by_id(SequenceCompiler().compile(plan))

        assert compiled["after"].depends_on == ["seq.b"]

    def test_subsequence_deps_inherited_by_dependency_free_children(self) -> None:
        sub = YamlStep(
            id="seq", type=StepType.SUBSEQUENCE, depends_on=["power"],
            params={"steps": [script_step("a"), script_step("b", depends_on=["a"])]},
        )
        plan = make_plan(script_step("power"), sub)

        compiled = by_id(SequenceCompiler().compile(plan))

        assert compiled["seq.a"].depends_on == ["power"]
        assert compiled["seq.b"].depends_on == ["seq.a"]

    def test_nested_subsequence_prefix_composition(self) -> None:
        inner = YamlStep(id="s2", type=StepType.SUBSEQUENCE, params={"steps": [script_step("x")]})
        outer = YamlStep(id="s1", type=StepType.SUBSEQUENCE, params={"steps": [inner]})
        plan = make_plan(outer)

        compiled = SequenceCompiler().compile(plan)

        assert [s.id for s in compiled] == ["s1.s2.x"]

    def test_loop_inside_subsequence_gets_prefix_and_suffix(self) -> None:
        loop = YamlLoop(
            id="lp", loop_type=LoopType.FOR,
            count=2, steps=[script_step("x")],
        )
        sub = YamlStep(id="seq", type=StepType.SUBSEQUENCE, params={"steps": [loop]})
        plan = make_plan(sub)

        compiled = SequenceCompiler().compile(plan)

        assert [s.id for s in compiled] == ["seq.x_iter0", "seq.x_iter1"]


class TestCycleDetection:
    """Circular depends_on graphs raise CircularDependencyError after remapping."""

    def test_self_dependent_step_raises(self) -> None:
        plan = make_plan(script_step("a", depends_on=["a"]))

        with pytest.raises(CircularDependencyError):
            SequenceCompiler().compile(plan)

    def test_circular_pair_raises(self) -> None:
        plan = make_plan(script_step("a", depends_on=["b"]), script_step("b", depends_on=["a"]))

        with pytest.raises(CircularDependencyError):
            SequenceCompiler().compile(plan)

    def test_cycle_inside_loop_detected_after_remap(self) -> None:
        loop = YamlLoop(
            id="lp", loop_type=LoopType.FOR,
            count=2,
            steps=[script_step("a", depends_on=["b"]), script_step("b", depends_on=["a"])],
        )
        plan = make_plan(loop)

        with pytest.raises(CircularDependencyError):
            SequenceCompiler().compile(plan)

    def test_unknown_dependency_raises_value_error(self) -> None:
        plan = make_plan(script_step("a", depends_on=["ghost"]))

        with pytest.raises(ValueError, match="ghost"):
            SequenceCompiler().compile(plan)


class TestExportOutputsAndFixture:
    """export_outputs propagation and the real v3.2 production fixture."""

    def test_export_outputs_propagates_from_inner_step(self) -> None:
        loop = YamlLoop(
            id="lp", loop_type=LoopType.FOR,
            count=2, steps=[YamlStep(id="x", script="x.py", export_outputs=True)],
        )
        plan = make_plan(loop)

        compiled = by_id(SequenceCompiler().compile(plan))

        assert compiled["x_iter0"].export_outputs is True
        assert compiled["x_iter1"].export_outputs is True

    def test_compile_production_fixture(self) -> None:
        plan = YamlParser().parse(FIXTURES_DIR / "plan_v32_production.yaml")

        compiled = SequenceCompiler().compile(plan)

        # fixture_clamp, power_on, sync_power_on + 5 iterations x (set_load, measure)
        # + fixture_release = 14 flat nodes.
        assert len(compiled) == 14
        assert [s.id for s in compiled[:5]] == [
            "fixture_clamp", "power_on", "sync_power_on", "set_load_iter0", "measure_iter0",
        ]

    def test_production_fixture_edge_remapping(self) -> None:
        plan = YamlParser().parse(FIXTURES_DIR / "plan_v32_production.yaml")

        compiled = by_id(SequenceCompiler().compile(plan))

        assert compiled["power_on"].depends_on == ["fixture_clamp"]
        assert compiled["sync_power_on"].depends_on == ["power_on"]
        assert compiled["set_load_iter4"].depends_on == ["sync_power_on"]
        assert compiled["fixture_release"].depends_on == ["measure_iter4"]

    def test_production_fixture_step_attributes_carried(self) -> None:
        plan = YamlParser().parse(FIXTURES_DIR / "plan_v32_production.yaml")

        compiled = by_id(SequenceCompiler().compile(plan))

        measure = compiled["measure_iter3"]
        assert measure.timeout == 10
        assert measure.retry == 2
        assert measure.on_failure == "continue"
        assert compiled["sync_power_on"].barrier_name == "all_powered_on"
        assert compiled["fixture_clamp"].action == "clamp"
        assert compiled["fixture_clamp"].fixture_id == "fixture_ps_12v5a_v1"
        assert compiled["power_on"].uut_affinity == "any"
