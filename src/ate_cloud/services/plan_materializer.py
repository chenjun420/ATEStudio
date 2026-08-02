"""Execution plan materializer — loads Sequence YAML from DB and builds YamlPlan.

Loads an Execution record by ID, follows its sequence_id to the Sequence model,
parses the Sequence's yaml_content via yaml.safe_load, and constructs a YamlPlan
ready for dispatch. Does NOT cache — every call hits the database fresh.
Does NOT dispatch — only materializes.
"""

from __future__ import annotations

from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models import Execution, Sequence
from shared.dsl import (
    ExecutionMode,
    LoopType,
    YamlLoop,
    YamlPlan,
    YamlStep,
)


class PlanMaterializeError(Exception):
    """Raised when an execution plan cannot be materialized from the database."""


class ExecutionPlanMaterializer:
    """Materializes a YamlPlan from the database for a given execution.

    Accepts an AsyncSession in the constructor. The materialize() method loads
    the Execution, resolves its Sequence, parses the stored YAML, and returns a
    YamlPlan. No caching — each call performs fresh database reads.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def materialize(self, execution_id: str) -> YamlPlan:
        """Load Execution → Sequence → parse YAML → return YamlPlan.

        Args:
            execution_id: The execution record ID (= run_id).

        Returns:
            A freshly constructed YamlPlan from the Sequence's yaml_content.

        Raises:
            PlanMaterializeError: If the execution or sequence is not found,
                the YAML content is empty/None, or the YAML is malformed.
        """
        execution = await self._session.get(Execution, execution_id)
        if execution is None:
            raise PlanMaterializeError(f"execution {execution_id} not found")

        sequence_id = execution.sequence_id
        if sequence_id is None:
            raise PlanMaterializeError(f"execution {execution_id} has no sequence_id")

        sequence = await self._session.get(Sequence, sequence_id)
        if sequence is None:
            raise PlanMaterializeError(f"sequence {sequence_id} not found")

        yaml_content = sequence.yaml_content
        if not yaml_content:
            raise PlanMaterializeError("sequence has no YAML content")

        try:
            data = yaml.safe_load(yaml_content)
        except yaml.YAMLError as e:
            raise PlanMaterializeError(f"YAML parse error: {e}") from e

        if data is None:
            raise PlanMaterializeError("empty YAML document")

        if not isinstance(data, dict):
            raise PlanMaterializeError("YAML content must be a mapping, got " + type(data).__name__)

        try:
            return _build_plan(data)
        except (ValueError, TypeError, KeyError) as e:
            raise PlanMaterializeError(f"failed to build plan: {e}") from e


# ---------------------------------------------------------------------------
# YAML dict → YamlPlan construction (module-private helpers)
# ---------------------------------------------------------------------------


def _build_plan(data: dict[str, Any]) -> YamlPlan:
    """Construct a YamlPlan from the parsed YAML mapping."""
    scope = data.get("scope", {})
    if isinstance(scope, str):
        scope = {"name": scope}
    if not isinstance(scope, dict):
        scope = {}

    steps_data = data.get("steps", [])
    steps = [_build_step_or_loop(s) for s in steps_data] if isinstance(steps_data, list) else []

    return YamlPlan(
        name=data.get("name", ""),
        version=str(data.get("version", "")),
        scope=scope,
        max_concurrency=data.get("max_concurrency", 1),
        steps=steps,
    )


def _build_step_or_loop(data: dict[str, Any]) -> YamlStep | YamlLoop:
    """Dispatch to step or loop builder based on presence of 'loop_type' key."""
    if "loop_type" in data:
        return _build_loop(data)
    return _build_step(data)


def _build_step(data: dict[str, Any]) -> YamlStep:
    """Construct a YamlStep from a YAML mapping."""
    return YamlStep(
        id=data.get("id", ""),
        script=data.get("script", ""),
        params=data.get("params", {}),
        preconditions=data.get("preconditions", []),
        resources=data.get("resources", {}),
        timeout=data.get("timeout", 60),
        retry=data.get("retry", 0),
        on_fail=data.get("on_fail"),
        export_outputs=data.get("export_outputs", False),
        skip_if=data.get("skip_if"),
        skip_reason=data.get("skip_reason"),
    )


def _build_loop(data: dict[str, Any]) -> YamlLoop:
    """Construct a YamlLoop from a YAML mapping."""
    loop_type_str = data.get("loop_type", "FOR")
    loop_type = LoopType(str(loop_type_str).upper())

    exec_mode_str = data.get("execution_mode", "SERIAL")
    execution_mode = ExecutionMode(str(exec_mode_str).upper())

    steps_data = data.get("steps", [])
    steps = [_build_step_or_loop(s) for s in steps_data] if isinstance(steps_data, list) else []

    return YamlLoop(
        id=data.get("id", ""),
        loop_type=loop_type,
        steps=steps,
        count=data.get("count"),
        condition=data.get("condition"),
        collection=data.get("collection"),
        iterator_var=data.get("iterator_var"),
        execution_mode=execution_mode,
        max_iterations=data.get("max_iterations", 1000),
        skip_if=data.get("skip_if"),
        skip_reason=data.get("skip_reason"),
    )
