"""Resource Manager for ATE Platform.

This module provides thread-safe resource locking with timeout support
and deadlock detection for test step execution.

Key features:
- Thread-safe acquire/release operations
- Timeout-based waiting
- Ownership tracking
- Deadlock detection via timeout
- Event publishing on resource release

Typical usage:
    rm = ResourceManager()
    if rm.acquire('DMM_CH1', 'step1', timeout=5.0):
        # use resource
        rm.release('DMM_CH1', 'step1')
"""

from __future__ import annotations

import threading
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, override

from ..exceptions import ResourceAcquireError

if TYPE_CHECKING:
    from .event_bus import EventBus


class ResourceManager:
    """Thread-safe resource lock manager with timeout and ownership tracking.

    Manages exclusive access to named resources (e.g., DMM channels, GPIO pins)
    with support for timeout-based acquisition and owner identification.

    Attributes:
        _locks: Mapping from resource_id to Lock object
        _owners: Mapping from resource_id to owner_id string
        _lock: Global lock protecting internal state

    Example:
        >>> rm = ResourceManager()
        >>> rm.acquire('DMM_CH1', 'step1', timeout=1.0)
        True
        >>> rm.is_available('DMM_CH1')
        False
        >>> rm.get_owner('DMM_CH1')
        'step1'
        >>> rm.release('DMM_CH1', 'step1')
        >>> rm.is_available('DMM_CH1')
        True
    """

    def __init__(self, event_bus: EventBus | None = None) -> None:
        """Initialize the resource manager with empty lock and owner mappings.

        Args:
            event_bus: Optional EventBus for publishing RESOURCE_RELEASED events.
                When provided, release() will fire RESOURCE_RELEASED events.
        """
        self._locks: dict[str, threading.Lock] = {}
        self._owners: dict[str, str] = {}
        self._lock: threading.Lock = threading.Lock()  # Protects _locks and _owners
        self._event_bus: EventBus | None = event_bus

    def acquire(
        self, resource_id: str, owner_id: str, timeout: float | None = None
    ) -> bool:
        """Attempt to acquire exclusive access to a resource.

        Thread-safe acquisition with optional timeout. If timeout is specified
        and the resource is held by another owner, waits up to timeout seconds
        before returning False.

        Args:
            resource_id: Unique identifier for the resource (e.g., 'DMM_CH1')
            owner_id: Identifier for the acquirer (e.g., 'step1')
            timeout: Maximum seconds to wait. None means no waiting (non-blocking).
                     0 or negative means immediate return if not available.

        Returns:
            True if resource was acquired, False otherwise

        Raises:
            ResourceAcquireError: If the same owner already holds this resource

        Example:
            >>> rm = ResourceManager()
            >>> rm.acquire('DMM_CH1', 'step1', timeout=1.0)
            True
            >>> rm.acquire('DMM_CH1', 'step2', timeout=0.1)
            False
        """
        with self._lock:
            # Create lock for resource if not exists
            if resource_id not in self._locks:
                self._locks[resource_id] = threading.Lock()

            resource_lock = self._locks[resource_id]

            # Check if same owner already holds this resource
            current_owner = self._owners.get(resource_id)
            if current_owner == owner_id:
                raise ResourceAcquireError(
                    f"Owner '{owner_id}' already holds resource '{resource_id}'"
                )

        # Attempt to acquire the resource lock (outside global lock)
        acquired = False
        try:
            if timeout is not None:
                if timeout <= 0:
                    # Non-blocking attempt
                    acquired = resource_lock.acquire(blocking=False)
                else:
                    # Wait with timeout
                    acquired = resource_lock.acquire(blocking=True, timeout=timeout)
            else:
                # Non-blocking (default)
                acquired = resource_lock.acquire(blocking=False)
        except threading.ThreadError:
            # Handle rare threading errors
            return False

        if acquired:
            with self._lock:
                self._owners[resource_id] = owner_id

        return acquired

    def release(self, resource_id: str, owner_id: str) -> None:
        """Release a held resource.

        Thread-safe release that verifies ownership before releasing.
        Only the actual owner can release the resource.
        Fires RESOURCE_RELEASED event if event_bus is configured.

        Args:
            resource_id: Unique identifier for the resource
            owner_id: Identifier claiming to own the resource

        Raises:
            ResourceAcquireError: If resource not found or caller is not the owner

        Example:
            >>> rm = ResourceManager()
            >>> rm.acquire('DMM_CH1', 'step1')
            True
            >>> rm.release('DMM_CH1', 'step1')
            >>> rm.is_available('DMM_CH1')
            True
        """
        with self._lock:
            if resource_id not in self._locks:
                raise ResourceAcquireError(
                    f"Resource '{resource_id}' not found"
                )

            current_owner = self._owners.get(resource_id)
            if current_owner != owner_id:
                raise ResourceAcquireError(
                    f"Cannot release '{resource_id}': owner is '{current_owner}', not '{owner_id}'"
                )

            # Remove owner before releasing lock
            del self._owners[resource_id]

        # Release the lock (outside global lock to avoid deadlock)
        self._locks[resource_id].release()

        # Fire RESOURCE_RELEASED event outside the lock
        if self._event_bus is not None:
            from shared.events import EventType, ResourceReleasedData

            event_data = asdict(ResourceReleasedData(
                resource_id=resource_id,
                owner_id=owner_id,
            ))
            self._event_bus.publish_sync(EventType.RESOURCE_RELEASED, event_data)

    def is_available(self, resource_id: str) -> bool:
        """Check if a resource is available (not held by any owner).

        Thread-safe check for resource availability.

        Args:
            resource_id: Unique identifier for the resource

        Returns:
            True if resource exists and is not held, False otherwise

        Example:
            >>> rm = ResourceManager()
            >>> rm.is_available('DMM_CH1')
            True
            >>> rm.acquire('DMM_CH1', 'step1')
            True
            >>> rm.is_available('DMM_CH1')
            False
        """
        with self._lock:
            if resource_id not in self._locks:
                # Resource not yet created, so it's available
                return True

            # Resource is available if no owner
            return resource_id not in self._owners

    def get_owner(self, resource_id: str) -> str | None:
        """Get the current owner of a resource.

        Thread-safe owner lookup.

        Args:
            resource_id: Unique identifier for the resource

        Returns:
            Owner identifier string if resource is held, None otherwise

        Example:
            >>> rm = ResourceManager()
            >>> rm.get_owner('DMM_CH1')
            None
            >>> rm.acquire('DMM_CH1', 'step1')
            True
            >>> rm.get_owner('DMM_CH1')
            'step1'
        """
        with self._lock:
            return self._owners.get(resource_id)

    @override
    def __repr__(self) -> str:
        """Return string representation showing held resources."""
        with self._lock:
            held = dict(self._owners.items())
            return f"ResourceManager(held_resources={held})"
