"""Syrupy snapshot tests for YAML DSL parse/serialize round-trip.

These tests verify that a YamlPlan parsed from YAML can be serialized back
to a dict and re-parsed into an equivalent plan. The snapshot captures the
serialized dict form (JSON format) so that any drift in the DSL schema or
parser output is caught by a snapshot diff.

Run with: pytest tests/unit/dsl/test_dsl_snapshot.py -v -m snapshot
Update snapshots: pytest tests/unit/dsl/test_dsl_snapshot.py --snapshot-update
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from syrupy.extensions.json import JSONSnapshotExtension

from ate_platform.dsl.parser import YamlParser
from shared.dsl import YamlLoop, YamlPlan, YamlStep

pytestmark = pytest.mark.snapshot


@pytest.fixture
def parser() -> YamlParser:
    """Create a parser instance (mirrors test_parser.py fixture)."""
    return YamlParser()


@pytest.fixture
def snapshot_json(snapshot: pytest.SnapshotFixture) -> Any:
    """Use JSON snapshot format (one .json file per snapshot)."""
    return snapshot.use_extension(JSONSnapshotExtension)


def _serialize_step(step: YamlStep) -> dict[str, Any]:
    """Serialize a YamlStep to a plain dict suitable for YAML round-trip."""
    return {
        "id": step.id,
        "script": step.script,
        "params": step.params,
        "preconditions": step.preconditions,
        "resources": step.resources,
        "timeout": step.timeout,
        "retry": step.retry,
        "on_fail": step.on_fail,
        "export_outputs": step.export_outputs,
    }


def _serialize_loop(loop: YamlLoop) -> dict[str, Any]:
    """Serialize a YamlLoop to a plain dict suitable for YAML round-trip."""
    return {
        "id": loop.id,
        "loop_type": loop.loop_type.value,
        "steps": [_serialize_any_step(s) for s in loop.steps],
        "count": loop.count,
        "condition": loop.condition,
        "collection": loop.collection,
        "iterator_var": loop.iterator_var,
        "execution_mode": loop.execution_mode.value,
        "max_iterations": loop.max_iterations,
    }


def _serialize_any_step(step: YamlStep | YamlLoop) -> dict[str, Any]:
    """Dispatch serialization to step or loop."""
    if isinstance(step, YamlLoop):
        return _serialize_loop(step)
    return _serialize_step(step)


def _serialize_plan(plan: YamlPlan) -> dict[str, Any]:
    """Serialize a YamlPlan to a plain dict suitable for YAML round-trip."""
    return {
        "name": plan.name,
        "version": plan.version,
        "scope": plan.scope,
        "max_concurrency": plan.max_concurrency,
        "steps": [_serialize_any_step(s) for s in plan.steps],
    }


def _write_plan_yaml(plan: YamlPlan, path: Path) -> None:
    """Serialize a YamlPlan to a YAML file."""
    data = _serialize_plan(plan)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


class TestDSLSnapshotRoundTrip:
    """Snapshot tests for YAML DSL parse → serialize → parse round-trip."""

    def test_simple_plan_round_trip(
        self, parser: YamlParser, tmp_path: Path, snapshot_json: Any
    ) -> None:
        """Parse a simple plan, serialize it, snapshot the dict form."""
        yaml_file = tmp_path / "simple.yaml"
        yaml_file.write_text(
            """
name: simple_plan
version: "1.0"
scope: production
max_concurrency: 2
steps:
  - id: step1
    script: python script1.py
    params:
      input: data.csv
    timeout: 120
    retry: 2
  - id: step2
    script: python script2.py
    preconditions:
      - step1
    on_fail: continue
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        serialized = _serialize_plan(plan)
        assert serialized == snapshot_json

        # Round-trip: write back to YAML and re-parse
        round_trip_file = tmp_path / "round_trip.yaml"
        _write_plan_yaml(plan, round_trip_file)
        plan2 = parser.parse(round_trip_file)

        assert plan2.name == plan.name
        assert plan2.version == plan.version
        assert plan2.max_concurrency == plan.max_concurrency
        assert len(plan2.steps) == len(plan.steps)

    def test_plan_with_loops_round_trip(
        self, parser: YamlParser, tmp_path: Path, snapshot_json: Any
    ) -> None:
        """Parse a plan with FOR/WHILE/FOREACH loops, serialize, snapshot."""
        yaml_file = tmp_path / "loops.yaml"
        yaml_file.write_text(
            """
name: loop_plan
version: "2.0"
scope:
  environment: staging
max_concurrency: 4
steps:
  - id: for_loop
    loop_type: FOR
    count: 5
    steps:
      - id: inner_step
        script: measure.py
        params:
          channel: 1
  - id: while_loop
    loop_type: WHILE
    condition: "result.status == 'pending'"
    max_iterations: 100
    steps:
      - id: poll
        script: poll.py
  - id: foreach_loop
    loop_type: FOREACH
    collection: channels
    iterator_var: ch
    execution_mode: PARALLEL
    steps:
      - id: measure
        script: measure.py
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        serialized = _serialize_plan(plan)
        assert serialized == snapshot_json

        # Round-trip: write back to YAML and re-parse
        round_trip_file = tmp_path / "round_trip_loops.yaml"
        _write_plan_yaml(plan, round_trip_file)
        plan2 = parser.parse(round_trip_file)

        assert plan2.name == plan.name
        # All three steps should be loops
        assert len(plan2.steps) == 3
        for original, round_tripped in zip(plan.steps, plan2.steps, strict=True):
            assert isinstance(round_tripped, YamlLoop)
            assert isinstance(original, YamlLoop)
            assert round_tripped.loop_type == original.loop_type
            assert round_tripped.execution_mode == original.execution_mode

    def test_plan_with_resources_round_trip(
        self, parser: YamlParser, tmp_path: Path, snapshot_json: Any
    ) -> None:
        """Parse a plan with resource constraints, serialize, snapshot."""
        yaml_file = tmp_path / "resources.yaml"
        yaml_file.write_text(
            """
name: resource_plan
version: "1.5"
scope: production
max_concurrency: 1
steps:
  - id: init
    script: init.py
    resources:
      gpib_bus: 1
    timeout: 30
  - id: measure
    script: measure.py
    preconditions:
      - init
    resources:
      gpib_bus: 1
      dmm: 1
    timeout: 60
    export_outputs: true
  - id: cleanup
    script: cleanup.py
    preconditions:
      - measure
    on_fail: ignore
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        serialized = _serialize_plan(plan)
        assert serialized == snapshot_json

        # Round-trip: write back to YAML and re-parse
        round_trip_file = tmp_path / "round_trip_resources.yaml"
        _write_plan_yaml(plan, round_trip_file)
        plan2 = parser.parse(round_trip_file)

        assert plan2.name == plan.name
        step1 = plan2.steps[0]
        assert isinstance(step1, YamlStep)
        assert step1.resources == {"gpib_bus": 1}
        step2 = plan2.steps[1]
        assert isinstance(step2, YamlStep)
        assert step2.export_outputs is True
        assert step2.resources == {"gpib_bus": 1, "dmm": 1}
