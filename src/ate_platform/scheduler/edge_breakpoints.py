"""Edge-side evaluation of typed breakpoints (T39/T40, task 20).

The cloud persists typed breakpoints (kind/target/condition/enabled) and
pushes the definitions to the edge worker over NATS; the edge evaluates them
ITSELF — the cloud never issues a pause per hit:

- :func:`parse_breakpoint_defs` tolerantly decodes the wire payload (a list of
  breakpoint dicts in the cloud ``TypedBreakpoint`` shape). Malformed entries
  are dropped and counted — a bad def never blocks/hangs a run.
- :class:`EdgeBreakpointEngine` matches an about-to-dispatch step against the
  armed defs. Only ``step``/``condition`` kinds are observable at the
  scheduler's step-dispatch gate (the single point every plan step crosses);
  ``instrument_call``/``variable_change`` defs are stored but not hit here.
- :func:`build_variable_snapshot` captures the current :class:`VariableSpace`
  for the hit event (reuses the existing store — no new variable source).

On a hit the scheduler reuses the EXISTING pause/resume gate (``pause()`` /
``_pause_event`` / ``resume()``) — the same gate T40 step-mode and the DSL
BREAKPOINT step use. This module contains no gate of its own.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from typing import Any

from simpleeval import simple_eval

from .variable_space import VariableSpace

logger = logging.getLogger(__name__)

#: Breakpoint kinds understood by the edge. Kept in sync with the cloud
#: registry (``ate_cloud.services.breakpoint_registry.BREAKPOINT_KINDS``) —
#: duplicated here intentionally so the edge never imports the cloud package.
EDGE_BREAKPOINT_KINDS: frozenset[str] = frozenset(
    ("step", "instrument_call", "variable_change", "condition"),
)

#: Kinds the scheduler's step-dispatch gate can actually observe.
_STEP_GATE_KINDS: frozenset[str] = frozenset(("step", "condition"))

#: AST nodes allowed inside a breakpoint condition (no calls/attributes/
#: subscripts — mirrors the cloud simpleeval subset).
_ALLOWED_CONDITION_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Name,
    ast.Load,
    ast.Constant,
)


@dataclass(frozen=True, slots=True)
class EdgeBreakpoint:
    """Immutable edge-side breakpoint definition (wire shape, decoded)."""

    id: str
    kind: str
    target: str
    condition: str | None = None
    enabled: bool = True


def _condition_is_safe(expression: str) -> bool:
    """Return True when ``expression`` parses within the allowed AST subset."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        return False
    return all(isinstance(node, _ALLOWED_CONDITION_NODES) for node in ast.walk(tree))


def parse_breakpoint_defs(raw: Any) -> tuple[list[EdgeBreakpoint], int]:
    """Tolerantly decode a breakpoint-defs payload into edge breakpoints.

    Args:
        raw: The decoded payload — expected to be a list of breakpoint dicts
            shaped like the cloud ``TypedBreakpoint.to_dict()``
            (``id``/``kind``/``target``/``condition``/``enabled``).

    Returns:
        ``(breakpoints, dropped)`` — the valid definitions and the count of
        malformed entries skipped. A non-list payload yields ``([], 0)`` (no
        defs to arm; a wrong envelope is treated as "no breakpoints", never an
        error that blocks execution).
    """
    if not isinstance(raw, list):
        logger.warning(
            "Breakpoint defs payload is not a list (%s) — running without "
            "edge breakpoints",
            type(raw).__name__,
        )
        return [], 0

    parsed: list[EdgeBreakpoint] = []
    dropped = 0
    for index, item in enumerate(raw):
        bp = _parse_one(item, index)
        if bp is None:
            dropped += 1
        else:
            parsed.append(bp)

    if dropped:
        logger.warning(
            "Edge breakpoint defs: %d valid, %d malformed (skipped, no suspend)",
            len(parsed),
            dropped,
        )
    return parsed, dropped


def _parse_one(item: Any, index: int) -> EdgeBreakpoint | None:
    """Decode one breakpoint entry; return None (and log) when malformed."""
    if not isinstance(item, dict):
        logger.warning("Breakpoint def #%d is not an object — skipped", index)
        return None

    bp_id = item.get("id")
    kind = item.get("kind")
    target = item.get("target")
    condition = item.get("condition")
    enabled = item.get("enabled", True)

    if not isinstance(bp_id, str) or not bp_id.strip():
        logger.warning("Breakpoint def #%d missing string 'id' — skipped", index)
        return None
    if not isinstance(kind, str) or kind not in EDGE_BREAKPOINT_KINDS:
        logger.warning(
            "Breakpoint %r: unknown/missing kind %r — skipped", bp_id, kind,
        )
        return None
    if not isinstance(target, str) or not target.strip():
        logger.warning("Breakpoint %r: missing non-empty 'target' — skipped", bp_id)
        return None
    if not isinstance(enabled, bool):
        logger.warning("Breakpoint %r: non-boolean 'enabled' — skipped", bp_id)
        return None

    if kind == "condition":
        if not isinstance(condition, str) or not condition.strip():
            logger.warning(
                "Breakpoint %r: condition kind requires a non-empty expression — "
                "skipped",
                bp_id,
            )
            return None
        if not _condition_is_safe(condition):
            logger.warning(
                "Breakpoint %r: malformed/unsafe condition %r — skipped",
                bp_id,
                condition,
            )
            return None
    elif condition is not None and str(condition).strip():
        # A condition on a non-condition kind is a shape error — drop it
        # rather than silently ignoring half the definition.
        logger.warning(
            "Breakpoint %r: 'condition' is only valid for the condition kind "
            "— skipped",
            bp_id,
        )
        return None
    else:
        condition = None

    return EdgeBreakpoint(
        id=bp_id,
        kind=kind,
        target=target,
        condition=condition,
        enabled=enabled,
    )


def build_variable_snapshot(variable_space: VariableSpace | None) -> dict[str, Any]:
    """Capture the current variable space for a breakpoint-hit event.

    Reuses :meth:`VariableSpace.snapshot` (scope/steps/loop) — the existing
    variable store the scheduler already owns. Values are returned as-is; the
    NATS forwarder JSON-encodes the payload, so non-serialisable objects are
    best-effort coerced to strings.
    """
    if variable_space is None:
        return {"scope": {}, "steps": {}, "loop": {}}
    snapshot = variable_space.snapshot()
    return {
        "scope": _json_safe(snapshot.get("scope", {})),
        "steps": _json_safe(snapshot.get("steps", {})),
        "loop": _json_safe(snapshot.get("loop", {})),
    }


def _json_safe(value: Any) -> Any:
    """Best-effort coerce a value tree to JSON-serialisable scalars."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class EdgeBreakpointEngine:
    """Holds armed edge breakpoints and evaluates them at the step gate.

    A breakpoint fires at most ONCE per arming (recorded in ``_fired``) so a
    step breakpoint suspends at its step rather than re-suspending on every
    later dispatch pass. :meth:`replace` re-arms (and resets the fired set)
    whenever a fresh definition set arrives.
    """

    def __init__(self, breakpoints: list[EdgeBreakpoint] | None = None) -> None:
        self._breakpoints: list[EdgeBreakpoint] = list(breakpoints or [])
        self._fired: set[str] = set()

    def replace(self, breakpoints: list[EdgeBreakpoint]) -> None:
        """Atomically re-arm with a fresh definition set (resetting fired)."""
        self._breakpoints = list(breakpoints)
        self._fired.clear()

    @property
    def breakpoints(self) -> tuple[EdgeBreakpoint, ...]:
        """The currently armed definitions."""
        return tuple(self._breakpoints)

    def check_step(
        self,
        step_id: str,
        variables: dict[str, Any],
    ) -> EdgeBreakpoint | None:
        """Return the first armed breakpoint that fires before ``step_id``.

        Args:
            step_id: The step about to dispatch.
            variables: The variable snapshot (scope/steps/loop) used to
                evaluate condition-kind breakpoints.

        Returns:
            The firing breakpoint (marked as fired), or None. Evaluation
            errors never raise — a broken condition simply does not hit.
        """
        names = _condition_names(variables)
        for bp in self._breakpoints:
            if not bp.enabled or bp.id in self._fired:
                continue
            if bp.kind not in _STEP_GATE_KINDS:
                continue
            if bp.kind == "step" and bp.target != step_id:
                continue
            if bp.kind == "condition" and not _evaluate(bp.condition or "", names):
                continue
            self._fired.add(bp.id)
            return bp
        return None


def _condition_names(variables: dict[str, Any]) -> dict[str, Any]:
    """Flatten the variable snapshot into bare-name bindings for conditions.

    Conditions read naturally (``voltage > 3.0``); expose both the scope
    variables (already bare-keyed) and the full dotted keys.
    """
    names: dict[str, Any] = {}
    scope = variables.get("scope")
    if isinstance(scope, dict):
        names.update(scope)
    return names


def _evaluate(expression: str, names: dict[str, Any]) -> bool:
    """Evaluate a condition expression; any failure means NO hit (never raise)."""
    try:
        return bool(simple_eval(expression, names=names))
    except Exception as exc:  # noqa: BLE001 — eval failure must not crash the gate
        logger.debug("Edge breakpoint condition %r failed: %s", expression, exc)
        return False
