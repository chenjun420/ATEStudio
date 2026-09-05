"""DSL YAML plan extraction (deterministic, task 12).

Parses YAML test plans with the REAL edge DSL parser
(:class:`ate_platform.dsl.parser.YamlParser`) — never ad-hoc YAML scraping —
and flattens them into one :class:`ExtractedPlan`: a requirement per
plan/product and one :class:`ExtractedStep` per executable step (steps nested
in loops / subsequences are flattened in document order; branch/barrier/
fixture-control/breakpoint steps all count as test steps).

A file that is not a DSL plan (e.g. the MockTCP protocol fixture, which has
no ``version``) fails the parser's own validation and is reported as
``None`` so batch extraction can log and skip it — never crash.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ate_platform.dsl.parser import YamlParser
from shared.dsl import StepType, YamlLoop, YamlStep

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExtractedStep:
    """One executable DSL step, flattened from the plan's step tree."""

    step_id: str
    sequence_index: int
    step_type: str
    title: str


@dataclass(frozen=True, slots=True)
class ExtractedPlan:
    """A parsed DSL plan mapped to requirement + test-case inputs."""

    plan_name: str
    requirement_code: str
    title: str
    steps: list[ExtractedStep] = field(default_factory=list)


def flatten_steps(items: list[YamlStep | YamlLoop]) -> list[YamlStep]:
    """Return every leaf :class:`YamlStep` in document order.

    Loop containers expand to their nested steps (a loop iteration executes
    the same step, so one test case per leaf step is sufficient for the
    traceability chain); branches/subsequences carry their nested steps in
    ``params`` at compile time and their branch step itself is a leaf.
    """
    out: list[YamlStep] = []
    for item in items:
        if isinstance(item, YamlLoop):
            out.extend(flatten_steps(item.steps))
        else:
            out.append(item)
    return out


def _step_type(step: YamlStep) -> str:
    """Canonical step-type id (untyped legacy steps are ``script``)."""
    if step.type is None:
        return StepType.SCRIPT.value
    return step.type.value


def extract_plan(path: str | Path) -> ExtractedPlan | None:
    """Parse one DSL YAML file into an :class:`ExtractedPlan`.

    Returns ``None`` (and logs a warning) when the file is not a parseable
    DSL plan — a non-plan YAML fixture or a malformed document is skipped,
    never fatal to a batch extraction.
    """
    try:
        plan = YamlParser().parse(Path(path))
    except Exception as exc:  # noqa: BLE001 - non-plan/malformed YAML: skip+log
        logger.warning("Skipping DSL plan %s: not a parseable DSL plan (%s)", path, exc)
        return None

    leaves = flatten_steps(plan.steps)
    steps = [
        ExtractedStep(
            step_id=step.id,
            sequence_index=index,
            step_type=_step_type(step),
            title=(step.script or step.action or step.barrier_name or step.id),
        )
        for index, step in enumerate(leaves)
    ]
    return ExtractedPlan(
        plan_name=plan.name,
        requirement_code=f"REQ-DSL-{_slug(plan.name)}",
        title=f"DSL plan '{plan.name}' (v{plan.version})",
        steps=steps,
    )


def _slug(text: str) -> str:
    """Deterministic uppercase-safe slug for stable requirement/case codes."""
    import re

    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").upper()


__all__ = ["ExtractedPlan", "ExtractedStep", "extract_plan", "flatten_steps"]
