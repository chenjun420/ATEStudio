"""Tests for BreakpointManager.

Tests cover:
- add: register breakpoints, session_id mismatch, duplicate id
- remove: by id, not found
- get: by id, not found
- list_all: all breakpoints
- list_for_step: enabled breakpoints for a step
- update: fields, step_id re-index, not found
- clear: remove all
- serialize/deserialize X6 node
- new_breakpoint_id: UUID4 format
"""

from __future__ import annotations

import pytest

from ate_platform.debug.breakpoint_manager import (
    BreakpointData,
    BreakpointManager,
    new_breakpoint_id,
)


def _make_bp(
    bp_id: str = "bp-1",
    session_id: str = "sess-1",
    step_id: str = "step-1",
    node_id: str = "node-1",
    line_number: int = 10,
    condition: str | None = None,
    enabled: bool = True,
    node_data: dict | None = None,
) -> BreakpointData:
    """Create a BreakpointData for testing."""
    return BreakpointData(
        id=bp_id,
        session_id=session_id,
        step_id=step_id,
        node_id=node_id,
        line_number=line_number,
        condition=condition,
        enabled=enabled,
        node_data=node_data,
    )


class TestBreakpointManagerAdd:
    """Tests for BreakpointManager.add()."""

    def test_add_breakpoint(self) -> None:
        """Should register a breakpoint and return it."""
        manager = BreakpointManager(session_id="sess-1")
        bp = _make_bp()

        result = manager.add(bp)

        assert result is bp
        assert manager.get("bp-1") is bp

    def test_add_session_id_mismatch(self) -> None:
        """Should raise ValueError when session_id does not match."""
        manager = BreakpointManager(session_id="sess-1")
        bp = _make_bp(session_id="sess-2")

        with pytest.raises(ValueError, match="session_id"):
            manager.add(bp)

    def test_add_duplicate_id(self) -> None:
        """Should raise ValueError when breakpoint id already exists."""
        manager = BreakpointManager(session_id="sess-1")
        manager.add(_make_bp(bp_id="bp-1"))

        with pytest.raises(ValueError, match="already exists"):
            manager.add(_make_bp(bp_id="bp-1", step_id="step-2"))

    def test_add_multiple_same_step(self) -> None:
        """Should index multiple breakpoints for the same step."""
        manager = BreakpointManager(session_id="sess-1")
        manager.add(_make_bp(bp_id="bp-1", step_id="step-1"))
        manager.add(_make_bp(bp_id="bp-2", step_id="step-1"))

        bps = manager.list_for_step("step-1")
        assert len(bps) == 2


class TestBreakpointManagerRemove:
    """Tests for BreakpointManager.remove()."""

    def test_remove_existing(self) -> None:
        """Should remove and return the breakpoint."""
        manager = BreakpointManager(session_id="sess-1")
        bp = _make_bp()
        manager.add(bp)

        result = manager.remove("bp-1")

        assert result is bp
        assert manager.get("bp-1") is None

    def test_remove_not_found(self) -> None:
        """Should return None when breakpoint does not exist."""
        manager = BreakpointManager(session_id="sess-1")

        result = manager.remove("nonexistent")

        assert result is None

    def test_remove_cleans_step_index(self) -> None:
        """Should clean up the step index when last bp for step is removed."""
        manager = BreakpointManager(session_id="sess-1")
        manager.add(_make_bp(bp_id="bp-1", step_id="step-1"))
        manager.remove("bp-1")

        assert manager.list_for_step("step-1") == []


class TestBreakpointManagerGet:
    """Tests for BreakpointManager.get()."""

    def test_get_existing(self) -> None:
        """Should return the breakpoint."""
        manager = BreakpointManager(session_id="sess-1")
        bp = _make_bp()
        manager.add(bp)

        result = manager.get("bp-1")

        assert result is bp

    def test_get_not_found(self) -> None:
        """Should return None when not found."""
        manager = BreakpointManager(session_id="sess-1")

        assert manager.get("nonexistent") is None


class TestBreakpointManagerList:
    """Tests for BreakpointManager.list_all() and list_for_step()."""

    def test_list_all_empty(self) -> None:
        """Should return empty list when no breakpoints."""
        manager = BreakpointManager(session_id="sess-1")

        assert manager.list_all() == []

    def test_list_all_with_breakpoints(self) -> None:
        """Should return all registered breakpoints."""
        manager = BreakpointManager(session_id="sess-1")
        manager.add(_make_bp(bp_id="bp-1"))
        manager.add(_make_bp(bp_id="bp-2", step_id="step-2"))

        result = manager.list_all()

        assert len(result) == 2

    def test_list_for_step_only_enabled(self) -> None:
        """Should only return enabled breakpoints for a step."""
        manager = BreakpointManager(session_id="sess-1")
        manager.add(_make_bp(bp_id="bp-1", step_id="step-1", enabled=True))
        manager.add(_make_bp(bp_id="bp-2", step_id="step-1", enabled=False))

        result = manager.list_for_step("step-1")

        assert len(result) == 1
        assert result[0].id == "bp-1"

    def test_list_for_step_no_breakpoints(self) -> None:
        """Should return empty list when step has no breakpoints."""
        manager = BreakpointManager(session_id="sess-1")

        assert manager.list_for_step("nonexistent") == []


class TestBreakpointManagerUpdate:
    """Tests for BreakpointManager.update()."""

    def test_update_fields(self) -> None:
        """Should update fields and return new immutable value."""
        manager = BreakpointManager(session_id="sess-1")
        manager.add(_make_bp(line_number=10, condition=None))

        result = manager.update("bp-1", line_number=20, condition="x > 5")

        assert result is not None
        assert result.line_number == 20
        assert result.condition == "x > 5"
        # Original is immutable - the stored value is the new one
        assert manager.get("bp-1").line_number == 20

    def test_update_step_id_reindexes(self) -> None:
        """Should re-index when step_id changes."""
        manager = BreakpointManager(session_id="sess-1")
        manager.add(_make_bp(bp_id="bp-1", step_id="step-1"))

        manager.update("bp-1", step_id="step-2")

        assert manager.list_for_step("step-1") == []
        assert len(manager.list_for_step("step-2")) == 1

    def test_update_not_found(self) -> None:
        """Should return None when breakpoint does not exist."""
        manager = BreakpointManager(session_id="sess-1")

        result = manager.update("nonexistent", line_number=20)

        assert result is None

    def test_update_enabled_toggle(self) -> None:
        """Should toggle the enabled state."""
        manager = BreakpointManager(session_id="sess-1")
        manager.add(_make_bp(enabled=True))

        result = manager.update("bp-1", enabled=False)

        assert result is not None
        assert result.enabled is False
        # Disabled breakpoints should not appear in list_for_step
        assert manager.list_for_step("step-1") == []


class TestBreakpointManagerClear:
    """Tests for BreakpointManager.clear()."""

    def test_clear_removes_all(self) -> None:
        """Should remove all breakpoints."""
        manager = BreakpointManager(session_id="sess-1")
        manager.add(_make_bp(bp_id="bp-1"))
        manager.add(_make_bp(bp_id="bp-2", step_id="step-2"))

        manager.clear()

        assert manager.list_all() == []
        assert manager.list_for_step("step-1") == []

    def test_clear_empty(self) -> None:
        """Should be a no-op when already empty."""
        manager = BreakpointManager(session_id="sess-1")

        manager.clear()  # Should not raise


class TestX6NodeSerialization:
    """Tests for BreakpointManager.serialize_x6_node / deserialize_x6_node."""

    def test_serialize_dict(self) -> None:
        """Should round-trip a dict node."""
        node = {"id": "node-1", "shape": "rect", "position": {"x": 10, "y": 20}}

        serialized = BreakpointManager.serialize_x6_node(node)
        deserialized = BreakpointManager.deserialize_x6_node(serialized)

        assert deserialized == node

    def test_serialize_none(self) -> None:
        """Should return None for None input."""
        assert BreakpointManager.deserialize_x6_node(None) is None

    def test_serialize_returns_independent_copy(self) -> None:
        """Should return a deep copy, not a reference."""
        node = {"id": "node-1", "data": {"label": "test"}}

        serialized = BreakpointManager.serialize_x6_node(node)
        serialized["id"] = "changed"

        assert node["id"] == "node-1"

    def test_serialize_with_to_json_method(self) -> None:
        """Should call toJSON() on objects that have it."""

        class FakeX6Node:
            # Name mirrors AntV X6's JS toJSON() hook that serialize_x6_node
            # duck-types via getattr(node, "toJSON"); renaming breaks the test.
            def toJSON(self) -> dict:  # noqa: N802
                return {"id": "fake", "shape": "rect"}

        result = BreakpointManager.serialize_x6_node(FakeX6Node())

        assert result == {"id": "fake", "shape": "rect"}


class TestNewBreakpointId:
    """Tests for new_breakpoint_id()."""

    def test_returns_uuid_string(self) -> None:
        """Should return a UUID4 string."""
        bp_id = new_breakpoint_id()

        # UUID4 format: 8-4-4-4-12 hex chars
        parts = bp_id.split("-")
        assert len(parts) == 5
        assert len(parts[0]) == 8
        assert len(parts[1]) == 4
        assert len(parts[2]) == 4
        assert len(parts[3]) == 4
        assert len(parts[4]) == 12

    def test_returns_unique_ids(self) -> None:
        """Should return unique IDs on each call."""
        id1 = new_breakpoint_id()
        id2 = new_breakpoint_id()

        assert id1 != id2


class TestBreakpointDataImmutable:
    """Tests for BreakpointData immutability."""

    def test_frozen_dataclass(self) -> None:
        """Should be immutable (frozen=True)."""
        bp = _make_bp()

        with pytest.raises(Exception):  # FrozenInstanceError  # noqa: B017
            bp.line_number = 99  # type: ignore[misc]

    def test_slots_dataclass(self) -> None:
        """Should use slots (no __dict__)."""
        bp = _make_bp()

        assert not hasattr(bp, "__dict__")
