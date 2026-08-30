"""Tests for YAML DSL Parser."""

from pathlib import Path

import pytest

from ate_platform.dsl.parser import YamlParser
from shared.dsl import ExecutionMode, LoopType, YamlLoop, YamlPlan, YamlStep


@pytest.fixture
def parser() -> YamlParser:
    """Create a parser instance."""
    return YamlParser()


@pytest.fixture
def valid_yaml_content() -> str:
    """Valid YAML content for testing."""
    return """
name: test_plan
version: "1.0"
scope: testing
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
"""


@pytest.fixture
def valid_yaml_file(tmp_path: Path, valid_yaml_content: str) -> Path:
    """Create a temporary valid YAML file."""
    yaml_file = tmp_path / "test_plan.yaml"
    yaml_file.write_text(valid_yaml_content, encoding="utf-8")
    return yaml_file


class TestYamlParserParse:
    """Tests for YamlParser.parse() method."""

    def test_parse_valid_yaml(self, parser: YamlParser, valid_yaml_file: Path) -> None:
        """Test parsing a valid YAML file."""
        plan = parser.parse(valid_yaml_file)

        assert isinstance(plan, YamlPlan)
        assert plan.name == "test_plan"
        assert plan.version == "1.0"
        # String scope is converted to dict for backward compatibility
        assert plan.scope == {"name": "testing"}
        assert plan.max_concurrency == 2
        assert len(plan.steps) == 2

    def test_parse_step_details(self, parser: YamlParser, valid_yaml_file: Path) -> None:
        """Test parsing step details correctly."""
        plan = parser.parse(valid_yaml_file)

        step1 = plan.steps[0]
        assert isinstance(step1, YamlStep)
        assert step1.id == "step1"
        assert step1.script == "python script1.py"
        assert step1.params == {"input": "data.csv"}
        assert step1.timeout == 120
        assert step1.retry == 2
        assert step1.on_fail is None

        step2 = plan.steps[1]
        assert isinstance(step2, YamlStep)
        assert step2.id == "step2"
        assert step2.script == "python script2.py"
        assert step2.preconditions == ["step1"]
        assert step2.on_fail == "continue"

    def test_parse_dict_scope(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing YAML with dict scope."""
        yaml_file = tmp_path / "dict_scope.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope:
  environment: staging
  region: us-east-1
steps:
  - id: step1
    script: test.py
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        assert plan.scope == {"environment": "staging", "region": "us-east-1"}

    def test_parse_string_scope_backward_compat(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test that string scope is converted to dict for backward compatibility."""
        yaml_file = tmp_path / "string_scope.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: production
steps:
  - id: step1
    script: test.py
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        assert plan.scope == {"name": "production"}

    def test_parse_missing_file(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing a non-existent file raises FileNotFoundError."""
        missing_file = tmp_path / "missing.yaml"
        with pytest.raises(FileNotFoundError, match="YAML file not found"):
            parser.parse(missing_file)

    def test_parse_missing_name(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing YAML without name raises ValueError."""
        yaml_file = tmp_path / "no_name.yaml"
        yaml_file.write_text(
            """
version: "1.0"
scope: testing
steps:
  - id: step1
    script: test.py
""",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Missing required field: 'name'"):
            parser.parse(yaml_file)

    def test_parse_missing_version(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing YAML without version raises ValueError."""
        yaml_file = tmp_path / "no_version.yaml"
        yaml_file.write_text(
            """
name: test
scope: testing
steps:
  - id: step1
    script: test.py
""",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Missing required field: 'version'"):
            parser.parse(yaml_file)

    def test_parse_missing_scope(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing YAML without scope raises ValueError."""
        yaml_file = tmp_path / "no_scope.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
steps:
  - id: step1
    script: test.py
""",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Missing required field: 'scope'"):
            parser.parse(yaml_file)

    def test_parse_missing_step_id(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing step without ID raises ValueError."""
        yaml_file = tmp_path / "no_step_id.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - script: test.py
""",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Step missing required field: 'id'"):
            parser.parse(yaml_file)

    def test_parse_missing_step_script(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing step without script raises ValueError."""
        yaml_file = tmp_path / "no_step_script.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - id: step1
""",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Step 'step1' missing required field: 'script'"):
            parser.parse(yaml_file)

    def test_parse_export_outputs(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing step with export_outputs field."""
        yaml_file = tmp_path / "export_outputs.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - id: step1
    script: test.py
    export_outputs: true
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        step = plan.steps[0]
        assert isinstance(step, YamlStep)
        assert step.export_outputs is True

    def test_parse_export_outputs_default(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test that export_outputs defaults to False."""
        yaml_file = tmp_path / "no_export.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - id: step1
    script: test.py
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        step = plan.steps[0]
        assert isinstance(step, YamlStep)
        assert step.export_outputs is False


class TestYamlParserLoopParsing:
    """Tests for loop parsing in YamlParser."""

    def test_parse_for_loop(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing a FOR loop."""
        yaml_file = tmp_path / "for_loop.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - id: loop1
    loop_type: FOR
    count: 5
    steps:
      - id: inner_step
        script: test.py
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        loop = plan.steps[0]
        assert isinstance(loop, YamlLoop)
        assert loop.id == "loop1"
        assert loop.loop_type == LoopType.FOR
        assert loop.count == 5
        assert len(loop.steps) == 1
        assert isinstance(loop.steps[0], YamlStep)

    def test_parse_while_loop(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing a WHILE loop."""
        yaml_file = tmp_path / "while_loop.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - id: loop1
    loop_type: WHILE
    condition: "result.status == 'pending'"
    max_iterations: 100
    steps:
      - id: poll_step
        script: poll.py
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        loop = plan.steps[0]
        assert isinstance(loop, YamlLoop)
        assert loop.loop_type == LoopType.WHILE
        assert loop.condition == "result.status == 'pending'"
        assert loop.max_iterations == 100

    def test_parse_foreach_loop(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing a FOREACH loop."""
        yaml_file = tmp_path / "foreach_loop.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - id: loop1
    loop_type: FOREACH
    collection: channels
    iterator_var: channel
    steps:
      - id: measure_step
        script: measure.py
        params:
          channel: "{{ channel }}"
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        loop = plan.steps[0]
        assert isinstance(loop, YamlLoop)
        assert loop.loop_type == LoopType.FOREACH
        assert loop.collection == "channels"
        assert loop.iterator_var == "channel"

    def test_parse_nested_loops(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing nested loops."""
        yaml_file = tmp_path / "nested_loop.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - id: outer_loop
    loop_type: FOR
    count: 3
    steps:
      - id: inner_loop
        loop_type: FOREACH
        collection: items
        iterator_var: item
        steps:
          - id: inner_step
            script: test.py
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        outer = plan.steps[0]
        assert isinstance(outer, YamlLoop)
        assert outer.loop_type == LoopType.FOR
        inner = outer.steps[0]
        assert isinstance(inner, YamlLoop)
        assert inner.loop_type == LoopType.FOREACH
        assert isinstance(inner.steps[0], YamlStep)

    def test_parse_loop_execution_mode(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing loop with parallel execution mode."""
        yaml_file = tmp_path / "parallel_loop.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - id: parallel_loop
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
        loop = plan.steps[0]
        assert isinstance(loop, YamlLoop)
        assert loop.execution_mode == ExecutionMode.PARALLEL

    def test_parse_loop_invalid_type(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing loop with invalid loop_type raises ValueError."""
        yaml_file = tmp_path / "bad_loop.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - id: loop1
    loop_type: INVALID
    steps:
      - id: step1
        script: test.py
""",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="invalid loop_type"):
            parser.parse(yaml_file)

    def test_parse_loop_missing_id(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test parsing loop without ID raises ValueError."""
        yaml_file = tmp_path / "no_loop_id.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - loop_type: FOR
    count: 3
    steps:
      - id: step1
        script: test.py
""",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Loop missing required field: 'id'"):
            parser.parse(yaml_file)


class TestYamlParserDefaults:
    """Tests for default values in parsed YAML."""

    def test_default_max_concurrency(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test default max_concurrency is 1."""
        yaml_file = tmp_path / "defaults.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - id: step1
    script: test.py
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        assert plan.max_concurrency == 1

    def test_default_step_timeout(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test default timeout is 60 seconds."""
        yaml_file = tmp_path / "defaults.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - id: step1
    script: test.py
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        assert plan.steps[0].timeout == 60

    def test_default_step_retry(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test default retry is 0."""
        yaml_file = tmp_path / "defaults.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - id: step1
    script: test.py
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        assert plan.steps[0].retry == 0

    def test_default_empty_collections(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test default empty collections for params, preconditions, resources."""
        yaml_file = tmp_path / "defaults.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - id: step1
    script: test.py
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        step = plan.steps[0]
        assert step.params == {}
        assert step.preconditions == []
        assert step.resources == {}

    def test_default_loop_execution_mode(self, parser: YamlParser, tmp_path: Path) -> None:
        """Test default loop execution mode is SERIAL."""
        yaml_file = tmp_path / "loop_defaults.yaml"
        yaml_file.write_text(
            """
name: test
version: "1.0"
scope: testing
steps:
  - id: loop1
    loop_type: FOR
    count: 3
    steps:
      - id: step1
        script: test.py
""",
            encoding="utf-8",
        )
        plan = parser.parse(yaml_file)
        loop = plan.steps[0]
        assert isinstance(loop, YamlLoop)
        assert loop.execution_mode == ExecutionMode.SERIAL
        assert loop.max_iterations == 1000


class TestYamlParserValidate:
    """Tests for YamlParser.validate() method."""

    def test_validate_valid_plan(self, parser: YamlParser, valid_yaml_file: Path) -> None:
        """Test validation of a valid plan returns no errors."""
        plan = parser.parse(valid_yaml_file)
        errors = parser.validate(plan)
        assert errors == []

    def test_validate_empty_name(self, parser: YamlParser) -> None:
        """Test validation catches empty name."""
        plan = YamlPlan(
            name="",
            version="1.0",
            scope={"name": "testing"},
            steps=[YamlStep(id="step1", script="test.py")],
        )
        errors = parser.validate(plan)
        assert any("name cannot be empty" in e for e in errors)

    def test_validate_empty_scope(self, parser: YamlParser) -> None:
        """Test validation catches empty scope."""
        plan = YamlPlan(
            name="test",
            version="1.0",
            scope={},
            steps=[YamlStep(id="step1", script="test.py")],
        )
        errors = parser.validate(plan)
        assert any("scope cannot be empty" in e for e in errors)

    def test_validate_invalid_max_concurrency(self, parser: YamlParser) -> None:
        """Test validation catches invalid max_concurrency."""
        plan = YamlPlan(
            name="test",
            version="1.0",
            scope={"name": "testing"},
            max_concurrency=0,
            steps=[YamlStep(id="step1", script="test.py")],
        )
        errors = parser.validate(plan)
        assert any("max_concurrency must be at least 1" in e for e in errors)

    def test_validate_duplicate_step_ids(self, parser: YamlParser) -> None:
        """Test validation catches duplicate step IDs."""
        plan = YamlPlan(
            name="test",
            version="1.0",
            scope={"name": "testing"},
            steps=[
                YamlStep(id="step1", script="test1.py"),
                YamlStep(id="step1", script="test2.py"),
            ],
        )
        errors = parser.validate(plan)
        assert any("Duplicate step ID" in e for e in errors)

    def test_validate_empty_script(self, parser: YamlParser) -> None:
        """Test validation catches empty script."""
        plan = YamlPlan(
            name="test",
            version="1.0",
            scope={"name": "testing"},
            steps=[YamlStep(id="step1", script="")],
        )
        errors = parser.validate(plan)
        assert any("script cannot be empty" in e for e in errors)

    def test_validate_negative_timeout(self, parser: YamlParser) -> None:
        """Test validation catches negative timeout."""
        plan = YamlPlan(
            name="test",
            version="1.0",
            scope={"name": "testing"},
            steps=[YamlStep(id="step1", script="test.py", timeout=-1)],
        )
        errors = parser.validate(plan)
        assert any("timeout must be non-negative" in e for e in errors)

    def test_validate_negative_retry(self, parser: YamlParser) -> None:
        """Test validation catches negative retry."""
        plan = YamlPlan(
            name="test",
            version="1.0",
            scope={"name": "testing"},
            steps=[YamlStep(id="step1", script="test.py", retry=-1)],
        )
        errors = parser.validate(plan)
        assert any("retry must be non-negative" in e for e in errors)

    def test_validate_no_steps(self, parser: YamlParser) -> None:
        """Test validation catches plan with no steps."""
        plan = YamlPlan(
            name="test",
            version="1.0",
            scope={"name": "testing"},
            steps=[],
        )
        errors = parser.validate(plan)
        assert any("must have at least one step" in e for e in errors)

    def test_validate_while_loop_without_condition(self, parser: YamlParser) -> None:
        """Test validation catches WHILE loop without condition or max_iterations."""
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.WHILE,
            steps=[YamlStep(id="step1", script="test.py")],
            max_iterations=0,
        )
        plan = YamlPlan(
            name="test",
            version="1.0",
            scope={"name": "testing"},
            steps=[loop],
        )
        errors = parser.validate(plan)
        assert any("WHILE loop must have a condition" in e for e in errors)

    def test_validate_for_loop_without_count(self, parser: YamlParser) -> None:
        """Test validation catches FOR loop without count."""
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.FOR,
            steps=[YamlStep(id="step1", script="test.py")],
        )
        plan = YamlPlan(
            name="test",
            version="1.0",
            scope={"name": "testing"},
            steps=[loop],
        )
        errors = parser.validate(plan)
        assert any("FOR loop must have a 'count' field" in e for e in errors)

    def test_validate_foreach_loop_without_collection(self, parser: YamlParser) -> None:
        """Test validation catches FOREACH loop without collection."""
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.FOREACH,
            steps=[YamlStep(id="step1", script="test.py")],
        )
        plan = YamlPlan(
            name="test",
            version="1.0",
            scope={"name": "testing"},
            steps=[loop],
        )
        errors = parser.validate(plan)
        assert any("FOREACH loop must have a 'collection' field" in e for e in errors)

    def test_validate_loop_empty_steps(self, parser: YamlParser) -> None:
        """Test validation catches loop with no steps."""
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.FOR,
            count=3,
            steps=[],
        )
        plan = YamlPlan(
            name="test",
            version="1.0",
            scope={"name": "testing"},
            steps=[loop],
        )
        errors = parser.validate(plan)
        assert any("must have at least one step" in e for e in errors)

    def test_validate_nested_duplicate_ids(self, parser: YamlParser) -> None:
        """Test validation catches duplicate IDs across nested duplicate IDs across loops."""
        loop = YamlLoop(
            id="step1",
            loop_type=LoopType.FOR,
            count=3,
            steps=[YamlStep(id="step1", script="test.py")],
        )
        plan = YamlPlan(
            name="test",
            version="1.0",
            scope={"name": "testing"},
            steps=[loop],
        )
        errors = parser.validate(plan)
        assert any("Duplicate step ID" in e for e in errors)


class TestYamlPlanDataclass:
    """Tests for YamlPlan dataclass."""

    def test_yaml_plan_creation(self) -> None:
        """Test creating a YamlPlan instance."""
        plan = YamlPlan(
            name="test",
            version="1.0",
            scope={"name": "testing"},
            max_concurrency=4,
            steps=[YamlStep(id="step1", script="test.py")],
        )
        assert plan.name == "test"
        assert plan.version == "1.0"
        assert plan.scope == {"name": "testing"}
        assert plan.max_concurrency == 4
        assert len(plan.steps) == 1

    def test_yaml_plan_with_loop(self) -> None:
        """Test creating a YamlPlan with a loop step."""
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.FOR,
            count=3,
            steps=[YamlStep(id="inner_step", script="test.py")],
        )
        plan = YamlPlan(
            name="test",
            version="1.0",
            scope={"name": "testing"},
            steps=[loop],
        )
        assert len(plan.steps) == 1
        assert isinstance(plan.steps[0], YamlLoop)


class TestYamlStepDataclass:
    """Tests for YamlStep dataclass."""

    def test_yaml_step_creation(self) -> None:
        """Test creating a YamlStep instance."""
        step = YamlStep(
            id="step1",
            script="python test.py",
            params={"key": "value"},
            preconditions=["pre_step"],
            resources={"cpu": 2},
            timeout=120,
            retry=3,
            on_fail="abort",
            export_outputs=True,
        )
        assert step.id == "step1"
        assert step.script == "python test.py"
        assert step.params == {"key": "value"}
        assert step.preconditions == ["pre_step"]
        assert step.resources == {"cpu": 2}
        assert step.timeout == 120
        assert step.retry == 3
        assert step.on_fail == "abort"
        assert step.export_outputs is True

    def test_yaml_step_defaults(self) -> None:
        """Test YamlStep default values."""
        step = YamlStep(id="step1", script="test.py")
        assert step.params == {}
        assert step.preconditions == []
        assert step.resources == {}
        assert step.timeout == 60
        assert step.retry == 0
        assert step.on_fail is None
        assert step.export_outputs is False


class TestYamlLoopDataclass:
    """Tests for YamlLoop dataclass."""

    def test_yaml_loop_creation(self) -> None:
        """Test creating a YamlLoop instance."""
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.FOR,
            count=5,
            steps=[YamlStep(id="step1", script="test.py")],
            execution_mode=ExecutionMode.PARALLEL,
        )
        assert loop.id == "loop1"
        assert loop.loop_type == LoopType.FOR
        assert loop.count == 5
        assert len(loop.steps) == 1
        assert loop.execution_mode == ExecutionMode.PARALLEL

    def test_yaml_loop_defaults(self) -> None:
        """Test YamlLoop default values."""
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.WHILE,
            condition="x > 0",
        )
        assert loop.steps == []
        assert loop.count is None
        assert loop.collection is None
        assert loop.iterator_var is None
        assert loop.execution_mode == ExecutionMode.SERIAL
        assert loop.max_iterations == 1000
