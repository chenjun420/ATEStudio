"""Circuit breaker pattern implementation for async call protection.

Provides a three-state finite state machine (CLOSED → OPEN → HALF_OPEN)
that prevents cascading failures by stopping calls to a failing service
after a configurable failure threshold.

Usage:
    # Context manager interface
    breaker = CircuitBreaker()
    async with breaker:
        await do_work()

    # Callable interface
    result = await breaker.call(some_async_fn, arg1, arg2)
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(enum.Enum):
    """Represents the current state of the circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(RuntimeError):
    """Raised when a call is attempted while the circuit is OPEN."""

    def __init__(self, message: str = "Circuit breaker is OPEN — call rejected") -> None:
        super().__init__(message)


class CircuitBreaker:
    """An async circuit breaker implementing the standard three-state FSM.

    States:
        CLOSED: Normal operation. Failures are counted. After
            ``failure_threshold`` consecutive failures, transitions to OPEN.
        OPEN: Calls are rejected immediately with CircuitBreakerOpenError.
            After ``timeout`` seconds, transitions to HALF_OPEN.
        HALF_OPEN: Allows exactly one probe call. If it succeeds,
            transitions to CLOSED. If it fails, transitions back to OPEN.

    Parameters:
        failure_threshold: Number of consecutive failures before opening the circuit.
        timeout: Seconds to wait before transitioning from OPEN to HALF_OPEN.
        name: Optional name for this breaker (used in log messages).
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        timeout: float = 30.0,
        *,
        name: str = "",
    ) -> None:
        if failure_threshold < 1:
            msg = f"failure_threshold must be >= 1, got {failure_threshold}"
            raise ValueError(msg)
        if timeout <= 0:
            msg = f"timeout must be > 0, got {timeout}"
            raise ValueError(msg)

        self._failure_threshold = failure_threshold
        self._timeout = timeout
        self._name = name or f"cb-{id(self):x}"

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._lock = asyncio.Lock()

    # ── Public properties ──────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        """Current state of the circuit breaker."""
        return self._state

    @property
    def failure_count(self) -> int:
        """Number of consecutive failures counted so far."""
        return self._failure_count

    # ── State management ───────────────────────────────────────────

    async def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state and log the event."""
        old_state = self._state
        self._state = new_state
        logger.info(
            "CircuitBreaker %s: %s → %s (failures=%d)",
            self._name,
            old_state.value,
            new_state.value,
            self._failure_count,
        )

    async def _on_success(self) -> None:
        """Handle a successful call.

        In CLOSED: reset failure count.
        In HALF_OPEN: reset failure count and close the circuit.
        """
        self._failure_count = 0
        if self._state == CircuitState.HALF_OPEN:
            await self._transition_to(CircuitState.CLOSED)

    async def _on_failure(self) -> None:
        """Handle a failed call.

        In CLOSED: increment failure count; if threshold reached, open circuit.
        In HALF_OPEN: transition back to OPEN immediately (one failure re-opens).
        """
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state == CircuitState.CLOSED:
            logger.debug(
                "CircuitBreaker %s: failure %d/%d",
                self._name,
                self._failure_count,
                self._failure_threshold,
            )
            if self._failure_count >= self._failure_threshold:
                await self._transition_to(CircuitState.OPEN)
        elif self._state == CircuitState.HALF_OPEN:
            await self._transition_to(CircuitState.OPEN)

    async def _before_call(self) -> None:
        """Check circuit state before making a call.

        In OPEN state: check if timeout has elapsed to transition to HALF_OPEN.
        If still within timeout, raise CircuitBreakerOpenError.
        """
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._timeout:
                await self._transition_to(CircuitState.HALF_OPEN)
            else:
                remaining = self._timeout - elapsed
                logger.debug(
                    "CircuitBreaker %s: OPEN, %.1fs remaining",
                    self._name,
                    remaining,
                )
                msg = (
                    f"Circuit breaker '{self._name}' is OPEN. "
                    f"Try again in {remaining:.1f}s."
                )
                raise CircuitBreakerOpenError(msg)

    # ── Async context manager ──────────────────────────────────────

    async def __aenter__(self) -> CircuitBreaker:
        """Enter the circuit breaker context — checks state before allowing."""
        async with self._lock:
            await self._before_call()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        """Exit the circuit breaker context — record success or failure."""
        async with self._lock:
            if exc_type is None:
                await self._on_success()
            else:
                await self._on_failure()
        # Don't suppress exceptions — let them propagate
        return False

    # ── Callable interface ─────────────────────────────────────────

    async def call(
        self,
        fn: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute ``fn(*args, **kwargs)`` within the circuit breaker.

        This is a convenience wrapper around the context manager protocol
        for use with functions that don't need the ``async with`` syntax.

        Returns:
            The return value of ``fn``.

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN.
            Any exception raised by ``fn`` itself.
        """
        async with self:
            result = await fn(*args, **kwargs)
        return result

    # ── Manual reset ───────────────────────────────────────────────

    async def reset(self) -> None:
        """Manually reset the circuit breaker to CLOSED state.

        Clears the failure count and transitions back to CLOSED
        regardless of current state.
        """
        async with self._lock:
            self._failure_count = 0
            if self._state != CircuitState.CLOSED:
                await self._transition_to(CircuitState.CLOSED)
