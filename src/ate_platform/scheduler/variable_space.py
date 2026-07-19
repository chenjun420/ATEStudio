"""Variable space management for ATE Platform.

This module provides thread-safe variable storage with a three-level scope hierarchy:
- scope: Sequence-level variables (read/write)
- steps: Step-level variables (steps.<step_id>.<key>)
- global: Global variables (read-only)

Thread Safety:
    All operations are protected by threading.Lock for concurrent access.

Variable Naming:
    Variables are accessed using dot-notation prefixes:
    - 'scope.xxx' for sequence-level variables
    - 'steps.<step_id>.xxx' for step-level variables
    - 'global.xxx' for global variables

Expression Resolution:
    Variables can be resolved in expressions using ${scope.xxx} syntax.
"""

import re
import threading
from typing import Any


class VariableSpace:
    """Thread-safe variable storage with scope hierarchy.

    Manages variables across three scopes:
    - Sequence scope: Variables that persist across the entire sequence
    - Steps scope: Variables specific to individual steps
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
    WRITABLE_SCOPES = frozenset(["scope", "steps"])

    def __init__(self) -> None:
        """Initialize an empty variable space."""
        self._scope: dict[str, Any] = {}
        self._steps: dict[str, dict[str, Any]] = {}
        self._global: dict[str, Any] = {}
        self._lock = threading.Lock()

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
            elif scope_prefix == "global":
                return self._global.get(var_path, default)
            else:
                return default

    def set(self, name: str, value: Any) -> None:
        """Set a variable value with whitelist validation.

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

            if scope_prefix == "scope":
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
                self._steps[step_id][key] = value

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
        pattern = r"\$\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+)\}"

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
