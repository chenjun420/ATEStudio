"""PlanSerializer — serialize/deserialize test sequence graphs as YAML DAG.

Converts an AntV X6-compatible graph representation (nodes + edges) to a
declarative YAML format using `depends_on` DAG edges, inspired by TofuPilot's
declarative sequence model. Round-trip (serialize → deserialize) preserves
nodes, edges, step configs, loop structures, and resources exactly.

Graph representation:
    The serializer works with plain dicts representing X6 graph data:
    {
        "nodes": [
            {"id": "step1", "data": {...}, "parent": None},
            {"id": "inner", "data": {...}, "parent": "loop1"},
            ...
        ],
        "edges": [
            {"source": "step1", "target": "step2", "data": {...}},
            ...
        ]
    }

YAML output format (depends_on DAG):
    name: My Sequence
    version: "3.0"
    steps:
      - id: step1
        script: measure.py
        params: {...}
        resources: {...}
        timeout: 60
        depends_on: []           # empty = no dependencies
      - id: step2
        script: analyze.py
        depends_on:
          - step1
      - id: loop1
        loop_type: FOR
        count: 5
        depends_on:
          - step2
        steps:
          - id: inner_step
            script: inner.py
            depends_on: []
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from shared.dsl import (
    ExecutionMode,
    LoopType,
    YamlLoop,
    YamlPlan,
    YamlStep,
)

# ---------------------------------------------------------------------------
# Graph data structures
# ---------------------------------------------------------------------------

@dataclass
class GraphNode:
    """A node in the test sequence graph.

    Attributes:
        id: Unique node identifier.
        data: Node configuration data (step or loop config).
        parent: Parent node ID (None for top-level nodes).
    """

    id: str
    data: dict[str, Any]
    parent: str | None = None


@dataclass
class GraphEdge:
    """An edge in the test sequence graph (dependency relationship).

    Attributes:
        source: Source node ID (predecessor).
        target: Target node ID (dependent).
        data: Optional edge metadata.
    """

    source: str
    target: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class SequenceGraph:
    """A complete test sequence graph.

    Attributes:
        nodes: List of graph nodes.
        edges: List of dependency edges.
    """

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PlanSerializer
# ---------------------------------------------------------------------------

class PlanSerializer:
    """Serialize and deserialize test sequence graphs as YAML DAG.

    Converts between SequenceGraph (AntV X6-compatible) and YAML with
    `depends_on` DAG format. Supports:
    - Script steps (YamlStep) with params, resources, timeout, retry, etc.
    - Loop containers (YamlLoop) with FOR/WHILE/FOREACH types
    - Dependency edges (preconditions → depends_on)
    - Nested loops (parent-child node relationships)
    - Round-trip preservation of all node/edge/step/loop properties
    """

    # Keys that are excluded from YAML output when they have default values
    _STEP_DEFAULT_OMIT: dict[str, Any] = {
        "params": {},
        "preconditions": [],
        "resources": {},
        "retry": 0,
        "on_fail": None,
        "export_outputs": False,
        "skip_if": None,
        "skip_reason": None,
    }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def serialize_plan(
        self,
        graph: SequenceGraph,
        name: str = "Untitled Sequence",
        version: str = "3.0",
    ) -> dict[str, Any]:
        """Serialize a SequenceGraph to a YAML-compatible dict with depends_on DAG.

        Args:
            graph: The sequence graph to serialize.
            name: Plan name.
            version: Plan version.

        Returns:
            Dict suitable for yaml.dump() — contains name, version, steps
            with depends_on lists.
        """
        # Build edge map: target → list of source IDs (dependencies)
        dep_map: dict[str, list[str]] = {}
        for edge in graph.edges:
            dep_map.setdefault(edge.target, []).append(edge.source)

        # Separate top-level nodes from child nodes
        child_ids = {n.id for n in graph.nodes if n.parent is not None}
        top_level = [n for n in graph.nodes if n.parent is None]

        # Build steps from top-level nodes
        steps: list[dict[str, Any]] = []
        for node in top_level:
            step_dict = self._node_to_yaml(node, graph, dep_map, child_ids)
            steps.append(step_dict)

        return {
            "name": name,
            "version": version,
            "steps": steps,
        }

    def deserialize_plan(self, yaml_data: dict[str, Any]) -> SequenceGraph:
        """Deserialize a YAML dict (with depends_on DAG) back to a SequenceGraph.

        Args:
            yaml_data: Dict parsed from YAML (must have "steps" key).

        Returns:
            SequenceGraph with nodes and edges reconstructed from depends_on.

        Raises:
            ValueError: If yaml_data is not a dict.
        """
        if not isinstance(yaml_data, dict):
            msg = "YAML content must be a dictionary"
            raise ValueError(msg)

        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []

        steps_data = yaml_data.get("steps", [])
        for step_data in steps_data:
            self._yaml_to_node(
                step_data,
                parent=None,
                nodes=nodes,
                edges=edges,
            )

        return SequenceGraph(nodes=nodes, edges=edges)

    def to_yaml_string(
        self,
        graph: SequenceGraph,
        name: str = "Untitled Sequence",
        version: str = "3.0",
    ) -> str:
        """Serialize a graph directly to a YAML string.

        Args:
            graph: The sequence graph to serialize.
            name: Plan name.
            version: Plan version.

        Returns:
            YAML string with depends_on DAG format.
        """
        data = self.serialize_plan(graph, name=name, version=version)
        result: str = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
        return result

    def from_yaml_string(self, yaml_str: str) -> SequenceGraph:
        """Deserialize a YAML string to a SequenceGraph.

        Args:
            yaml_str: YAML string in depends_on DAG format.

        Returns:
            Reconstructed SequenceGraph.
        """
        data: dict[str, Any] = yaml.safe_load(yaml_str)
        if not isinstance(data, dict):
            msg = "YAML content must be a dictionary"
            raise ValueError(msg)
        return self.deserialize_plan(data)

    # ------------------------------------------------------------------
    # Serialization helpers (node → YAML dict)
    # ------------------------------------------------------------------

    def _node_to_yaml(
        self,
        node: GraphNode,
        graph: SequenceGraph,
        dep_map: dict[str, list[str]],
        child_ids: set[str],
    ) -> dict[str, Any]:
        """Convert a GraphNode to a YAML-compatible step/loop dict.

        Args:
            node: The node to convert.
            graph: Full graph (for finding child nodes of loops).
            dep_map: Dependency map (target → sources).
            child_ids: Set of node IDs that are children of a loop.

        Returns:
            Dict representation of the step or loop.
        """
        data = node.data
        depends_on = sorted(dep_map.get(node.id, []))

        # Detect loop vs step
        if "loop_type" in data:
            return self._loop_to_yaml(node, graph, dep_map, child_ids, depends_on)
        return self._step_to_yaml(node, depends_on)

    def _step_to_yaml(
        self,
        node: GraphNode,
        depends_on: list[str],
    ) -> dict[str, Any]:
        """Convert a script step node to a YAML dict.

        Args:
            node: The step node (data must have id, script).
            depends_on: List of dependency node IDs.

        Returns:
            Dict with step properties and depends_on.
        """
        data = node.data
        result: dict[str, Any] = {
            "id": data.get("id", node.id),
            "script": data.get("script", ""),
            "depends_on": depends_on,
        }

        # Include optional fields only if non-default
        optional_fields = [
            "params",
            "resources",
            "timeout",
            "retry",
            "on_fail",
            "export_outputs",
            "skip_if",
            "skip_reason",
        ]
        for key in optional_fields:
            val = data.get(key)
            default = self._STEP_DEFAULT_OMIT.get(key)
            if val is not None and val != default:
                result[key] = val

        return result

    def _loop_to_yaml(
        self,
        node: GraphNode,
        graph: SequenceGraph,
        dep_map: dict[str, list[str]],
        child_ids: set[str],
        depends_on: list[str],
    ) -> dict[str, Any]:
        """Convert a loop container node to a YAML dict.

        Args:
            node: The loop node.
            graph: Full graph (for finding child nodes).
            dep_map: Dependency map.
            child_ids: Set of child node IDs.
            depends_on: List of dependency node IDs for this loop.

        Returns:
            Dict with loop properties, nested steps, and depends_on.
        """
        data = node.data
        result: dict[str, Any] = {
            "id": data.get("id", node.id),
            "loop_type": data.get("loop_type", "FOR"),
            "depends_on": depends_on,
        }

        # Loop-specific optional fields
        loop_optional = [
            "count",
            "condition",
            "collection",
            "iterator_var",
            "execution_mode",
            "max_iterations",
            "skip_if",
            "skip_reason",
        ]
        for key in loop_optional:
            val = data.get(key)
            if val is not None:
                result[key] = val

        # Serialize child nodes (nodes whose parent is this loop)
        child_nodes = [n for n in graph.nodes if n.parent == node.id]
        if child_nodes:
            nested_steps: list[dict[str, Any]] = []
            for child in child_nodes:
                child_dep_map: dict[str, list[str]] = {}
                # Only include edges where both source and target are children
                child_id_set = {c.id for c in child_nodes}
                for edge in graph.edges:
                    if edge.source in child_id_set and edge.target in child_id_set:
                        child_dep_map.setdefault(edge.target, []).append(edge.source)
                child_dict = self._node_to_yaml(child, graph, child_dep_map, child_ids)
                nested_steps.append(child_dict)
            result["steps"] = nested_steps

        return result

    # ------------------------------------------------------------------
    # Deserialization helpers (YAML dict → node)
    # ------------------------------------------------------------------

    def _yaml_to_node(
        self,
        step_data: dict[str, Any],
        parent: str | None,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        """Convert a YAML step/loop dict to graph nodes and edges.

        Args:
            step_data: Dict from parsed YAML.
            parent: Parent node ID (None for top-level).
            nodes: List to append created nodes to.
            edges: List to append created edges to.
        """
        step_id = step_data.get("id", "")
        is_loop = "loop_type" in step_data

        if is_loop:
            node_data = self._yaml_to_loop_data(step_data)
        else:
            node_data = self._yaml_to_step_data(step_data)

        nodes.append(GraphNode(id=step_id, data=node_data, parent=parent))

        # Create edges from depends_on
        depends_on = step_data.get("depends_on", [])
        for dep in depends_on:
            edges.append(GraphEdge(source=dep, target=step_id))

        # Recurse into loop children
        if is_loop:
            nested_steps = step_data.get("steps", [])
            for nested in nested_steps:
                self._yaml_to_node(nested, parent=step_id, nodes=nodes, edges=edges)

    def _yaml_to_step_data(self, step_data: dict[str, Any]) -> dict[str, Any]:
        """Convert a YAML step dict to node data dict.

        Args:
            step_data: Dict from parsed YAML.

        Returns:
            Node data dict with all step properties.
        """
        return {
            "id": step_data.get("id", ""),
            "script": step_data.get("script", ""),
            "params": step_data.get("params", {}),
            "resources": step_data.get("resources", {}),
            "timeout": step_data.get("timeout", 60),
            "retry": step_data.get("retry", 0),
            "on_fail": step_data.get("on_fail"),
            "export_outputs": step_data.get("export_outputs", False),
            "skip_if": step_data.get("skip_if"),
            "skip_reason": step_data.get("skip_reason"),
        }

    def _yaml_to_loop_data(self, step_data: dict[str, Any]) -> dict[str, Any]:
        """Convert a YAML loop dict to node data dict.

        Args:
            step_data: Dict from parsed YAML.

        Returns:
            Node data dict with all loop properties.
        """
        return {
            "id": step_data.get("id", ""),
            "loop_type": step_data.get("loop_type", "FOR"),
            "count": step_data.get("count"),
            "condition": step_data.get("condition"),
            "collection": step_data.get("collection"),
            "iterator_var": step_data.get("iterator_var"),
            "execution_mode": step_data.get("execution_mode", "SERIAL"),
            "max_iterations": step_data.get("max_iterations", 1000),
            "skip_if": step_data.get("skip_if"),
            "skip_reason": step_data.get("skip_reason"),
        }

    # ------------------------------------------------------------------
    # YamlPlan conversion (interop with DSL types)
    # ------------------------------------------------------------------

    def from_yaml_plan(self, plan: YamlPlan) -> SequenceGraph:
        """Convert a YamlPlan (DSL types) to a SequenceGraph.

        Args:
            plan: YamlPlan instance.

        Returns:
            SequenceGraph with nodes and edges built from plan steps/loops.
        """
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []

        prev_id: str | None = None
        for step in plan.steps:
            self._dsl_step_to_node(step, parent=None, prev_id=prev_id, nodes=nodes, edges=edges)
            prev_id = step.id

        return SequenceGraph(nodes=nodes, edges=edges)

    def _dsl_step_to_node(
        self,
        step: YamlStep | YamlLoop,
        parent: str | None,
        prev_id: str | None,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        """Convert a DSL YamlStep/YamlLoop to graph node(s) and edges."""
        if isinstance(step, YamlLoop):
            node_data: dict[str, Any] = {
                "id": step.id,
                "loop_type": step.loop_type.value,
                "count": step.count,
                "condition": step.condition,
                "collection": step.collection,
                "iterator_var": step.iterator_var,
                "execution_mode": step.execution_mode.value,
                "max_iterations": step.max_iterations,
                "skip_if": step.skip_if,
                "skip_reason": step.skip_reason,
            }
            nodes.append(GraphNode(id=step.id, data=node_data, parent=parent))

            # Add child nodes
            child_prev: str | None = None
            for child in step.steps:
                self._dsl_step_to_node(child, parent=step.id, prev_id=child_prev, nodes=nodes, edges=edges)
                child_prev = child.id

        else:
            node_data = {
                "id": step.id,
                "script": step.script,
                "params": step.params,
                "resources": step.resources,
                "timeout": step.timeout,
                "retry": step.retry,
                "on_fail": step.on_fail,
                "export_outputs": step.export_outputs,
                "skip_if": step.skip_if,
                "skip_reason": step.skip_reason,
            }
            nodes.append(GraphNode(id=step.id, data=node_data, parent=parent))

        # Create edges from preconditions
        for pre in getattr(step, "preconditions", []):
            edges.append(GraphEdge(source=pre, target=step.id))

        # If no preconditions but there's a previous sibling, create implicit edge
        if prev_id is not None and not getattr(step, "preconditions", []):
            edges.append(GraphEdge(source=prev_id, target=step.id))

    def to_yaml_plan(
        self,
        graph: SequenceGraph,
        name: str = "Untitled Sequence",
        version: str = "3.0",
    ) -> YamlPlan:
        """Convert a SequenceGraph to a YamlPlan (DSL types).

        Args:
            graph: The sequence graph.
            name: Plan name.
            version: Plan version.

        Returns:
            YamlPlan with steps/loops built from graph nodes/edges.
        """
        yaml_dict = self.serialize_plan(graph, name=name, version=version)
        return self._yaml_dict_to_plan(yaml_dict)

    def _yaml_dict_to_plan(self, data: dict[str, Any]) -> YamlPlan:
        """Convert a serialized YAML dict to a YamlPlan.

        Args:
            data: Dict with name, version, steps.

        Returns:
            YamlPlan instance.
        """
        steps: list[YamlStep | YamlLoop] = []
        for step_data in data.get("steps", []):
            step = self._yaml_dict_to_step_or_loop(step_data)
            steps.append(step)

        return YamlPlan(
            name=data.get("name", "Untitled"),
            version=data.get("version", "1.0"),
            scope=data.get("scope", {}),
            max_concurrency=data.get("max_concurrency", 1),
            steps=steps,
        )

    def _yaml_dict_to_step_or_loop(
        self,
        data: dict[str, Any],
    ) -> YamlStep | YamlLoop:
        """Convert a step/loop dict to the appropriate DSL type.

        Args:
            data: Dict with step or loop properties.

        Returns:
            YamlStep or YamlLoop instance.
        """
        if "loop_type" in data:
            return self._yaml_dict_to_loop(data)
        return self._yaml_dict_to_step(data)

    def _yaml_dict_to_step(self, data: dict[str, Any]) -> YamlStep:
        """Convert a dict to a YamlStep.

        Args:
            data: Dict with step properties.

        Returns:
            YamlStep instance.
        """
        depends_on = data.get("depends_on", [])
        return YamlStep(
            id=data.get("id", ""),
            script=data.get("script", ""),
            params=data.get("params", {}),
            preconditions=depends_on,
            resources=data.get("resources", {}),
            timeout=data.get("timeout", 60),
            retry=data.get("retry", 0),
            on_fail=data.get("on_fail"),
            export_outputs=data.get("export_outputs", False),
            skip_if=data.get("skip_if"),
            skip_reason=data.get("skip_reason"),
        )

    def _yaml_dict_to_loop(self, data: dict[str, Any]) -> YamlLoop:
        """Convert a dict to a YamlLoop.

        Args:
            data: Dict with loop properties.

        Returns:
            YamlLoop instance.
        """
        nested_steps: list[YamlStep | YamlLoop] = []
        for nested in data.get("steps", []):
            nested_steps.append(self._yaml_dict_to_step_or_loop(nested))

        loop_type_str = data.get("loop_type", "FOR")
        try:
            loop_type = LoopType(loop_type_str.upper())
        except ValueError:
            loop_type = LoopType.FOR

        exec_mode_str = data.get("execution_mode", "SERIAL")
        try:
            exec_mode = ExecutionMode(exec_mode_str.upper())
        except ValueError:
            exec_mode = ExecutionMode.SERIAL

        return YamlLoop(
            id=data.get("id", ""),
            loop_type=loop_type,
            steps=nested_steps,
            count=data.get("count"),
            condition=data.get("condition"),
            collection=data.get("collection"),
            iterator_var=data.get("iterator_var"),
            execution_mode=exec_mode,
            max_iterations=data.get("max_iterations", 1000),
            skip_if=data.get("skip_if"),
            skip_reason=data.get("skip_reason"),
        )
