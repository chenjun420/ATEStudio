"""Context proxy for script execution in ATE Platform.

This module provides a proxy interface for test scripts to access:
- Variable space (read/write with whitelist)
- Resources (via ResourceManager)
- Logging capabilities

The proxy ensures safe, controlled access to execution context.

Example:
    >>> from ate_platform.executor import ContextProxy
    >>> proxy = ContextProxy(variable_space, resource_manager, 'step1')
    >>> voltage = proxy['scope.voltage']  # Read variable
    >>> proxy['result'] = 3.3  # Write to step outputs (allowed)
    >>> dmm = proxy.resource('DMM_CH1')  # Access resource
    >>> proxy.log('info', 'Measurement complete')  # Log message
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, TypeVar

from ..scheduler.resource_manager import ResourceManager
from ..scheduler.variable_space import VariableSpace

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class ResourceProxy:
    """Proxy object for accessing a resource.

    Returned by ContextProxy.resource() to provide controlled access
    to resources managed by ResourceManager.

    Attributes:
        resource_id: The resource identifier
        owner_id: The step ID that owns this resource access
        resource_manager: Reference to the ResourceManager
    """

    resource_id: str
    owner_id: str
    resource_manager: ResourceManager
    _acquired: bool = field(default=False, init=False)

    def acquire(self, timeout: float | None = None) -> bool:
        """Acquire the resource.

        Args:
            timeout: Maximum seconds to wait. None means non-blocking.

        Returns:
            True if acquired successfully, False otherwise
        """
        acquired = self.resource_manager.acquire(
            self.resource_id, self.owner_id, timeout=timeout
        )
        if acquired:
            self._acquired = True
        return acquired

    def release(self) -> None:
        """Release the resource."""
        if self._acquired:
            self.resource_manager.release(self.resource_id, self.owner_id)
            self._acquired = False

    def is_available(self) -> bool:
        """Check if the resource is available."""
        return self.resource_manager.is_available(self.resource_id)

    def __enter__(self) -> "ResourceProxy":
        """Context manager entry - acquire resource."""
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit - release resource."""
        self.release()


@dataclass
class ContextProxy:
    """Proxy for script context access.

    Provides controlled access to:
    - Variable space (read via __getitem__, write via __setitem__ with whitelist)
    - Resources (via resource() method)
    - Logging (via log() method)

    Attributes:
        _variable_space: Reference to the VariableSpace
        _resource_manager: Reference to the ResourceManager
        _step_id: The step identifier for this context
        _outputs: Dictionary of step outputs
        _declared_outputs: Set of declared output variable names (via @measure)
    """

    _variable_space: VariableSpace
    _resource_manager: ResourceManager
    _step_id: str
    _outputs: dict[str, Any] = field(default_factory=dict)
    _declared_outputs: set[str] = field(default_factory=set)
    # 仪器代理进程管理器（可选；设置后脚本可通过 instrument() 访问代理仪器）
    _proxy_manager: Any | None = None

    # Whitelist of writable variable prefixes for step outputs
    WRITABLE_PREFIXES = frozenset(["steps"])

    def __getitem__(self, name: str) -> Any:
        """Read a variable from the variable space.

        Supports reading from all scopes: scope, steps, global.

        Args:
            name: Variable name with scope prefix (e.g., 'scope.voltage')

        Returns:
            The variable value

        Example:
            >>> proxy['scope.voltage']
            3.3
        """
        # Allow reading from any scope
        return self._variable_space.get(name)

    def __setitem__(self, name: str, value: Any) -> None:
        """Write a variable with whitelist validation.

        Only allows writing to:
        - Step-level outputs (prefix: 'steps.<step_id>.<key>')
        - Declared output variables (set via @measure decorator)

        Args:
            name: Variable name (without scope prefix for step outputs)
            value: Variable value to set

        Raises:
            ValueError: If attempting to write to non-whitelisted scope

        Example:
            >>> proxy['result'] = 3.3  # Sets steps.<step_id>.result
        """
        # If name doesn't have a scope prefix, treat it as a step output
        if "." not in name:
            # Write to step outputs directly
            if name in self._declared_outputs or len(self._declared_outputs) == 0:
                # If declared or no declarations (allow any), write to step outputs
                full_name = f"steps.{self._step_id}.{name}"
                self._variable_space.set(full_name, value)
                self._outputs[name] = value
            else:
                raise ValueError(
                    f"Cannot write to undeclared output '{name}'. "
                    f"Declared outputs: {sorted(self._declared_outputs)}"
                )
        else:
            # Has scope prefix - validate whitelist
            scope_prefix = name.split(".", 1)[0]

            if scope_prefix not in self.WRITABLE_PREFIXES:
                raise ValueError(
                    f"Cannot write to '{scope_prefix}' scope. "
                    f"Writable scopes: {list(self.WRITABLE_PREFIXES)}"
                )

            # Validate it's writing to this step's outputs
            if scope_prefix == "steps":
                expected_prefix = f"steps.{self._step_id}."
                if not name.startswith(expected_prefix):
                    raise ValueError(
                        f"Cannot write to another step's variables. "
                        f"Expected prefix: '{expected_prefix}'"
                    )

            self._variable_space.set(name, value)
            # Track in outputs
            output_key = name.split(".")[-1]
            self._outputs[output_key] = value

    def resource(self, resource_id: str) -> ResourceProxy:
        """Get a resource proxy for accessing a resource.

        Args:
            resource_id: The resource identifier

        Returns:
            ResourceProxy object for controlled resource access

        Example:
            >>> dmm = proxy.resource('DMM_CH1')
            >>> if dmm.acquire(timeout=5.0):
            ...     # Use resource
            ...     dmm.release()
        """
        return ResourceProxy(
            resource_id=resource_id,
            owner_id=self._step_id,
            resource_manager=self._resource_manager,
        )

    def instrument(self, resource_id: str, timeout: float = 30.0) -> Any:
        """Get an InstrumentClient for proxy-managed instrument access.

        All instrument operations are forwarded through the instrument proxy
        process (single entry point), which serializes access per instrument
        and records every call — solving cross-process lock invalidation (A2).

        Args:
            resource_id: The instrument resource identifier.
            timeout: Per-call timeout in seconds.

        Returns:
            InstrumentClient proxy for the instrument.

        Raises:
            RuntimeError: If no proxy manager is configured.

        Example:
            >>> dmm = context.instrument('DMM_CH1')
            >>> voltage = dmm.query('MEAS:VOLT?')
        """
        if self._proxy_manager is None:
            msg = (
                "No instrument proxy manager configured. "
                "Set context._proxy_manager to enable proxy instrument access."
            )
            raise RuntimeError(msg)
        return self._proxy_manager.client(resource_id, timeout=timeout)

    def log(self, level: str, message: str) -> None:
        """Log a message with structured context.

        Args:
            level: Log level ('debug', 'info', 'warning', 'error', 'critical')
            message: Log message

        Example:
            >>> proxy.log('info', 'Measurement complete')
        """
        level_lower = level.lower()
        log_func = getattr(logger, level_lower, logger.info)

        # Add step context to log message
        structured_message = f"[Step:{self._step_id}] {message}"
        _ = log_func(structured_message)  # type: ignore[func-returns-value]

    def declare_output(self, name: str) -> None:
        """Declare an output variable.

        Called by @measure decorator to register expected outputs.

        Args:
            name: Output variable name
        """
        self._declared_outputs.add(name)

    def get_outputs(self) -> dict[str, Any]:
        """Get all step outputs.

        Returns:
            Dictionary of output variable names to values
        """
        return self._outputs.copy()


def measure(*output_names: str) -> Callable[[F], F]:
    """Decorator to declare output variables for a function.

    Use this decorator on test functions to declare their outputs.
    The ContextProxy will validate that only declared outputs are set.

    Args:
        *output_names: Names of output variables

    Returns:
        Decorator function

    Example:
        >>> @measure('voltage', 'current')
        ... def test_measure(proxy: ContextProxy) -> None:
        ...     proxy['voltage'] = 3.3
        ...     proxy['current'] = 0.5
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(proxy: ContextProxy, *args: Any, **kwargs: Any) -> Any:
            # Declare outputs on the proxy
            for name in output_names:
                proxy.declare_output(name)

            # Execute the function
            return func(proxy, *args, **kwargs)

        return wrapper  # pyright: ignore[reportReturnType]

    return decorator
