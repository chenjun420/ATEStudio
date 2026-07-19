"""Event types for ATE Platform.

This module defines event-related types:
- EventType: Enum of supported event types
- Event: Data container for event messages
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EventType(Enum):
    """Enumeration of supported event types in the ATE Platform.

    Attributes:
        STEP_STATUS_CHANGED: A step's execution status has changed
        VARIABLE_CHANGED: A test variable has been modified
        RESOURCE_RELEASED: A resource has been released
        TIMER_EXPIRED: A timer has expired
        EXTERNAL_CMD: An external command has been received
    """

    STEP_STATUS_CHANGED = "STEP_STATUS_CHANGED"
    VARIABLE_CHANGED = "VARIABLE_CHANGED"
    RESOURCE_RELEASED = "RESOURCE_RELEASED"
    TIMER_EXPIRED = "TIMER_EXPIRED"
    EXTERNAL_CMD = "EXTERNAL_CMD"


@dataclass
class Event:
    """Container for event data.

    Attributes:
        type: The type of event
        data: The event payload
        timestamp: When the event was created (auto-generated if not provided)
    """

    type: EventType
    data: dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)