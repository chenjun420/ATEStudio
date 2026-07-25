"""YAML DSL Parser for ATE Platform."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from shared.dsl import ExecutionMode, LoopType, YamlLoop, YamlPlan, YamlStep


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

        Detects whether the entry is a loop (has 'loop_type' key) or a
        regular step and delegates to the appropriate parser.

        Args:
            data: Dictionary containing step or loop data.

        Returns:
            Parsed YamlStep or YamlLoop object.
        """
        if "loop_type" in data:
            return self._parse_loop(data)
        return self._parse_step(data)

    def _parse_step(self, data: dict[str, Any]) -> YamlStep:
        """Parse a single step from YAML data.

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

        script = data.get("script")
        if not script:
            raise ValueError(f"Step '{step_id}' missing required field: 'script'")

        return YamlStep(
            id=step_id,
            script=script,
            params=data.get("params", {}),
            preconditions=data.get("preconditions", []),
            resources=data.get("resources", {}),
            timeout=data.get("timeout", 60),
            retry=data.get("retry", 0),
            on_fail=data.get("on_fail"),
            export_outputs=data.get("export_outputs", False),
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
        if not step.script or not step.script.strip():
            errors.append(f"Step '{step.id}': script cannot be empty")

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