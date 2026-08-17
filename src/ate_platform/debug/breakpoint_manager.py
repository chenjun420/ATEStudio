"""Breakpoint manager for the debugpy breakpoint debugging framework.

BreakpointManager maintains an in-memory breakpoint registry for the current
debug session and provides X6 node serialisation for canvas restoration.

The persistent store (SQLAlchemy ORM in ``ate_cloud.models.breakpoint``) is
accessed through the async DB session supplied by the API layer. This module
stays in ``ate_platform`` (no ``ate_cloud`` import) to respect the dependency
rule: ``ate_platform`` never imports ``ate_cloud``.

The manager works with plain ``BreakpointData`` dataclass values so that the
``ate_cloud`` API layer can translate between ORM rows and these dataclass
instances without leaking ORM objects into the platform layer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, cast

from uuid import uuid4


@dataclass(frozen=True, slots=True)
class BreakpointData:
    """Immutable breakpoint value object for cross-layer transfer.

    Attributes:
        id: Unique breakpoint identifier (UUID).
        session_id: Debug session identifier.
        step_id: Test step identifier.
        node_id: X6 graph node identifier.
        line_number: Line number within the script (1-based; 0 = any).
        condition: Conditional expression (None = unconditional).
        enabled: Whether the breakpoint is active.
        node_data: X6 node serialised data for canvas restoration.
    """

    id: str
    session_id: str
    step_id: str
    node_id: str
    line_number: int = 0
    condition: str | None = None
    enabled: bool = True
    node_data: dict[str, Any] | None = None


@dataclass
class BreakpointManager:
    """Manage the breakpoint list for a debug session.

    The manager keeps an in-memory index of breakpoints keyed by ``id`` for
    O(1) lookup, and a secondary index by ``step_id`` for fast matching
    during script execution (the DebugProcessExecutor queries breakpoints
    for the step about to run).

    X6 node serialisation: ``serialize_x6_node`` / ``deserialize_x6_node``
    convert between a live X6 node dict and the JSON-serialisable dict stored
    in ``BreakpointData.node_data``.
    """

    session_id: str
    _by_id: dict[str, BreakpointData] = field(default_factory=dict)
    _by_step: dict[str, list[str]] = field(default_factory=dict)

    def add(self, bp: BreakpointData) -> BreakpointData:
        """Register a breakpoint.

        Args:
            bp: The breakpoint to add. Its ``session_id`` must match the
                manager's ``session_id``.

        Returns:
            The added breakpoint (same reference).

        Raises:
            ValueError: If a breakpoint with the same id already exists, or
                the breakpoint's session_id does not match.
        """
        if bp.session_id != self.session_id:
            raise ValueError(
                f"Breakpoint session_id {bp.session_id!r} does not match "
                f"manager session_id {self.session_id!r}"
            )
        if bp.id in self._by_id:
            raise ValueError(f"Breakpoint {bp.id} already exists")
        self._by_id[bp.id] = bp
        step_list = self._by_step.setdefault(bp.step_id, [])
        step_list.append(bp.id)
        return bp

    def remove(self, bp_id: str) -> BreakpointData | None:
        """Remove a breakpoint by id.

        Args:
            bp_id: The breakpoint identifier to remove.

        Returns:
            The removed breakpoint, or ``None`` if not found.
        """
        bp = self._by_id.pop(bp_id, None)
        if bp is None:
            return None
        step_list = self._by_step.get(bp.step_id)
        if step_list is not None:
            try:
                step_list.remove(bp_id)
            except ValueError:
                pass
            if not step_list:
                self._by_step.pop(bp.step_id, None)
        return bp

    def get(self, bp_id: str) -> BreakpointData | None:
        """Get a breakpoint by id.

        Args:
            bp_id: The breakpoint identifier.

        Returns:
            The breakpoint, or ``None`` if not found.
        """
        return self._by_id.get(bp_id)

    def list_all(self) -> list[BreakpointData]:
        """List all breakpoints in this session.

        Returns:
            A list of all registered breakpoints.
        """
        return list(self._by_id.values())

    def list_for_step(self, step_id: str) -> list[BreakpointData]:
        """List enabled breakpoints for a given step.

        Used by the DebugProcessExecutor to decide whether to attach the
        debugpy listener before executing a step.

        Args:
            step_id: The step identifier to look up.

        Returns:
            A list of enabled breakpoints attached to the step.
        """
        ids = self._by_step.get(step_id, [])
        return [self._by_id[i] for i in ids if self._by_id[i].enabled]

    def update(self, bp_id: str, **kwargs: Any) -> BreakpointData | None:
        """Update breakpoint fields and return the new immutable value.

        Because ``BreakpointData`` is frozen, this replaces the stored
        instance with a new one built from the merged fields.

        Args:
            bp_id: The breakpoint identifier to update.
            **kwargs: Fields to update (step_id, node_id, line_number,
                condition, enabled, node_data).

        Returns:
            The updated breakpoint, or ``None`` if not found.
        """
        existing = self._by_id.get(bp_id)
        if existing is None:
            return None
        merged: dict[str, Any] = {
            "id": existing.id,
            "session_id": existing.session_id,
            "step_id": existing.step_id,
            "node_id": existing.node_id,
            "line_number": existing.line_number,
            "condition": existing.condition,
            "enabled": existing.enabled,
            "node_data": existing.node_data,
        }
        merged.update(kwargs)
        updated = BreakpointData(**merged)

        # Re-index if step_id changed
        if kwargs.get("step_id") is not None and kwargs["step_id"] != existing.step_id:
            old_list = self._by_step.get(existing.step_id)
            if old_list is not None:
                try:
                    old_list.remove(bp_id)
                except ValueError:
                    pass
                if not old_list:
                    self._by_step.pop(existing.step_id, None)
            new_list = self._by_step.setdefault(updated.step_id, [])
            new_list.append(bp_id)

        self._by_id[bp_id] = updated
        return updated

    def clear(self) -> None:
        """Remove all breakpoints from this session."""
        self._by_id.clear()
        self._by_step.clear()

    @staticmethod
    def serialize_x6_node(node: Any) -> dict[str, Any]:
        """Serialize an X6 node to a JSON-compatible dict for storage.

        Accepts either a raw dict (already serialised) or an X6 Node object
        with ``toJSON()``. The result is safe to store in the ``node_data``
        JSON column of the breakpoint table.

        Args:
            node: An X6 Node instance or a serialised dict.

        Returns:
            A JSON-serialisable dict representing the node.
        """
        if isinstance(node, dict):
            return cast(dict[str, Any], json.loads(json.dumps(node, default=str)))
        # X6 Node objects expose toJSON() returning a plain dict
        to_json = getattr(node, "toJSON", None)
        if callable(to_json):
            data = to_json()
            return cast(dict[str, Any], json.loads(json.dumps(data, default=str)))
        # Fallback: best-effort dict conversion
        return cast(
            dict[str, Any],
            json.loads(json.dumps(asdict(node) if hasattr(node, "__dataclass_fields__") else {}, default=str)),
        )

    @staticmethod
    def deserialize_x6_node(node_data: dict[str, Any] | None) -> dict[str, Any] | None:
        """Deserialize stored node_data back to an X6-compatible dict.

        Args:
            node_data: The stored JSON dict, or None.

        Returns:
            The same dict (already in X6-compatible form), or None.
        """
        if node_data is None:
            return None
        return cast(dict[str, Any], json.loads(json.dumps(node_data, default=str)))


def new_breakpoint_id() -> str:
    """Generate a new UUID4 breakpoint identifier."""
    return str(uuid4())
