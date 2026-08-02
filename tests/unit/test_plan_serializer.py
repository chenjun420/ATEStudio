"""Unit tests for PlanSerializer — serialize/deserialize test sequence graphs.

Tests verify round-trip preservation of nodes, edges, step configs, loop
structures, and resources when serializing to YAML depends_on DAG format
and deserializing back.
"""

from __future__ import annotations

import pytest

from ate_platform.serialization.plan_serializer import (
    GraphEdge,
    GraphNode,
    PlanSerializer,
    SequenceGraph,
)
from shared.dsl import (
    LoopType,
    YamlLoop,
    YamlPlan,
    YamlStep,
)


class TestGraphDataStructures:
    """Tests for graph data structures."""

    def test_graph_node_defaults(self) -> None:
        """Test GraphNode default values."""
        node = GraphNode(id="n1", data={"script": "test.py"})
        assert node.id == "n1"
        assert node.data == {"script": "test.py"}
        assert node.parent is None

    def test_graph_node_with_parent(self) -> None:
        """Test GraphNode with parent."""
        node = GraphNode(id="child", data={}, parent="loop1")
        assert node.parent == "loop1"

    def test_graph_edge_defaults(self) -> None:
        """Test GraphEdge default values."""
        edge = GraphEdge(source="a", target="b")
        assert edge.source == "a"
        assert edge.target == "b"
        assert edge.data == {}

    def test_sequence_graph_defaults(self) -> None:
        """Test SequenceGraph default values."""
        graph = SequenceGraph()
        assert graph.nodes == []
        assert graph.edges == []


class TestSerializeSimpleSteps:
    """Tests for serializing simple step graphs."""

    def test_single_step(self) -> None:
        """Test serializing a single step with no edges."""
        serializer = PlanSerializer()
        graph = SequenceGraph(
            nodes=[
                GraphNode(
                    id="step1",
                    data={"id": "step1", "script": "measure.py", "timeout": 120},
                ),
            ],
        )

        result = serializer.serialize_plan(graph, name="Test", version="1.0")

        assert result["name"] == "Test"
        assert result["version"] == "1.0"
        assert len(result["steps"]) == 1
        assert result["steps"][0]["id"] == "step1"
        assert result["steps"][0]["script"] == "measure.py"
        assert result["steps"][0]["timeout"] == 120
        assert result["steps"][0]["depends_on"] == []

    def test_two_steps_with_dependency(self) -> None:
        """Test serializing two steps with a dependency edge."""
        serializer = PlanSerializer()
        graph = SequenceGraph(
            nodes=[
                GraphNode(id="step1", data={"id": "step1", "script": "init.py"}),
                GraphNode(id="step2", data={"id": "step2", "script": "measure.py"}),
            ],
            edges=[GraphEdge(source="step1", target="step2")],
        )

        result = serializer.serialize_plan(graph)

        assert len(result["steps"]) == 2
        step2 = result["steps"][1]
        assert step2["depends_on"] == ["step1"]

    def test_step_with_params_and_resources(self) -> None:
        """Test serializing a step with params and resources."""
        serializer = PlanSerializer()
        graph = SequenceGraph(
            nodes=[
                GraphNode(
                    id="step1",
                    data={
                        "id": "step1",
                        "script": "measure.py",
                        "params": {"channel": 1, "mode": "DCV"},
                        "resources": {"dmm": 1, "gpib_bus": 1},
                        "timeout": 60,
                        "retry": 3,
                        "on_fail": "skip",
                        "export_outputs": True,
                    },
                ),
            ],
        )

        result = serializer.serialize_plan(graph)
        step = result["steps"][0]

        assert step["params"] == {"channel": 1, "mode": "DCV"}
        assert step["resources"] == {"dmm": 1, "gpib_bus": 1}
        assert step["retry"] == 3
        assert step["on_fail"] == "skip"
        assert step["export_outputs"] is True

    def test_default_fields_omitted(self) -> None:
        """Test that default-value fields are omitted from output."""
        serializer = PlanSerializer()
        graph = SequenceGraph(
            nodes=[
                GraphNode(
                    id="step1",
                    data={
                        "id": "step1",
                        "script": "measure.py",
                        "params": {},
                        "resources": {},
                        "retry": 0,
                        "on_fail": None,
                        "export_outputs": False,
                    },
                ),
            ],
        )

        result = serializer.serialize_plan(graph)
        step = result["steps"][0]

        # These should be omitted since they have default values
        assert "params" not in step
        assert "resources" not in step
        assert "retry" not in step
        assert "on_fail" not in step
        assert "export_outputs" not in step


class TestSerializeLoops:
    """Tests for serializing loop containers."""

    def test_for_loop(self) -> None:
        """Test serializing a FOR loop with child steps."""
        serializer = PlanSerializer()
        graph = SequenceGraph(
            nodes=[
                GraphNode(
                    id="loop1",
                    data={
                        "id": "loop1",
                        "loop_type": "FOR",
                        "count": 5,
                        "execution_mode": "SERIAL",
                        "max_iterations": 100,
                    },
                ),
                GraphNode(
                    id="inner1",
                    data={"id": "inner1", "script": "measure.py"},
                    parent="loop1",
                ),
            ],
        )

        result = serializer.serialize_plan(graph)
        loop = result["steps"][0]

        assert loop["loop_type"] == "FOR"
        assert loop["count"] == 5
        assert loop["execution_mode"] == "SERIAL"
        assert "steps" in loop
        assert len(loop["steps"]) == 1
        assert loop["steps"][0]["id"] == "inner1"

    def test_while_loop(self) -> None:
        """Test serializing a WHILE loop."""
        serializer = PlanSerializer()
        graph = SequenceGraph(
            nodes=[
                GraphNode(
                    id="loop1",
                    data={
                        "id": "loop1",
                        "loop_type": "WHILE",
                        "condition": "result.status == 'pending'",
                        "max_iterations": 50,
                    },
                ),
                GraphNode(
                    id="poll",
                    data={"id": "poll", "script": "poll.py"},
                    parent="loop1",
                ),
            ],
        )

        result = serializer.serialize_plan(graph)
        loop = result["steps"][0]

        assert loop["loop_type"] == "WHILE"
        assert loop["condition"] == "result.status == 'pending'"
        assert loop["max_iterations"] == 50

    def test_foreach_loop(self) -> None:
        """Test serializing a FOREACH loop."""
        serializer = PlanSerializer()
        graph = SequenceGraph(
            nodes=[
                GraphNode(
                    id="loop1",
                    data={
                        "id": "loop1",
                        "loop_type": "FOREACH",
                        "collection": "channels",
                        "iterator_var": "ch",
                        "execution_mode": "PARALLEL",
                    },
                ),
                GraphNode(
                    id="measure",
                    data={"id": "measure", "script": "measure.py"},
                    parent="loop1",
                ),
            ],
        )

        result = serializer.serialize_plan(graph)
        loop = result["steps"][0]

        assert loop["loop_type"] == "FOREACH"
        assert loop["collection"] == "channels"
        assert loop["iterator_var"] == "ch"
        assert loop["execution_mode"] == "PARALLEL"

    def test_nested_loops(self) -> None:
        """Test serializing nested loops (loop inside loop)."""
        serializer = PlanSerializer()
        graph = SequenceGraph(
            nodes=[
                GraphNode(
                    id="outer_loop",
                    data={"id": "outer_loop", "loop_type": "FOR", "count": 3},
                ),
                GraphNode(
                    id="inner_loop",
                    data={"id": "inner_loop", "loop_type": "WHILE", "condition": "x < 10"},
                    parent="outer_loop",
                ),
                GraphNode(
                    id="inner_step",
                    data={"id": "inner_step", "script": "measure.py"},
                    parent="inner_loop",
                ),
            ],
        )

        result = serializer.serialize_plan(graph)
        outer = result["steps"][0]

        assert outer["loop_type"] == "FOR"
        assert outer["count"] == 3
        assert "steps" in outer
        inner = outer["steps"][0]
        assert inner["loop_type"] == "WHILE"
        assert inner["condition"] == "x < 10"
        assert "steps" in inner
        assert inner["steps"][0]["id"] == "inner_step"


class TestDeserialize:
    """Tests for deserializing YAML to graph."""

    def test_deserialize_single_step(self) -> None:
        """Test deserializing a single step."""
        serializer = PlanSerializer()
        yaml_data = {
            "name": "Test",
            "version": "1.0",
            "steps": [
                {"id": "step1", "script": "measure.py", "depends_on": []},
            ],
        }

        graph = serializer.deserialize_plan(yaml_data)

        assert len(graph.nodes) == 1
        assert graph.nodes[0].id == "step1"
        assert graph.nodes[0].data["script"] == "measure.py"
        assert graph.nodes[0].parent is None
        assert len(graph.edges) == 0

    def test_deserialize_with_dependencies(self) -> None:
        """Test deserializing steps with depends_on."""
        serializer = PlanSerializer()
        yaml_data = {
            "name": "Test",
            "version": "1.0",
            "steps": [
                {"id": "step1", "script": "init.py", "depends_on": []},
                {"id": "step2", "script": "measure.py", "depends_on": ["step1"]},
            ],
        }

        graph = serializer.deserialize_plan(yaml_data)

        assert len(graph.nodes) == 2
        assert len(graph.edges) == 1
        assert graph.edges[0].source == "step1"
        assert graph.edges[0].target == "step2"

    def test_deserialize_loop(self) -> None:
        """Test deserializing a loop with child steps."""
        serializer = PlanSerializer()
        yaml_data = {
            "name": "Test",
            "version": "1.0",
            "steps": [
                {
                    "id": "loop1",
                    "loop_type": "FOR",
                    "count": 5,
                    "depends_on": [],
                    "steps": [
                        {"id": "inner1", "script": "measure.py", "depends_on": []},
                    ],
                },
            ],
        }

        graph = serializer.deserialize_plan(yaml_data)

        assert len(graph.nodes) == 2
        loop_node = graph.nodes[0]
        assert loop_node.id == "loop1"
        assert loop_node.data["loop_type"] == "FOR"
        assert loop_node.data["count"] == 5

        inner_node = graph.nodes[1]
        assert inner_node.id == "inner1"
        assert inner_node.parent == "loop1"

    def test_deserialize_non_dict_raises(self) -> None:
        """Test deserializing non-dict data raises ValueError."""
        serializer = PlanSerializer()
        with pytest.raises(ValueError, match="must be a dictionary"):
            serializer.deserialize_plan("not a dict")  # type: ignore[arg-type]


class TestRoundTrip:
    """Tests for serialize → deserialize round-trip preservation."""

    def test_simple_round_trip(self) -> None:
        """Test round-trip with a simple 2-step graph."""
        serializer = PlanSerializer()
        original = SequenceGraph(
            nodes=[
                GraphNode(id="step1", data={"id": "step1", "script": "init.py"}),
                GraphNode(id="step2", data={"id": "step2", "script": "measure.py"}),
            ],
            edges=[GraphEdge(source="step1", target="step2")],
        )

        yaml_data = serializer.serialize_plan(original)
        restored = serializer.deserialize_plan(yaml_data)

        # Verify nodes match
        assert len(restored.nodes) == len(original.nodes)
        for orig, rest in zip(original.nodes, restored.nodes, strict=True):
            assert rest.id == orig.id
            assert rest.data["script"] == orig.data["script"]

        # Verify edges match
        assert len(restored.edges) == len(original.edges)
        assert restored.edges[0].source == original.edges[0].source
        assert restored.edges[0].target == original.edges[0].target

    def test_complex_round_trip(self) -> None:
        """Test round-trip with complex graph: 5+ nodes, loops, conditions, resources."""
        serializer = PlanSerializer()
        original = SequenceGraph(
            nodes=[
                GraphNode(
                    id="init",
                    data={
                        "id": "init",
                        "script": "init.py",
                        "params": {"config": "production"},
                        "resources": {"gpib_bus": 1},
                        "timeout": 30,
                        "retry": 2,
                        "on_fail": "stop",
                        "export_outputs": False,
                    },
                ),
                GraphNode(
                    id="measure",
                    data={
                        "id": "measure",
                        "script": "measure.py",
                        "params": {"channel": 1, "mode": "DCV"},
                        "resources": {"dmm": 1, "gpib_bus": 1},
                        "timeout": 60,
                        "retry": 3,
                        "on_fail": "skip",
                        "export_outputs": True,
                        "skip_if": "config.skip_measure == True",
                    },
                ),
                GraphNode(
                    id="for_loop",
                    data={
                        "id": "for_loop",
                        "loop_type": "FOR",
                        "count": 5,
                        "execution_mode": "SERIAL",
                        "max_iterations": 100,
                        "skip_if": None,
                    },
                ),
                GraphNode(
                    id="inner_measure",
                    data={
                        "id": "inner_measure",
                        "script": "inner_measure.py",
                        "params": {"voltage_range": 10.0},
                        "timeout": 15,
                    },
                    parent="for_loop",
                ),
                GraphNode(
                    id="while_loop",
                    data={
                        "id": "while_loop",
                        "loop_type": "WHILE",
                        "condition": "result.stable == False",
                        "max_iterations": 50,
                    },
                ),
                GraphNode(
                    id="poll",
                    data={
                        "id": "poll",
                        "script": "poll.py",
                        "timeout": 5,
                        "retry": 5,
                    },
                    parent="while_loop",
                ),
                GraphNode(
                    id="cleanup",
                    data={
                        "id": "cleanup",
                        "script": "cleanup.py",
                        "timeout": 10,
                        "on_fail": "ignore",
                    },
                ),
            ],
            edges=[
                GraphEdge(source="init", target="measure"),
                GraphEdge(source="measure", target="for_loop"),
                GraphEdge(source="for_loop", target="while_loop"),
                GraphEdge(source="while_loop", target="cleanup"),
            ],
        )

        yaml_data = serializer.serialize_plan(original, name="Complex Plan", version="2.0")
        restored = serializer.deserialize_plan(yaml_data)

        # Verify name and version
        assert yaml_data["name"] == "Complex Plan"
        assert yaml_data["version"] == "2.0"

        # Verify node count (7 nodes: 5 top-level + 2 children)
        assert len(restored.nodes) == len(original.nodes)
        assert len(restored.nodes) == 7

        # Verify edges
        assert len(restored.edges) == len(original.edges)
        assert len(restored.edges) == 4

        # Verify specific node properties
        rest_by_id = {n.id: n for n in restored.nodes}

        # Check init step
        assert rest_by_id["init"].data["script"] == "init.py"
        assert rest_by_id["init"].data["params"] == {"config": "production"}
        assert rest_by_id["init"].data["resources"] == {"gpib_bus": 1}
        assert rest_by_id["init"].data["timeout"] == 30
        assert rest_by_id["init"].data["retry"] == 2
        assert rest_by_id["init"].data["on_fail"] == "stop"

        # Check measure step
        assert rest_by_id["measure"].data["script"] == "measure.py"
        assert rest_by_id["measure"].data["params"] == {"channel": 1, "mode": "DCV"}
        assert rest_by_id["measure"].data["resources"] == {"dmm": 1, "gpib_bus": 1}
        assert rest_by_id["measure"].data["export_outputs"] is True
        assert rest_by_id["measure"].data["skip_if"] == "config.skip_measure == True"

        # Check for_loop
        assert rest_by_id["for_loop"].data["loop_type"] == "FOR"
        assert rest_by_id["for_loop"].data["count"] == 5
        assert rest_by_id["for_loop"].data["execution_mode"] == "SERIAL"
        assert rest_by_id["for_loop"].data["max_iterations"] == 100

        # Check parent-child relationships
        assert rest_by_id["inner_measure"].parent == "for_loop"
        assert rest_by_id["poll"].parent == "while_loop"
        assert rest_by_id["for_loop"].parent is None
        assert rest_by_id["while_loop"].parent is None

        # Check while_loop
        assert rest_by_id["while_loop"].data["loop_type"] == "WHILE"
        assert rest_by_id["while_loop"].data["condition"] == "result.stable == False"
        assert rest_by_id["while_loop"].data["max_iterations"] == 50

        # Check cleanup
        assert rest_by_id["cleanup"].data["script"] == "cleanup.py"
        assert rest_by_id["cleanup"].data["on_fail"] == "ignore"

        # Verify edges round-trip
        rest_edges = {(e.source, e.target) for e in restored.edges}
        orig_edges = {(e.source, e.target) for e in original.edges}
        assert rest_edges == orig_edges

    def test_yaml_string_round_trip(self) -> None:
        """Test round-trip via YAML string."""
        serializer = PlanSerializer()
        original = SequenceGraph(
            nodes=[
                GraphNode(id="s1", data={"id": "s1", "script": "a.py"}),
                GraphNode(id="s2", data={"id": "s2", "script": "b.py"}),
            ],
            edges=[GraphEdge(source="s1", target="s2")],
        )

        yaml_str = serializer.to_yaml_string(original, name="YAML Test")
        restored = serializer.from_yaml_string(yaml_str)

        assert len(restored.nodes) == 2
        assert len(restored.edges) == 1
        assert restored.nodes[0].data["script"] == "a.py"
        assert restored.nodes[1].data["script"] == "b.py"

    def test_loop_with_internal_dependencies(self) -> None:
        """Test round-trip with edges inside a loop."""
        serializer = PlanSerializer()
        original = SequenceGraph(
            nodes=[
                GraphNode(
                    id="loop1",
                    data={"id": "loop1", "loop_type": "FOR", "count": 3},
                ),
                GraphNode(
                    id="setup",
                    data={"id": "setup", "script": "setup.py"},
                    parent="loop1",
                ),
                GraphNode(
                    id="measure",
                    data={"id": "measure", "script": "measure.py"},
                    parent="loop1",
                ),
            ],
            edges=[
                GraphEdge(source="setup", target="measure"),  # internal edge
            ],
        )

        yaml_data = serializer.serialize_plan(original)
        restored = serializer.deserialize_plan(yaml_data)

        # Both child nodes should be present
        assert len(restored.nodes) == 3
        # Internal edge should be preserved
        assert len(restored.edges) == 1
        assert restored.edges[0].source == "setup"
        assert restored.edges[0].target == "measure"


class TestYamlPlanInterop:
    """Tests for interop with DSL YamlPlan types."""

    def test_from_yaml_plan(self) -> None:
        """Test converting YamlPlan to SequenceGraph."""
        serializer = PlanSerializer()
        plan = YamlPlan(
            name="Test",
            version="1.0",
            steps=[
                YamlStep(id="s1", script="a.py"),
                YamlStep(id="s2", script="b.py", preconditions=["s1"]),
            ],
        )

        graph = serializer.from_yaml_plan(plan)

        assert len(graph.nodes) == 2
        assert len(graph.edges) >= 1

    def test_to_yaml_plan(self) -> None:
        """Test converting SequenceGraph to YamlPlan."""
        serializer = PlanSerializer()
        graph = SequenceGraph(
            nodes=[
                GraphNode(id="s1", data={"id": "s1", "script": "a.py"}),
                GraphNode(id="s2", data={"id": "s2", "script": "b.py"}),
            ],
            edges=[GraphEdge(source="s1", target="s2")],
        )

        plan = serializer.to_yaml_plan(graph, name="Test", version="1.0")

        assert plan.name == "Test"
        assert plan.version == "1.0"
        assert len(plan.steps) == 2

    def test_yaml_plan_with_loop_round_trip(self) -> None:
        """Test YamlPlan → graph → YamlPlan with a loop."""
        serializer = PlanSerializer()
        original_plan = YamlPlan(
            name="Loop Test",
            version="2.0",
            steps=[
                YamlStep(id="init", script="init.py"),
                YamlLoop(
                    id="loop1",
                    loop_type=LoopType.FOR,
                    count=5,
                    steps=[
                        YamlStep(id="inner", script="inner.py"),
                    ],
                ),
            ],
        )

        graph = serializer.from_yaml_plan(original_plan)
        restored_plan = serializer.to_yaml_plan(graph, name="Loop Test", version="2.0")

        assert restored_plan.name == "Loop Test"
        assert len(restored_plan.steps) == 2

        # Check loop is preserved
        loop_step = restored_plan.steps[1]
        assert isinstance(loop_step, YamlLoop)
        assert loop_step.loop_type == LoopType.FOR
        assert loop_step.count == 5
        assert len(loop_step.steps) == 1
        assert loop_step.steps[0].id == "inner"


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_graph(self) -> None:
        """Test serializing an empty graph."""
        serializer = PlanSerializer()
        graph = SequenceGraph()

        result = serializer.serialize_plan(graph)

        assert result["steps"] == []

    def test_step_with_skip_if(self) -> None:
        """Test serializing a step with skip_if."""
        serializer = PlanSerializer()
        graph = SequenceGraph(
            nodes=[
                GraphNode(
                    id="s1",
                    data={
                        "id": "s1",
                        "script": "test.py",
                        "skip_if": "env.skip == true",
                    },
                ),
            ],
        )

        result = serializer.serialize_plan(graph)
        assert result["steps"][0]["skip_if"] == "env.skip == true"

    def test_multiple_dependencies(self) -> None:
        """Test a step depending on multiple predecessors."""
        serializer = PlanSerializer()
        graph = SequenceGraph(
            nodes=[
                GraphNode(id="a", data={"id": "a", "script": "a.py"}),
                GraphNode(id="b", data={"id": "b", "script": "b.py"}),
                GraphNode(id="c", data={"id": "c", "script": "c.py"}),
            ],
            edges=[
                GraphEdge(source="a", target="c"),
                GraphEdge(source="b", target="c"),
            ],
        )

        result = serializer.serialize_plan(graph)
        step_c = result["steps"][2]
        assert sorted(step_c["depends_on"]) == ["a", "b"]

    def test_no_dependencies_round_trip(self) -> None:
        """Test that steps with no dependencies have empty depends_on."""
        serializer = PlanSerializer()
        graph = SequenceGraph(
            nodes=[
                GraphNode(id="s1", data={"id": "s1", "script": "a.py"}),
            ],
        )

        yaml_data = serializer.serialize_plan(graph)
        assert yaml_data["steps"][0]["depends_on"] == []

        restored = serializer.deserialize_plan(yaml_data)
        assert len(restored.edges) == 0
