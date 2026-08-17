"""YAML DSL Parser for ATE Platform."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from shared.dsl import ExecutionMode, LoopType, StepType, YamlLoop, YamlPlan, YamlStep


class YamlParser:
    """Parser for YAML DSL files."""

    def parse(self, yaml_path: Path) -> YamlPlan:
        """Parse a YAML file into a YamlPlan object.

        Args:
            yaml_path: Path to the YAML file.

        Returns:
            Parsed YamlPlan object.

        Raises:
            FileNotFoundError: If the YAML file doesn't exist.
            yaml.YAMLError: If the YAML is malformed.
            ValueError: If required fields are missing.
        """
        if not yaml_path.exists():
            raise FileNotFoundError(f"YAML file not found: {yaml_path}")

        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError("YAML content must be a dictionary")

        # Extract required fields
        name = data.get("name")
        if not name:
            raise ValueError("Missing required field: 'name'")

        version = data.get("version")
        if not version:
            raise ValueError("Missing required field: 'version'")

        scope = data.get("scope")
        if not scope:
            raise ValueError("Missing required field: 'scope'")

        # Backward compatibility: accept string scope (e.g. "production")
        # and convert to dict format {"name": "production"}
        if isinstance(scope, str):
            scope = {"name": scope}
        elif not isinstance(scope, dict):
            raise ValueError("Scope must be a string or dictionary")

        # Extract optional fields with defaults
        max_concurrency = data.get("max_concurrency", 1)

        # Parse steps (can contain both YamlStep and YamlLoop)
        steps_data = data.get("steps", [])
        steps: list[YamlStep | YamlLoop] = []
        for step_data in steps_data:
            step = self._parse_step_or_loop(step_data)
            steps.append(step)

        return YamlPlan(
            name=name,
            version=version,
            scope=scope,
            max_concurrency=max_concurrency,
            steps=steps,
        )

    def _parse_step_or_loop(self, data: dict[str, Any]) -> YamlStep | YamlLoop:
        """Parse a step or loop entry from YAML data.

        Dispatch order (DSL v3.2, 设计文档 §6.5.4):
        1. ``type: loop`` / ``type: branch`` / ``type: subsequence`` 容器 →
           容器解析（递归展开内部步骤）
        2. 旧式 ``loop_type`` 键 → 旧式循环解析（v3.0 兼容）
        3. 其余（含 ``type: fixture_control`` / ``barrier`` / ``action`` /
           ``call`` / 无 type 的脚本步骤）→ ``_parse_step``

        Args:
            data: Dictionary containing step or loop data.

        Returns:
            Parsed YamlStep or YamlLoop object.
        """
        step_type = data.get("type")
        if step_type == "loop":
            return self._parse_loop_v32(data)
        if step_type == "branch":
            return self._parse_branch(data)
        if step_type == "subsequence":
            return self._parse_subsequence(data)
        if "loop_type" in data:
            return self._parse_loop(data)
        return self._parse_step(data)

    def _parse_step(self, data: dict[str, Any]) -> YamlStep:
        """Parse a single step from YAML data (DSL v3.2).

        v3.2 步骤类型（fixture_control/barrier/action/call/无 type 脚本步骤）
        均在此解析。脚本步骤必须含 ``script``；barrier 步骤必须含
        ``barrier_name``；fixture_control 步骤必须含 ``action``。

        Args:
            data: Dictionary containing step data.

        Returns:
            Parsed YamlStep object.

        Raises:
            ValueError: If required fields are missing.
        """
        step_id = data.get("id")
        if not step_id:
            raise ValueError("Step missing required field: 'id'")

        type_str = data.get("type")
        step_type: StepType | None = None
        if type_str is not None:
            try:
                step_type = StepType(str(type_str).lower())
            except ValueError:
                valid = ", ".join(t.value for t in StepType)
                raise ValueError(
                    f"Step '{step_id}' has invalid type '{type_str}'. Must be one of: {valid}"
                ) from None

        is_script_step = step_type in (None, StepType.SCRIPT, StepType.ACTION, StepType.CALL)
        if is_script_step:
            script = data.get("script")
            if not script:
                raise ValueError(f"Step '{step_id}' missing required field: 'script'")
        else:
            script = data.get("script", "")

        # on_failure（v3.2）与 on_fail（v3.0）别名归一：优先 on_failure
        on_failure = data.get("on_failure", data.get("on_fail"))

        # resources 归一：v3.2 用 list[string]（§6.5.2），v3.0 用 dict；
        # list → {"name": {}} 保持所有消费方（DryRunScheduler/cpsat 等）兼容
        resources = data.get("resources", {})
        if isinstance(resources, list):
            resources = {str(name): {} for name in resources}
        elif not isinstance(resources, dict):
            resources = {}

        return YamlStep(
            id=step_id,
            type=step_type,
            script=script,
            params=data.get("params", {}),
            preconditions=data.get("preconditions", []),
            depends_on=data.get("depends_on", []),
            resources=resources,
            timeout=data.get("timeout", 60),
            retry=data.get("retry", 0),
            on_fail=data.get("on_fail"),
            on_failure=on_failure,
            uut_affinity=data.get("uut_affinity"),
            barrier_name=data.get("barrier_name"),
            action=data.get("action"),
            fixture_id=data.get("fixture_id"),
            export_outputs=data.get("export_outputs", False),
            skip_if=data.get("skip_if"),
            skip_reason=data.get("skip_reason"),
        )

    def _parse_loop(self, data: dict[str, Any]) -> YamlLoop:
        """Parse a loop construct from YAML data.

        Args:
            data: Dictionary containing loop data.

        Returns:
            Parsed YamlLoop object.

        Raises:
            ValueError: If required fields are missing or loop type is invalid.
        """
        loop_id = data.get("id")
        if not loop_id:
            raise ValueError("Loop missing required field: 'id'")

        loop_type_str = data.get("loop_type")
        if not loop_type_str:
            raise ValueError(f"Loop '{loop_id}' missing required field: 'loop_type'")

        try:
            loop_type = LoopType(loop_type_str.upper())
        except ValueError:
            valid = ", ".join(t.value for t in LoopType)
            raise ValueError(
                f"Loop '{loop_id}' has invalid loop_type '{loop_type_str}'. Must be one of: {valid}"
            ) from None

        # Parse nested steps
        nested_steps_data = data.get("steps", [])
        nested_steps: list[YamlStep | YamlLoop] = []
        for step_data in nested_steps_data:
            nested_steps.append(self._parse_step_or_loop(step_data))

        # Parse execution mode
        execution_mode_str = data.get("execution_mode", "SERIAL")
        try:
            execution_mode = ExecutionMode(execution_mode_str.upper())
        except ValueError:
            valid = ", ".join(m.value for m in ExecutionMode)
            raise ValueError(
                f"Loop '{loop_id}' has invalid execution_mode '{execution_mode_str}'. Must be one of: {valid}"
            ) from None

        return YamlLoop(
            id=loop_id,
            loop_type=loop_type,
            steps=nested_steps,
            count=data.get("count"),
            condition=data.get("condition"),
            collection=data.get("collection"),
            iterator_var=data.get("iterator_var"),
            execution_mode=execution_mode,
            max_iterations=data.get("max_iterations", 1000),
        )

    def _parse_loop_v32(self, data: dict[str, Any]) -> YamlLoop:
        """Parse a v3.2 ``type: loop`` container (设计文档 §6.5.4).

        v3.2 循环使用 ``type: loop`` + ``count``/``iterator``/``steps``，
        与 v3.0 的 ``loop_type: FOR`` 语法并存。FOR 循环映射为
        ``LoopType.FOR``（count 必填）；带 condition 的映射为
        ``LoopType.WHILE``；带 collection/iterator_var 的映射为
        ``LoopType.FOREACH``。

        Args:
            data: Dictionary containing loop data.

        Returns:
            Parsed YamlLoop object.

        Raises:
            ValueError: If required fields are missing.
        """
        loop_id = data.get("id")
        if not loop_id:
            raise ValueError("Loop missing required field: 'id'")

        # 推断旧式 loop_type：count 存在 → FOR；condition → WHILE；否则 FOR（默认）
        if data.get("condition"):
            loop_type = LoopType.WHILE
        elif data.get("collection") or data.get("iterator_var"):
            loop_type = LoopType.FOREACH
        else:
            loop_type = LoopType.FOR

        nested_steps_data = data.get("steps", [])
        nested_steps: list[YamlStep | YamlLoop] = []
        for step_data in nested_steps_data:
            nested_steps.append(self._parse_step_or_loop(step_data))

        execution_mode_str = data.get("execution_mode", "SERIAL")
        try:
            execution_mode = ExecutionMode(execution_mode_str.upper())
        except ValueError:
            valid = ", ".join(m.value for m in ExecutionMode)
            raise ValueError(
                f"Loop '{loop_id}' has invalid execution_mode '{execution_mode_str}'. Must be one of: {valid}"
            ) from None

        return YamlLoop(
            id=loop_id,
            loop_type=loop_type,
            steps=nested_steps,
            count=data.get("count"),
            condition=data.get("condition"),
            collection=data.get("collection"),
            iterator_var=data.get("iterator_var"),
            execution_mode=execution_mode,
            max_iterations=data.get("max_iterations", 1000),
            depends_on=data.get("depends_on", []),
        )

    def _parse_branch(self, data: dict[str, Any]) -> YamlStep:
        """Parse a v3.2 ``type: branch`` container as a branch-eval step.

        分支编译为单一 YamlStep（type=BRANCH，condition + then_ids/else_ids
        由编译器消费），``then``/``else`` 内部步骤作为参数保留。

        Args:
            data: Dictionary containing branch data.

        Returns:
            A YamlStep of type BRANCH.

        Raises:
            ValueError: If required fields are missing.
        """
        branch_id = data.get("id")
        if not branch_id:
            raise ValueError("Branch missing required field: 'id'")

        params: dict[str, Any] = dict(data.get("params", {}))
        params["condition"] = data.get("condition", "True")
        params["then"] = data.get("then", [])
        params["else"] = data.get("else", [])

        return YamlStep(
            id=branch_id,
            type=StepType.BRANCH,
            script=data.get("script", ""),
            params=params,
            preconditions=data.get("preconditions", []),
            depends_on=data.get("depends_on", []),
            resources=data.get("resources", {}),
            timeout=data.get("timeout", 60),
            retry=data.get("retry", 0),
            on_failure=data.get("on_failure", data.get("on_fail")),
            uut_affinity=data.get("uut_affinity"),
            skip_if=data.get("skip_if"),
            skip_reason=data.get("skip_reason"),
        )

    def _parse_subsequence(self, data: dict[str, Any]) -> YamlStep:
        """Parse a v3.2 ``type: subsequence`` container as a call step.

        子序列被编译为单一 YamlStep（type=SUBSEQUENCE），内部步骤保留在
        params["steps"]，由编译器展开（§6.3.4 _compile_subsequence）。

        Args:
            data: Dictionary containing subsequence data.

        Returns:
            A YamlStep of type SUBSEQUENCE.

        Raises:
            ValueError: If required fields are missing.
        """
        sub_id = data.get("id")
        if not sub_id:
            raise ValueError("Subsequence missing required field: 'id'")

        params: dict[str, Any] = dict(data.get("params", {}))
        params["steps"] = data.get("steps", [])

        return YamlStep(
            id=sub_id,
            type=StepType.SUBSEQUENCE,
            script=data.get("script", ""),
            params=params,
            preconditions=data.get("preconditions", []),
            depends_on=data.get("depends_on", []),
            resources=data.get("resources", {}),
            timeout=data.get("timeout", 60),
            retry=data.get("retry", 0),
            on_failure=data.get("on_failure", data.get("on_fail")),
            uut_affinity=data.get("uut_affinity"),
            skip_if=data.get("skip_if"),
            skip_reason=data.get("skip_reason"),
        )

    def validate(self, plan: YamlPlan) -> list[str]:
        """Validate a YamlPlan object.

        Args:
            plan: The plan to validate.

        Returns:
            List of validation error messages. Empty if valid.
        """
        errors: list[str] = []

        # Validate plan-level fields
        if not plan.name or not plan.name.strip():
            errors.append("Plan name cannot be empty")

        if not plan.version:
            errors.append("Plan version cannot be empty")

        if not plan.scope:
            errors.append("Plan scope cannot be empty")

        if plan.max_concurrency < 1:
            errors.append("max_concurrency must be at least 1")

        # Validate steps and loops, collecting all IDs for uniqueness check
        step_ids: set[str] = set()
        self._validate_steps(plan.steps, step_ids, errors)

        # Check for at least one step
        if not plan.steps:
            errors.append("Plan must have at least one step")

        return errors

    def _validate_steps(
        self,
        steps: list[YamlStep | YamlLoop],
        seen_ids: set[str],
        errors: list[str],
    ) -> None:
        """Recursively validate steps and loops, checking ID uniqueness.

        Args:
            steps: List of steps/loops to validate.
            seen_ids: Set of IDs seen so far (mutated in place).
            errors: List to append error messages to (mutated in place).
        """
        for step in steps:
            # Check for duplicate IDs
            if step.id in seen_ids:
                errors.append(f"Duplicate step ID: '{step.id}'")
            seen_ids.add(step.id)

            if isinstance(step, YamlLoop):
                self._validate_loop(step, seen_ids, errors)
            else:
                self._validate_step(step, errors)

    def _validate_step(self, step: YamlStep, errors: list[str]) -> None:
        """Validate a single YamlStep.

        Args:
            step: The step to validate.
            errors: List to append error messages to.
        """
        # 脚本类步骤（无 type / script / action / call）必须提供 script
        if step.type in (None, StepType.SCRIPT, StepType.ACTION, StepType.CALL):
            if not step.script or not step.script.strip():
                errors.append(f"Step '{step.id}': script cannot be empty")

        # barrier 步骤必须声明 barrier_name（§6.3.7）
        if step.type == StepType.BARRIER:
            if not step.barrier_name:
                errors.append(f"Step '{step.id}': barrier step requires 'barrier_name'")

        # fixture_control 步骤必须声明 action 与 fixture_id（§6.7.1）
        if step.type == StepType.FIXTURE_CONTROL:
            if not step.action:
                errors.append(
                    f"Step '{step.id}': fixture_control step requires 'action' "
                    "(clamp/release/set_route/read_sensor)"
                )
            if not step.fixture_id:
                errors.append(f"Step '{step.id}': fixture_control step requires 'fixture_id'")

        if step.timeout < 0:
            errors.append(f"Step '{step.id}': timeout must be non-negative")

        if step.retry < 0:
            errors.append(f"Step '{step.id}': retry must be non-negative")

    def _validate_loop(
        self,
        loop: YamlLoop,
        seen_ids: set[str],
        errors: list[str],
    ) -> None:
        """Validate a YamlLoop construct.

        Checks:
        - WHILE loops must have a condition or max_iterations safety limit
        - FOR loops should have a count
        - FOREACH loops should have a collection and iterator_var
        - Nested steps are validated recursively

        Args:
            loop: The loop to validate.
            seen_ids: Set of IDs seen so far (mutated in place).
            errors: List to append error messages to.
        """
        # WHILE loop without condition could be infinite
        if loop.loop_type == LoopType.WHILE:
            if not loop.condition and loop.max_iterations <= 0:
                errors.append(
                    f"Loop '{loop.id}': WHILE loop must have a condition or max_iterations > 0"
                )

        # FOR loop should have a count
        if loop.loop_type == LoopType.FOR and loop.count is None:
            errors.append(f"Loop '{loop.id}': FOR loop must have a 'count' field")

        # FOREACH loop should have a collection and iterator_var
        if loop.loop_type == LoopType.FOREACH:
            if not loop.collection:
                errors.append(f"Loop '{loop.id}': FOREACH loop must have a 'collection' field")
            if not loop.iterator_var:
                errors.append(f"Loop '{loop.id}': FOREACH loop must have an 'iterator_var' field")

        # Loop must have at least one nested step
        if not loop.steps:
            errors.append(f"Loop '{loop.id}': must have at least one step")

        # Recursively validate nested steps
        self._validate_steps(loop.steps, seen_ids, errors)
