"""Tests for ExecutionPlanMaterializer — loads Sequence YAML from DB → YamlPlan.

All tests mock the AsyncSession via AsyncMock for session.get, and use
MagicMock(spec=...) for Execution and Sequence model instances.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models import Execution, Sequence
from ate_cloud.services.plan_materializer import ExecutionPlanMaterializer
from shared.dsl import ExecutionMode, LoopType, YamlLoop, YamlPlan, YamlStep

# Sample YAML exercising both YamlStep and YamlLoop (with nested step).
_SAMPLE_YAML = """\
name: test-plan
version: "1.0"
scope:
  variables:
    dut_id: "DUT-001"
max_concurrency: 2
steps:
  - id: step-1
    script: scripts/test_voltage.py
    params:
      voltage: 5.0
    timeout: 30
  - id: loop-1
    loop_type: FOR
    count: 3
    execution_mode: SERIAL
    steps:
      - id: step-2
        script: scripts/test_current.py
        params:
          current: 1.5
"""


def _make_execution(sequence_id: str | None = "seq-123") -> MagicMock:
    """Build a MagicMock spec'd to Execution with a sequence_id."""
    execution = MagicMock(spec=Execution)
    execution.sequence_id = sequence_id
    return execution


def _make_sequence(yaml_content: str) -> MagicMock:
    """Build a MagicMock spec'd to Sequence with yaml_content."""
    sequence = MagicMock(spec=Sequence)
    sequence.yaml_content = yaml_content
    return sequence


def _make_session(execution: MagicMock, sequence: MagicMock) -> AsyncMock:
    """Build an AsyncMock session whose get() returns execution then sequence."""
    session = AsyncMock(spec=AsyncSession)
    session.get = AsyncMock(side_effect=[execution, sequence])
    return session


class TestExecutionPlanMaterializer:
    """Tests for ExecutionPlanMaterializer.materialize()."""

    @pytest.mark.asyncio
    async def test_materialize_loads_sequence(self) -> None:
        """materialize loads Execution by execution_id then Sequence by sequence_id."""
        execution = _make_execution(sequence_id="seq-123")
        sequence = _make_sequence(_SAMPLE_YAML)
        session = _make_session(execution, sequence)

        materializer = ExecutionPlanMaterializer(session)
        await materializer.materialize("exec-001")

        # session.get called twice: first (Execution, exec_id), then (Sequence, seq_id)
        assert session.get.await_count == 2
        session.get.assert_any_await(Execution, "exec-001")
        session.get.assert_any_await(Sequence, "seq-123")

    @pytest.mark.asyncio
    async def test_materialize_parses_yaml(self) -> None:
        """materialize parses yaml_content into a YamlPlan with correct fields."""
        execution = _make_execution()
        sequence = _make_sequence(_SAMPLE_YAML)
        session = _make_session(execution, sequence)

        materializer = ExecutionPlanMaterializer(session)
        plan = await materializer.materialize("exec-001")

        # Top-level plan fields
        assert plan.name == "test-plan"
        assert plan.version == "1.0"
        assert plan.max_concurrency == 2
        assert plan.scope == {"variables": {"dut_id": "DUT-001"}}
        assert len(plan.steps) == 2

        # First step — plain YamlStep
        step1 = plan.steps[0]
        assert isinstance(step1, YamlStep)
        assert step1.id == "step-1"
        assert step1.script == "scripts/test_voltage.py"
        assert step1.params == {"voltage": 5.0}
        assert step1.timeout == 30

        # Second step — YamlLoop with a nested step
        loop1 = plan.steps[1]
        assert isinstance(loop1, YamlLoop)
        assert loop1.id == "loop-1"
        assert loop1.loop_type == LoopType.FOR
        assert loop1.count == 3
        assert loop1.execution_mode == ExecutionMode.SERIAL
        assert len(loop1.steps) == 1

        nested = loop1.steps[0]
        assert isinstance(nested, YamlStep)
        assert nested.id == "step-2"
        assert nested.script == "scripts/test_current.py"
        assert nested.params == {"current": 1.5}

    @pytest.mark.asyncio
    async def test_materialize_returns_yaml_plan(self) -> None:
        """materialize returns a YamlPlan instance (not a dict or other type)."""
        execution = _make_execution()
        sequence = _make_sequence(_SAMPLE_YAML)
        session = _make_session(execution, sequence)

        materializer = ExecutionPlanMaterializer(session)
        result = await materializer.materialize("exec-001")

        assert isinstance(result, YamlPlan)
        assert result.steps  # non-empty steps list

    @pytest.mark.asyncio
    async def test_no_cache(self) -> None:
        """Calling materialize twice with the same ID hits the DB twice (no caching)."""
        execution = _make_execution()
        sequence = _make_sequence(_SAMPLE_YAML)
        session = AsyncMock(spec=AsyncSession)
        # Each materialize call does 2 gets (Execution + Sequence); 2 calls = 4 gets.
        session.get = AsyncMock(side_effect=[execution, sequence, execution, sequence])

        materializer = ExecutionPlanMaterializer(session)
        plan_a = await materializer.materialize("exec-001")
        plan_b = await materializer.materialize("exec-001")

        # Both calls produced a valid plan.
        assert isinstance(plan_a, YamlPlan)
        assert isinstance(plan_b, YamlPlan)
        # session.get was called 4 times total — no caching shortcut.
        assert session.get.await_count == 4
