"""Tests for YAML DSL v3.2 additions (设计文档 §6.5.4).

Covers the v3.2 step types:
- ``type: fixture_control`` (action + fixture_id + on_failure)
- ``type: barrier`` (barrier_name)
- ``type: action`` (explicit script step with uut_affinity/depends_on)
- ``type: loop`` (count/iterator/steps container form)
- ``type: branch`` / ``type: subsequence`` (container forms for the compiler)
- ``on_failure``/``on_fail`` alias normalization
"""

from pathlib import Path

import pytest
import yaml

from ate_platform.dsl.parser import YamlParser
from shared.dsl import LoopType, StepType, YamlLoop, YamlStep

FIXTURE_CONTROL_YAML = """\
name: v32_plan
version: "3.2"
scope: production
max_concurrency: 2
steps:
  - id: fixture_clamp
    type: fixture_control
    action: clamp
    fixture_id: "fixture_ps_12v5a_v1"
    on_failure: abort
  - id: power_on
    type: action
    script: power_on.py
    uut_affinity: any
    resources: ["PSU_1", "ELoad_1"]
    depends_on: [fixture_clamp]
    on_failure: continue
  - id: sync_power_on
    type: barrier
    barrier_name: "all_powered_on"
    depends_on: [power_on]
"""


@pytest.fixture
def parser() -> YamlParser:
    """Create a parser instance."""
    return YamlParser()


def _write(tmp_path: Path, content: str) -> Path:
    """Write YAML content to a temp file and return its path."""
    yaml_file = tmp_path / "v32_plan.yaml"
    yaml_file.write_text(content, encoding="utf-8")
    return yaml_file


class TestV32FixtureControl:
    """Tests for ``type: fixture_control`` steps."""

    def test_parse_fixture_control_step(self, parser: YamlParser, tmp_path: Path) -> None:
        plan = parser.parse(_write(tmp_path, FIXTURE_CONTROL_YAML))

        step = plan.steps[0]
        assert isinstance(step, YamlStep)
        assert step.type == StepType.FIXTURE_CONTROL
        assert step.action == "clamp"
        assert step.fixture_id == "fixture_ps_12v5a_v1"
        assert step.on_failure == "abort"
        # non-script step keeps an empty script (no crash on serialize)
        assert step.script == ""

    def test_fixture_control_validate_missing_action(
        self, parser: YamlParser, tmp_path: Path
    ) -> None:
        content = """\
name: p
version: "3.2"
scope: production
steps:
  - id: bad_fixture
    type: fixture_control
    fixture_id: "f1"
"""
        plan = parser.parse(_write(tmp_path, content))
        errors = parser.validate(plan)
        assert any("requires 'action'" in e for e in errors)

    def test_fixture_control_validate_missing_fixture_id(
        self, parser: YamlParser, tmp_path: Path
    ) -> None:
        content = """\
name: p
version: "3.2"
scope: production
steps:
  - id: bad_fixture
    type: fixture_control
    action: clamp
"""
        plan = parser.parse(_write(tmp_path, content))
        errors = parser.validate(plan)
        assert any("requires 'fixture_id'" in e for e in errors)


class TestV32Barrier:
    """Tests for ``type: barrier`` steps."""

    def test_parse_barrier_step(self, parser: YamlParser, tmp_path: Path) -> None:
        plan = parser.parse(_write(tmp_path, FIXTURE_CONTROL_YAML))

        step = plan.steps[2]
        assert isinstance(step, YamlStep)
        assert step.type == StepType.BARRIER
        assert step.barrier_name == "all_powered_on"
        assert step.depends_on == ["power_on"]

    def test_barrier_validate_requires_name(
        self, parser: YamlParser, tmp_path: Path
    ) -> None:
        content = """\
name: p
version: "3.2"
scope: production
steps:
  - id: sync
    type: barrier
"""
        plan = parser.parse(_write(tmp_path, content))
        errors = parser.validate(plan)
        assert any("requires 'barrier_name'" in e for e in errors)


class TestV32ActionAndScript:
    """Tests for v3.2 ``type: action`` and legacy no-type script steps."""

    def test_parse_action_step(self, parser: YamlParser, tmp_path: Path) -> None:
        plan = parser.parse(_write(tmp_path, FIXTURE_CONTROL_YAML))

        step = plan.steps[1]
        assert isinstance(step, YamlStep)
        assert step.type == StepType.ACTION
        assert step.script == "power_on.py"
        assert step.uut_affinity == "any"
        assert step.depends_on == ["fixture_clamp"]
        # on_failure normalization — parser prefers on_failure over on_fail
        assert step.on_failure == "continue"

    def test_legacy_step_gets_script_type(
        self, parser: YamlParser, tmp_path: Path
    ) -> None:
        content = """\
name: p
version: "3.2"
scope: production
steps:
  - id: legacy
    script: foo.py
    on_fail: stop
"""
        plan = parser.parse(_write(tmp_path, content))
        step = plan.steps[0]
        assert isinstance(step, YamlStep)
        # no type → SCRIPT (backward compat)
        assert step.type is None
        assert step.script == "foo.py"
        # on_failure falls back to on_fail for the v3.2 alias
        assert step.on_failure == "stop"
        assert step.on_fail == "stop"

    def test_on_failure_preferred_over_on_fail(
        self, parser: YamlParser, tmp_path: Path
    ) -> None:
        content = """\
name: p
version: "3.2"
scope: production
steps:
  - id: both
    type: action
    script: foo.py
    on_failure: abort
    on_fail: continue
"""
        plan = parser.parse(_write(tmp_path, content))
        step = plan.steps[0]
        assert isinstance(step, YamlStep)
        assert step.on_failure == "abort"  # v3.2 takes precedence
        assert step.on_fail == "continue"  # raw v3.0 field preserved


class TestV32LoopContainer:
    """Tests for the v3.2 ``type: loop`` container form."""

    def test_parse_v32_for_loop(self, parser: YamlParser, tmp_path: Path) -> None:
        content = """\
name: p
version: "3.2"
scope: production
steps:
  - id: load_test
    type: loop
    count: 5
    depends_on: [sync]
    steps:
      - id: set_load
        type: action
        script: set_load.py
      - id: measure
        type: action
        script: measure_vout.py
        timeout: 10
        retry: 2
        on_failure: continue
"""
        plan = parser.parse(_write(tmp_path, content))

        loop = plan.steps[0]
        assert isinstance(loop, YamlLoop)
        assert loop.loop_type == LoopType.FOR
        assert loop.count == 5
        assert loop.depends_on == ["sync"]
        assert len(loop.steps) == 2

        inner = loop.steps[1]
        assert isinstance(inner, YamlStep)
        assert inner.type == StepType.ACTION
        assert inner.timeout == 10
        assert inner.retry == 2
        assert inner.on_failure == "continue"

    def test_parse_v32_while_loop(self, parser: YamlParser, tmp_path: Path) -> None:
        content = """\
name: p
version: "3.2"
scope: production
steps:
  - id: wait_ready
    type: loop
    condition: "${scope.ready} == false"
    steps:
      - id: poll
        type: action
        script: poll.py
"""
        plan = parser.parse(_write(tmp_path, content))
        loop = plan.steps[0]
        assert isinstance(loop, YamlLoop)
        assert loop.loop_type == LoopType.WHILE
        assert loop.condition == "${scope.ready} == false"


class TestV32BranchSubsequence:
    """Tests for ``type: branch`` and ``type: subsequence`` container forms."""

    def test_parse_branch(self, parser: YamlParser, tmp_path: Path) -> None:
        content = """\
name: p
version: "3.2"
scope: production
steps:
  - id: check_result
    type: branch
    condition: "${avg_voltage} > 11.4"
    depends_on: [load_test]
    then:
      - id: final_pass
        type: action
        script: mark_pass.py
    else:
      - id: final_fail
        type: action
        script: mark_fail.py
        on_failure: abort
"""
        plan = parser.parse(_write(tmp_path, content))
        step = plan.steps[0]
        assert isinstance(step, YamlStep)
        assert step.type == StepType.BRANCH
        assert step.params["condition"] == "${avg_voltage} > 11.4"
        assert len(step.params["then"]) == 1
        assert len(step.params["else"]) == 1

    def test_parse_subsequence(self, parser: YamlParser, tmp_path: Path) -> None:
        content = """\
name: p
version: "3.2"
scope: production
steps:
  - id: common_checks
    type: subsequence
    steps:
      - id: reset_inst
        type: action
        script: reset_inst.py
"""
        plan = parser.parse(_write(tmp_path, content))
        step = plan.steps[0]
        assert isinstance(step, YamlStep)
        assert step.type == StepType.SUBSEQUENCE
        assert len(step.params["steps"]) == 1


class TestV32InvalidType:
    """Tests for invalid v3.2 step types."""

    def test_unknown_type_rejected(self, parser: YamlParser, tmp_path: Path) -> None:
        content = """\
name: p
version: "3.2"
scope: production
steps:
  - id: weird
    type: teleport
    script: foo.py
"""
        with pytest.raises(ValueError, match="invalid type 'teleport'"):
            parser.parse(_write(tmp_path, content))

    def test_script_step_requires_script(self, parser: YamlParser, tmp_path: Path) -> None:
        content = """\
name: p
version: "3.2"
scope: production
steps:
  - id: no_script
    type: action
"""
        with pytest.raises(ValueError, match="missing required field: 'script'"):
            parser.parse(_write(tmp_path, content))

    def test_resources_list_normalized_to_dict(
        self, parser: YamlParser, tmp_path: Path
    ) -> None:
        # v3.2 §6.5.2: resources 为 list[string]；消费方（DryRunScheduler 等）
        # 期望 dict → parser 归一为 {"name": {}}
        content = """\
name: p
version: "3.2"
scope: production
steps:
  - id: action
    type: action
    script: foo.py
    resources: ["PSU_1", "ELoad_1"]
"""
        plan = parser.parse(_write(tmp_path, content))
        step = plan.steps[0]
        assert isinstance(step, YamlStep)
        assert step.resources == {"PSU_1": {}, "ELoad_1": {}}


class TestV32FullExample:
    """End-to-end: the §6.5.4 complete DSL example parses and validates."""

    def test_full_v32_example_round_trip(self, parser: YamlParser, tmp_path: Path) -> None:
        content = FIXTURE_CONTROL_YAML
        plan = parser.parse(_write(tmp_path, content))

        # Round-trip through YAML preserves v3.2 fields
        data = yaml.safe_load(content)
        for i, step_data in enumerate(data["steps"]):
            step = plan.steps[i]
            assert step.id == step_data["id"]
            if "type" in step_data:
                assert step.type.value == step_data["type"]

        errors = parser.validate(plan)
        assert errors == []
