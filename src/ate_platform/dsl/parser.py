"""YAML DSL Parser for ATE Platform."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from shared.dsl import YamlPlan, YamlStep


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

        # Extract optional fields with defaults
        max_concurrency = data.get("max_concurrency", 1)

        # Parse steps
        steps_data = data.get("steps", [])
        steps = []
        for step_data in steps_data:
            step = self._parse_step(step_data)
            steps.append(step)

        return YamlPlan(
            name=name,
            version=version,
            scope=scope,
            max_concurrency=max_concurrency,
            steps=steps,
        )

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

        if not plan.scope or not plan.scope.strip():
            errors.append("Plan scope cannot be empty")

        if plan.max_concurrency < 1:
            errors.append("max_concurrency must be at least 1")

        # Validate steps
        step_ids: set[str] = set()
        for step in plan.steps:
            # Check for duplicate IDs
            if step.id in step_ids:
                errors.append(f"Duplicate step ID: '{step.id}'")
            step_ids.add(step.id)

            # Validate step fields
            if not step.script or not step.script.strip():
                errors.append(f"Step '{step.id}': script cannot be empty")

            if step.timeout < 0:
                errors.append(f"Step '{step.id}': timeout must be non-negative")

            if step.retry < 0:
                errors.append(f"Step '{step.id}': retry must be non-negative")

            # Validate preconditions reference existing steps
            for precond in step.preconditions:
                if precond not in step_ids:
                    # Note: This is a soft check - precondition might be defined later
                    # We'll allow it but could flag it as a warning
                    pass

        # Check for at least one step
        if not plan.steps:
            errors.append("Plan must have at least one step")

        return errors