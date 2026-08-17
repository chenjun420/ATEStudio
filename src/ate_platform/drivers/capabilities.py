"""Pydantic instrument capability models.

Each instrument type has a capabilities model describing its hardware limits
and features. These models are attached to MAL abstractions via the
`capabilities` ClassVar and instantiated by `get_capabilities()`.

Design:
- Parse-don't-validate: capabilities are validated at construction time.
- Immutable by default: all fields are frozen after creation.
- Sensible defaults: models can be constructed with no arguments for mock use.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DMMCapabilities(BaseModel):
    """Capabilities model for Digital Multimeter instruments.

    Attributes:
        channels: Number of measurement channels.
        max_voltage: Maximum measurable voltage in volts.
        max_current: Maximum measurable current in amperes.
        can_measure_resistance: Whether the DMM can measure resistance.
        can_measure_current: Whether the DMM can measure current directly.
        resolution_digits: Measurement resolution in digits (e.g., 6.5).
    """

    model_config = {"frozen": True}

    channels: int = Field(default=1, ge=1)
    max_voltage: float = Field(default=1000.0, gt=0)
    max_current: float = Field(default=3.0, gt=0)
    can_measure_resistance: bool = True
    can_measure_current: bool = True
    resolution_digits: float = Field(default=6.5, gt=0)


class PSUCapabilities(BaseModel):
    """Capabilities model for Programmable Power Supply instruments.

    Attributes:
        channels: Number of output channels.
        max_voltage: Maximum output voltage in volts.
        max_current: Maximum output current in amperes.
        has_remote_sense: Whether the PSU supports remote sense connections.
    """

    model_config = {"frozen": True}

    channels: int = Field(default=1, ge=1)
    max_voltage: float = Field(default=30.0, gt=0)
    max_current: float = Field(default=3.0, gt=0)
    has_remote_sense: bool = False


class ELoadCapabilities(BaseModel):
    """Capabilities model for Electronic Load instruments.

    Attributes:
        channels: Number of load channels.
        max_power: Maximum dissipation power in watts.
        max_current: Maximum load current in amperes.
        max_voltage: Maximum input voltage in volts.
        modes: Supported operating modes (CC/CV/CR/CP).
    """

    model_config = {"frozen": True}

    channels: int = Field(default=1, ge=1)
    max_power: float = Field(default=350.0, gt=0)
    max_current: float = Field(default=60.0, gt=0)
    max_voltage: float = Field(default=80.0, gt=0)
    modes: tuple[str, ...] = ("CC", "CV", "CR", "CP")
