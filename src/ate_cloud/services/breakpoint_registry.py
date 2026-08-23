"""Typed simulation breakpoints (T39, v41-gap-analysis #39).

An in-memory, per-run registry of typed breakpoints for the SimulationConsole
(§8.4): four kinds — ``step`` (step id), ``instrument_call`` (resource.method),
``variable_change`` (scope.key) and ``condition`` (simpleeval-subset expression
evaluated SERVER-SIDE only; the client never evaluates conditions).

Hit detection runs in the cloud layer inside :class:`ExecutionStatusRelay`:
every status event streamed from the edge is matched against the run's
breakpoints via :func:`handle_status_event`. On a hit the relay publishes a
SSE ``BREAKPOINT_HIT`` event ``{breakpoint_id, kind, target, context}`` and
reuses the EXISTING pause control contract verbatim (Core NATS
``ate.control.{run_id}`` with ``{"action": "pause", "run_id": ...}``) — the
wire shape is unchanged, only the trigger differs.

The registry is intentionally in-memory (like SSEBridge): breakpoints arm a
running execution; persistence across restarts is out of scope for T39.
"""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from simpleeval import simple_eval

logger = logging.getLogger(__name__)

#: §8.4 breakpoint kinds.
BREAKPOINT_KINDS: tuple[str, ...] = (
    "step",
    "instrument_call",
    "variable_change",
    "condition",
)

#: AST node whitelist for the simpleeval-subset condition language.
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
class TypedBreakpoint:
    """Immutable typed breakpoint value.

    Attributes:
        id: Unique breakpoint identifier.
        run_id: Execution run the breakpoint arms.
        kind: One of BREAKPOINT_KINDS.
        target: Match target (step id / resource.method / scope.key / "*").
        condition: Expression evaluated server-side (condition kind only).
        enabled: Disabled breakpoints never hit.
    """

    id: str
    run_id: str
    kind: str
    target: str
    condition: str | None = None
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable dict matching BreakpointResponse."""
        return {
            "id": self.id,
            "run_id": self.run_id,
            "kind": self.kind,
            "target": self.target,
            "condition": self.condition,
            "enabled": self.enabled,
        }


def validate_condition_syntax(expression: str) -> None:
    """Validate an expression against the simpleeval subset grammar.

    Parses with :mod:`ast` and whitelists node types (no calls, attributes or
    subscripts) so malformed conditions are rejected at creation time.

    Args:
        expression: The candidate condition expression.

    Raises:
        ValueError: If the expression is empty, unparsable, or uses nodes
            outside the allowed subset.
    """
    if not expression or not expression.strip():
        raise ValueError("condition must be a non-empty expression")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as e:
        raise ValueError(f"malformed condition expression: {e.msg}") from e
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_CONDITION_NODES):
            raise ValueError(
                f"condition uses unsupported syntax: {type(node).__name__}"
            )


def evaluate_condition(expression: str, names: dict[str, Any]) -> bool:
    """Evaluate a condition server-side against event context names.

    Evaluation errors (missing names, type errors) count as NO hit — a broken
    expression must never crash the relay loop.

    Args:
        expression: The validated condition expression.
        names: Variable context (e.g. ``{"voltage": 3.3}``).

    Returns:
        True when the expression evaluates truthy.
    """
    try:
        return bool(simple_eval(expression, names=names))
    except Exception as e:  # noqa: BLE001 — any eval failure means no hit
        logger.debug("Breakpoint condition %r failed: %s", expression, e)
        return False


def validate_breakpoint(kind: str, target: str, condition: str | None) -> None:
    """Validate a breakpoint creation request.

    Args:
        kind: Requested breakpoint kind.
        target: Match target (must be non-empty).
        condition: Optional expression — non-empty ONLY for the condition kind.

    Raises:
        ValueError: On unknown kind, empty target, empty/malformed condition
            for the condition kind, or a condition supplied for any other kind.
    """
    if kind not in BREAKPOINT_KINDS:
        raise ValueError(
            f"unknown breakpoint kind {kind!r}; allowed: {list(BREAKPOINT_KINDS)}"
        )
    if not target or not target.strip():
        raise ValueError("target must be a non-empty string")
    if kind == "condition":
        validate_condition_syntax(condition or "")
    elif condition is not None and condition.strip():
        raise ValueError("condition is only allowed for the 'condition' kind")


class BreakpointRegistry:
    """In-memory per-run registry of typed breakpoints."""

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._by_run: dict[str, dict[str, TypedBreakpoint]] = {}

    def add(self, bp: TypedBreakpoint) -> TypedBreakpoint:
        """Register a breakpoint for its run."""
        self._by_run.setdefault(bp.run_id, {})[bp.id] = bp
        return bp

    def remove(self, run_id: str, bp_id: str) -> bool:
        """Remove a breakpoint; idempotent (False when absent)."""
        run_bps = self._by_run.get(run_id)
        if run_bps is None or bp_id not in run_bps:
            return False
        del run_bps[bp_id]
        if not run_bps:
            self._by_run.pop(run_id, None)
        return True

    def get(self, bp_id: str) -> TypedBreakpoint | None:
        """Find a breakpoint by id across all runs."""
        for run_bps in self._by_run.values():
            bp = run_bps.get(bp_id)
            if bp is not None:
                return bp
        return None

    def disable(self, bp_id: str) -> bool:
        """Disable a breakpoint by id (True when found)."""
        bp = self.get(bp_id)
        if bp is None:
            return False
        self.add(TypedBreakpoint(
            id=bp.id, run_id=bp.run_id, kind=bp.kind, target=bp.target,
            condition=bp.condition, enabled=False,
        ))
        return True

    def list_for_run(self, run_id: str) -> list[TypedBreakpoint]:
        """List all breakpoints registered for a run."""
        return list(self._by_run.get(run_id, {}).values())

    def clear_run(self, run_id: str) -> None:
        """Drop every breakpoint for a run (used on terminal transitions)."""
        self._by_run.pop(run_id, None)

    def check_hit(
        self,
        run_id: str,
        kind: str,
        target: str,
        context: dict[str, Any] | None = None,
    ) -> list[TypedBreakpoint]:
        """Return enabled breakpoints of ``kind`` matching this observation.

        Matching rules per kind:
        - ``step``: exact target match on the step id.
        - ``instrument_call``: bp target ``resource.method`` matches when the
          resource part equals the observed instrument id AND (when the bp
          declares a method) the observed method equals it.
        - ``variable_change``: exact match on ``scope.key`` name.
        - ``condition``: wildcard target ``*`` (or any) — hits when the
          expression evaluates truthy against ``context``.
        """
        ctx = context or {}
        hits: list[TypedBreakpoint] = []
        for bp in self.list_for_run(run_id):
            if not bp.enabled or bp.kind != kind:
                continue
            if kind == "step":
                if bp.target == target:
                    hits.append(bp)
            elif kind == "instrument_call":
                resource, _, method = bp.target.partition(".")
                if resource == target and (not method or method == ctx.get("method")):
                    hits.append(bp)
            elif kind == "variable_change":
                if bp.target == target:
                    hits.append(bp)
            elif kind == "condition":
                if evaluate_condition(bp.condition or "", ctx):
                    hits.append(bp)
        return hits


def new_breakpoint_id() -> str:
    """Generate a fresh breakpoint identifier."""
    return str(uuid4())


async def handle_status_event(
    registry: BreakpointRegistry,
    bridge: Any,
    nc: Any,
    event: dict[str, Any],
) -> None:
    """Match one relayed status event against the run's breakpoints.

    Event mapping (cloud-side equivalent of "scheduler/sim loop checks before
    step dispatch / instrument call / variable set"):
    - ``STEP_STARTED`` → step-kind candidates on ``step_id``
    - ``measurement_recorded`` with ``instrument_id`` → instrument_call
      candidates on the instrument id (+ optional ``method`` field)
    - ``measurement_recorded`` without instrument → variable_change
      candidates on ``name`` (``scope.key``)
    - every event additionally feeds condition-kind evaluation with the
      flattened payload as names.

    Each hit publishes a SSE ``BREAKPOINT_HIT`` event and reuses the existing
    pause contract verbatim: Core NATS ``ate.control.{run_id}`` with
    ``{"action": "pause", "run_id": ...}`` (identical wire shape to the
    POST /pause endpoint — NOT a new contract).

    Args:
        registry: The breakpoint registry.
        bridge: SSEBridge used to publish BREAKPOINT_HIT events.
        nc: NATS client (or None/mock) for the pause control message.
        event: The parsed status event dict.
    """
    run_id = event.get("run_id")
    if not run_id:
        return
    etype = event.get("type", "")
    context = {k: v for k, v in event.items() if k != "type"}

    # Condition-kind breakpoints observe every event's flattened payload.
    # Measurement names ("scope.key") are additionally exposed under their
    # bare key so conditions read naturally: "voltage > 3.0".
    eval_names = dict(context)
    var_name = event.get("name")
    if var_name and isinstance(event.get("new_value"), (bool, int, float, str)):
        eval_names[var_name] = event["new_value"]
        eval_names[var_name.rsplit(".", 1)[-1]] = event["new_value"]

    observations: list[tuple[str, str, dict[str, Any]]] = []
    if etype == "STEP_STARTED" and event.get("step_id"):
        observations.append(("step", event["step_id"], context))
    elif etype == "measurement_recorded":
        instrument_id = event.get("instrument_id")
        if instrument_id:
            observations.append((
                "instrument_call", instrument_id,
                {**context, "method": event.get("method")},
            ))
        if event.get("name"):
            observations.append((
                "variable_change", event["name"],
                {**context, "value": event.get("new_value")},
            ))
    observations.append(("condition", "*", eval_names))

    for kind, target, obs_ctx in observations:
        for bp in registry.check_hit(run_id, kind, target, obs_ctx):
            logger.info(
                "Breakpoint %s (%s) hit for run %s at %r", bp.id, bp.kind, run_id, target,
            )
            await bridge.publish_event(
                run_id=run_id,
                event_type="BREAKPOINT_HIT",
                data={
                    "breakpoint_id": bp.id,
                    "kind": bp.kind,
                    "target": bp.target,
                    "context": obs_ctx,
                },
            )
            # Reuse the existing pause control contract verbatim (non-fatal).
            try:
                if nc is not None:
                    await nc.publish(
                        f"ate.control.{run_id}",
                        json.dumps({"action": "pause", "run_id": run_id}).encode(),
                    )
            except Exception as e:  # noqa: BLE001 — pause is best-effort here
                logger.warning("Failed to publish breakpoint pause control: %s", e)
