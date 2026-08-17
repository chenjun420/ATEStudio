"""Variable space management for ATE Platform.

This module provides thread-safe variable storage with a four-level scope hierarchy:
- scope: Sequence-level variables (read/write)
- steps: Step-level variables (steps.<step_id>.<key>)
- loop: Loop iteration variables (loop.<loop_id>.<iteration>.<key>)
- global: Global variables (read-only)

Thread Safety:
    All operations are protected by threading.Lock for concurrent access.

Variable Naming:
    Variables are accessed using dot-notation prefixes:
    - 'scope.xxx' for sequence-level variables
    - 'steps.<step_id>.xxx' for step-level variables
    - 'loop.<loop_id>.<iteration>.xxx' for loop iteration variables
    - 'global.xxx' for global variables

Expression Resolution:
    Variables can be resolved in expressions using ${scope.xxx} syntax.
"""

from __future__ import annotations

import re
import threading
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .event_bus import EventBus


class VariableSpace:
    """Thread-safe variable storage with scope hierarchy.

    Manages variables across four scopes:
    - Sequence scope: Variables that persist across the entire sequence
    - Steps scope: Variables specific to individual steps
    - Loop scope: Variables specific to loop iterations
    - Global scope: Read-only system-wide variables

    Thread Safety:
        Uses threading.Lock to protect all read/write operations.

    Example:
        >>> vs = VariableSpace()
        >>> vs.set('scope.voltage', 3.3)
        >>> vs.get('scope.voltage')
        3.3
        >>> vs.resolve('${scope.voltage}')
        '3.3'
    """

    # Whitelist of writable scope prefixes
    WRITABLE_SCOPES = frozenset(["scope", "steps", "loop"])

    def __init__(self, event_bus: EventBus | None = None) -> None:
        """Initialize an empty variable space.

        Args:
            event_bus: Optional EventBus for publishing MEASUREMENT_RECORDED events.
                When provided, set() will fire MEASUREMENT_RECORDED events.
        """
        self._scope: dict[str, Any] = {}
        self._steps: dict[str, dict[str, Any]] = {}
        self._loop: dict[str, dict[str, Any]] = {}
        self._global: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._event_bus: EventBus | None = event_bus

    def get(self, name: str, default: Any = None) -> Any:
        """Retrieve a variable value with scope resolution.

        Args:
            name: Variable name with scope prefix (e.g., 'scope.voltage')
            default: Default value if variable not found

        Returns:
            The variable value, or default if not found

        Example:
            >>> vs = VariableSpace()
            >>> vs.set('scope.voltage', 3.3)
            >>> vs.get('scope.voltage')
            3.3
            >>> vs.get('scope.unknown', 'default_value')
            'default_value'
        """
        with self._lock:
            parts = name.split(".", 1)
            if len(parts) != 2:
                return default

            scope_prefix, var_path = parts

            if scope_prefix == "scope":
                return self._scope.get(var_path, default)
            elif scope_prefix == "steps":
                # Parse steps.<step_id>.<key>
                step_parts = var_path.split(".", 1)
                if len(step_parts) != 2:
                    return default
                step_id, key = step_parts
                step_vars = self._steps.get(step_id)
                if step_vars is None:
                    return default
                return step_vars.get(key, default)
            elif scope_prefix == "loop":
                # Parse loop.<loop_id>.<iteration>.<key> or loop.<loop_id>.<key>
                loop_parts = var_path.split(".", 2)
                if len(loop_parts) < 2:
                    return default
                loop_id = loop_parts[0]
                loop_vars = self._loop.get(loop_id)
                if loop_vars is None:
                    return default
                # Remaining path after loop_id (e.g. "0.i" or "result")
                remaining = ".".join(loop_parts[1:])
                return loop_vars.get(remaining, default)
            elif scope_prefix == "global":
                return self._global.get(var_path, default)
            else:
                return default

    def set(self, name: str, value: Any) -> None:
        """Set a variable value with whitelist validation.

        Fires MEASUREMENT_RECORDED event if event_bus is configured.

        Args:
            name: Variable name with scope prefix (e.g., 'scope.voltage')
            value: Variable value to set

        Raises:
            ValueError: If attempting to write to read-only global scope

        Example:
            >>> vs = VariableSpace()
            >>> vs.set('scope.voltage', 3.3)
            >>> vs.get('scope.voltage')
            3.3
        """
        with self._lock:
            parts = name.split(".", 1)
            if len(parts) != 2:
                raise ValueError(
                    f"Invalid variable name '{name}'. Must be '<scope>.<name>'"
                )

            scope_prefix, var_path = parts

            if scope_prefix not in self.WRITABLE_SCOPES:
                raise ValueError(
                    f"Cannot write to '{scope_prefix}' scope. "
                    f"Writable scopes: {list(self.WRITABLE_SCOPES)}"
                )

            # Capture old value for event
            old_value: Any = None
            if scope_prefix == "scope":
                old_value = self._scope.get(var_path)
                self._scope[var_path] = value
            elif scope_prefix == "steps":
                # Parse steps.<step_id>.<key>
                step_parts = var_path.split(".", 1)
                if len(step_parts) != 2:
                    raise ValueError(
                        f"Invalid steps variable name '{name}'. "
                        f"Must be 'steps.<step_id>.<key>'"
                    )
                step_id, key = step_parts
                if step_id not in self._steps:
                    self._steps[step_id] = {}
                else:
                    old_value = self._steps[step_id].get(key)
                self._steps[step_id][key] = value
            elif scope_prefix == "loop":
                # Parse loop.<loop_id>.<remaining_path>
                loop_parts = var_path.split(".", 1)
                if len(loop_parts) != 2:
                    raise ValueError(
                        f"Invalid loop variable name '{name}'. "
                        f"Must be 'loop.<loop_id>.<path>'"
                    )
                loop_id, remaining = loop_parts
                if loop_id not in self._loop:
                    self._loop[loop_id] = {}
                else:
                    old_value = self._loop[loop_id].get(remaining)
                self._loop[loop_id][remaining] = value

        # Fire MEASUREMENT_RECORDED event outside the lock
        if self._event_bus is not None:
            import time as _time

            from shared.events import EventType, MeasurementRecordedData

            event_data = asdict(MeasurementRecordedData(
                name=name,
                old_value=old_value,
                new_value=value,
                timestamp=_time.time(),
            ))
            self._event_bus.publish_sync(EventType.MEASUREMENT_RECORDED, event_data)

    def resolve(self, expression: str) -> str:
        """Resolve variable references in an expression.

        Supports ${scope.xxx} and ${steps.xxx.yyy} syntax.

        Args:
            expression: String containing variable references

        Returns:
            Expression with all variable references replaced by their values

        Example:
            >>> vs = VariableSpace()
            >>> vs.set('scope.voltage', 3.3)
            >>> vs.resolve('Voltage is ${scope.voltage}V')
            'Voltage is 3.3V'
        """
        # Pattern to match ${scope.xxx} or ${steps.xxx.yyy} etc.
        # Allows numeric segments (e.g., loop.test.0.value) after the first identifier
        pattern = r"\$\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)*)\}"

        def replace_var(match: re.Match[str]) -> str:
            var_name = match.group(1)
            value = self.get(var_name)
            if value is None:
                return match.group(0)  # Return original if not found
            return str(value)

        return re.sub(pattern, replace_var, expression)

    def set_global(self, key: str, value: Any) -> None:
        """Set a global variable (internal use only).

        Global variables are read-only from user perspective.
        This method should only be called during initialization.

        Args:
            key: Variable name (without 'global.' prefix)
            value: Variable value
        """
        with self._lock:
            self._global[key] = value

    def clear_scope(self) -> None:
        """Clear all sequence-level variables.

        Used when resetting or starting a new sequence.
        """
        with self._lock:
            self._scope.clear()

    def clear_steps(self) -> None:
        """Clear all step-level variables.

        Used when resetting or starting a new sequence.
        """
        with self._lock:
            self._steps.clear()

    def get_all_scope_vars(self) -> dict[str, Any]:
        """Get a copy of all sequence-level variables.

        Returns:
            Dictionary copy of scope variables
        """
        with self._lock:
            return self._scope.copy()

    def get_step_vars(self, step_id: str) -> dict[str, Any]:
        """Get a copy of variables for a specific step.

        Args:
            step_id: The step identifier

        Returns:
            Dictionary copy of step variables, empty dict if step not found
        """
        with self._lock:
            return self._steps.get(step_id, {}).copy()

    def set_loop_variable(self, loop_id: str, iteration: int, key: str, value: Any) -> None:
        """Set a loop iteration variable.

        Convenience method that constructs the full variable name
        'loop.<loop_id>.<iteration>.<key>' and delegates to set().

        Args:
            loop_id: The loop identifier
            iteration: Zero-based iteration index
            key: Variable name within this iteration
            value: Variable value
        """
        self.set(f"loop.{loop_id}.{iteration}.{key}", value)

    def get_loop_variable(self, loop_id: str, iteration: int, key: str, default: Any = None) -> Any:
        """Get a loop iteration variable.

        Convenience method that constructs the full variable name
        'loop.<loop_id>.<iteration>.<key>' and delegates to get().

        Args:
            loop_id: The loop identifier
            iteration: Zero-based iteration index
            key: Variable name within this iteration
            default: Default value if variable not found

        Returns:
            The variable value, or default if not found
        """
        return self.get(f"loop.{loop_id}.{iteration}.{key}", default)

    def get_loop_result(self, loop_id: str) -> Any:
        """Retrieve the LoopResult stored for a completed loop.

        Args:
            loop_id: The loop identifier

        Returns:
            The LoopResult object, or None if not found
        """
        return self.get(f"loop.{loop_id}.result")

    def clear_loop(self) -> None:
        """Clear all loop-level variables.

        Used when resetting or starting a new sequence.
        """
        with self._lock:
            self._loop.clear()

    # ------------------------------------------------------------------
    # 快照 / 恢复（§6.6 崩溃恢复）
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """导出可持久化变量状态（崩溃恢复用）。

        global 为系统只读变量，不持久化。返回结构：
        ``{"scope": ..., "steps": ..., "loop": ...}``

        Returns:
            可 JSON 序列化的变量字典。
        """
        with self._lock:
            return {
                "scope": dict(self._scope),
                "steps": {sid: dict(vars_) for sid, vars_ in self._steps.items()},
                "loop": {lid: dict(vars_) for lid, vars_ in self._loop.items()},
            }

    def restore(self, state: dict[str, Any]) -> None:
        """从快照恢复变量状态（崩溃后断点续跑）。

        覆盖恢复：先清空现有 scope/steps/loop 再写入快照内容，保证与
        崩溃前完全一致（与快照导出结构对仗）。

        Args:
            state: :meth:`snapshot` 导出的字典；非法结构时忽略该部分。
        """
        with self._lock:
            scope = state.get("scope") if isinstance(state, dict) else None
            steps = state.get("steps") if isinstance(state, dict) else None
            loop = state.get("loop") if isinstance(state, dict) else None

            if isinstance(scope, dict):
                self._scope = dict(scope)
            if isinstance(steps, dict):
                self._steps = {
                    str(sid): dict(vars_) if isinstance(vars_, dict) else {}
                    for sid, vars_ in steps.items()
                }
            if isinstance(loop, dict):
                self._loop = {
                    str(lid): dict(vars_) if isinstance(vars_, dict) else {}
                    for lid, vars_ in loop.items()
                }
