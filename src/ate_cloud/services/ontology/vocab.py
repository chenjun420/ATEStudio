"""Unified controlled vocabulary for instruments and faults.

Before this module the codebase carried FOUR duplicate instrument/fault
vocabularies:

1. ``shared.fixture_topology.InstrumentType`` / ``FaultType`` enums
   (``psu``/``dmm``/..., 7 electrical fault types) used by fixture topology;
2. ``FixtureDeviceTemplate.category`` / ``type`` free strings persisted by the
   fixture template library;
3. KG seed Instrument nodes / fault categories in ``kg_seeder``
   (``"Digital Multimeter"``, 6 FMEA categories, 100+ fault records);
4. ``InstrumentConfig.instrument_type`` free strings in station config
   (e.g. ``"digital_multimeter"``) and the fault-injector action words.

This module defines ONE canonical ID space — :class:`InstrumentKind` and
:class:`FaultKind` (values are the canonical, stable IDs shared by the
ontology, the KG seed (task 8) and extraction) — plus alias resolution that
maps every legacy spelling onto a canonical concept.

The 3-level fixture ``Severity`` (warning/error/critical) is deliberately NOT
here: FMEA ratings use a separate 1-10 scale modeled in the ontology
(task 10). This module is deterministic (no LLM, no I/O).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal


class InstrumentKind(str, Enum):
    """Canonical instrument IDs — the single namespace for instrument types."""

    POWER_SUPPLY = "power_supply"
    DIGITAL_MULTIMETER = "digital_multimeter"
    ELECTRONIC_LOAD = "electronic_load"
    OSCILLOSCOPE = "oscilloscope"
    LOGIC_ANALYZER = "logic_analyzer"
    SPECTRUM_ANALYZER = "spectrum_analyzer"
    SIGNAL_GENERATOR = "signal_generator"
    PROTOCOL_ANALYZER = "protocol_analyzer"
    CAN_ANALYZER = "can_analyzer"
    USB_ANALYZER = "usb_analyzer"
    CABLE_TESTER = "cable_tester"
    THERMAL_CAMERA = "thermal_camera"
    GPIB_GATEWAY = "gpib_gateway"
    GENERIC = "generic"


class FaultCategory(str, Enum):
    """FMEA fault categories (kg_seeder's 6 buckets, unified)."""

    COMMUNICATION_INTERCONNECTS = "communication_interconnects"
    POWER = "power"
    ASSEMBLY_SOLDERING = "assembly_soldering"
    PASSIVE_COMPONENTS = "passive_components"
    ENVIRONMENTAL_ESD = "environmental_esd"
    MIXED_SIGNAL_TIMING = "mixed_signal_timing"


class FaultKind(str, Enum):
    """Canonical failure-mode IDs (fixture FaultType + injector/seed words)."""

    OPEN_CIRCUIT = "open_circuit"
    SHORT_CIRCUIT = "short_circuit"
    OVER_VOLTAGE = "over_voltage"
    OVER_CURRENT = "over_current"
    COMMUNICATION = "communication"
    OUT_OF_RANGE = "out_of_range"
    RELAY_FAULT = "relay_fault"
    TIMEOUT = "timeout"
    SIGNAL_LOSS = "signal_loss"
    INTERMITTENT = "intermittent"
    NOISE = "noise"
    DRIFT = "drift"
    OVERHEAT = "overheat"


VocabScheme = Literal["instrument", "fault"]


@dataclass(frozen=True, slots=True)
class VocabConcept:
    """One canonical controlled-vocabulary concept and its legacy aliases."""

    canonical: str
    scheme: VocabScheme
    label: str
    enum_member: Enum
    aliases: tuple[str, ...]
    sources: tuple[str, ...]
    category: FaultCategory | None = None


def _concepts() -> tuple[VocabConcept, ...]:
    return (
        # ── Instruments ──────────────────────────────────────────────────────
        VocabConcept("power_supply", "instrument", "Power supply", InstrumentKind.POWER_SUPPLY,
                     ("psu", "power_supply", "power supply", "power supply unit", "dc power supply", "psu_main"),
                     ("InstrumentType.psu", "InstrumentConfig.instrument_type", "FixtureDeviceTemplate.type")),
        VocabConcept("digital_multimeter", "instrument", "Digital multimeter", InstrumentKind.DIGITAL_MULTIMETER,
                     ("dmm", "digital_multimeter", "digital multimeter", "multimeter", "dmm_meter"),
                     ("InstrumentType.dmm", "kg_seeder Instrument node", "InstrumentConfig.instrument_type")),
        VocabConcept("electronic_load", "instrument", "Electronic load", InstrumentKind.ELECTRONIC_LOAD,
                     ("eload", "electronic_load", "electronic load", "e-load", "dc electronic load"),
                     ("InstrumentType.eload",)),
        VocabConcept("oscilloscope", "instrument", "Oscilloscope", InstrumentKind.OSCILLOSCOPE,
                     ("oscilloscope", "scope", "osc", "mso", "dpo"),
                     ("InstrumentType.oscilloscope", "kg_seeder Instrument node", "patterns DMM-/OSC-")),
        VocabConcept("logic_analyzer", "instrument", "Logic analyzer", InstrumentKind.LOGIC_ANALYZER,
                     ("logic_analyzer", "logic analyzer",),
                     ("kg_seeder Instrument node",)),
        VocabConcept("spectrum_analyzer", "instrument", "Spectrum analyzer", InstrumentKind.SPECTRUM_ANALYZER,
                     ("spectrum_analyzer", "spectrum analyzer", "sa"),
                     ("kg_seeder Instrument node",)),
        VocabConcept("signal_generator", "instrument", "Signal / waveform generator", InstrumentKind.SIGNAL_GENERATOR,
                     ("signal_generator", "signal generator", "awg", "arbitrary waveform generator",
                      "function generator", "waveform generator"),
                     ("InstrumentConfig.instrument_type",)),
        VocabConcept("protocol_analyzer", "instrument", "Protocol analyzer", InstrumentKind.PROTOCOL_ANALYZER,
                     ("protocol_analyzer", "protocol analyzer", "bus analyzer"),
                     ("kg_seeder Instrument node",)),
        VocabConcept("can_analyzer", "instrument", "CAN bus analyzer", InstrumentKind.CAN_ANALYZER,
                     ("can_analyzer", "can analyzer",),
                     ("kg_seeder Instrument node",)),
        VocabConcept("usb_analyzer", "instrument", "USB analyzer", InstrumentKind.USB_ANALYZER,
                     ("usb_analyzer", "usb analyzer",),
                     ("kg_seeder Instrument node",)),
        VocabConcept("cable_tester", "instrument", "Cable tester", InstrumentKind.CABLE_TESTER,
                     ("cable_tester", "cable tester", "cable test"),
                     ("kg_seeder Instrument node",)),
        VocabConcept("thermal_camera", "instrument", "Thermal camera", InstrumentKind.THERMAL_CAMERA,
                     ("thermal_camera", "thermal camera", "thermal imager", "ir camera"),
                     ("kg_seeder Instrument node",)),
        VocabConcept("gpib_gateway", "instrument", "GPIB gateway", InstrumentKind.GPIB_GATEWAY,
                     ("gpib_gateway", "gpib gateway", "gateway"),
                     ("InstrumentType.gpib_gateway",)),
        VocabConcept("generic", "instrument", "Generic / custom instrument", InstrumentKind.GENERIC,
                     ("generic", "custom", "tcp_device", "tcp device", "unknown"),
                     ("InstrumentType.custom", "InstrumentType.tcp_device")),
        # ── Faults ──────────────────────────────────────────────────────────
        VocabConcept("open_circuit", "fault", "Open circuit", FaultKind.OPEN_CIRCUIT,
                     ("open_circuit", "open circuit", "open", "opened"),
                     ("FaultType.open_circuit",), FaultCategory.PASSIVE_COMPONENTS),
        VocabConcept("short_circuit", "fault", "Short circuit", FaultKind.SHORT_CIRCUIT,
                     ("short_circuit", "short circuit", "short", "shorted"),
                     ("FaultType.short_circuit",), FaultCategory.POWER),
        VocabConcept("over_voltage", "fault", "Over-voltage", FaultKind.OVER_VOLTAGE,
                     ("over_voltage", "overvoltage", "over voltage", "over-voltage", "ovp"),
                     ("FaultType.over_voltage",), FaultCategory.POWER),
        VocabConcept("over_current", "fault", "Over-current", FaultKind.OVER_CURRENT,
                     ("over_current", "overcurrent", "over current", "over-current", "ocp"),
                     ("FaultType.over_current",), FaultCategory.POWER),
        VocabConcept("communication", "fault", "Communication failure", FaultKind.COMMUNICATION,
                     ("communication", "comm", "communication failure", "comm failure", "bus_off",
                      "bus off", "busoff", "link_down", "link down", "linkdown"),
                     ("FaultType.communication",), FaultCategory.COMMUNICATION_INTERCONNECTS),
        VocabConcept("out_of_range", "fault", "Measurement out of range", FaultKind.OUT_OF_RANGE,
                     ("out_of_range", "out of range", "measurement_out_of_range", "oor", "exceed",
                      "exceeded", "out-of-range"),
                     ("FaultType.measurement_out_of_range",), FaultCategory.MIXED_SIGNAL_TIMING),
        VocabConcept("relay_fault", "fault", "Relay fault", FaultKind.RELAY_FAULT,
                     ("relay_fault", "relay fault", "stuck relay", "relay"),
                     ("FaultType.relay_fault",), FaultCategory.ASSEMBLY_SOLDERING),
        VocabConcept("timeout", "fault", "Timeout / no response", FaultKind.TIMEOUT,
                     ("timeout", "time_out", "timed out", "no response"),
                     ("fault_injector action",), FaultCategory.COMMUNICATION_INTERCONNECTS),
        VocabConcept("signal_loss", "fault", "Signal loss", FaultKind.SIGNAL_LOSS,
                     ("signal_loss", "signal loss", "loss of signal", "no signal"),
                     ("fault_injector action",), FaultCategory.COMMUNICATION_INTERCONNECTS),
        VocabConcept("intermittent", "fault", "Intermittent failure", FaultKind.INTERMITTENT,
                     ("intermittent", "intermittent failure", "flaky"),
                     ("failure descriptions",), FaultCategory.ASSEMBLY_SOLDERING),
        VocabConcept("noise", "fault", "Noise / ripple / glitch", FaultKind.NOISE,
                     ("noise", "ripple", "glitch", "emi", "crosstalk"),
                     ("failure descriptions",), FaultCategory.MIXED_SIGNAL_TIMING),
        VocabConcept("drift", "fault", "Drift / offset / deviation", FaultKind.DRIFT,
                     ("drift", "offset", "deviation", "out of spec", "out-of-spec"),
                     ("failure descriptions",), FaultCategory.PASSIVE_COMPONENTS),
        VocabConcept("overheat", "fault", "Overheat / thermal runaway", FaultKind.OVERHEAT,
                     ("overheat", "overheating", "thermal", "thermal runaway", "tsd"),
                     ("failure descriptions",), FaultCategory.ENVIRONMENTAL_ESD),
    )


_CONCEPTS: tuple[VocabConcept, ...] = _concepts()

_NORMALIZE = re.compile(r"[^a-z0-9]+")
_PREFIXED = re.compile(r"^([a-z]+)_[0-9]+$")


def _normalize(text: str) -> str:
    return _NORMALIZE.sub("_", text.lower()).strip("_")


def _alias_index(scheme: VocabScheme) -> dict[str, VocabConcept]:
    index: dict[str, VocabConcept] = {}
    for concept in _CONCEPTS:
        if concept.scheme != scheme:
            continue
        for alias in (concept.canonical, *concept.aliases):
            key = _normalize(alias)
            if key and key not in index:
                index[key] = concept
    return index


_INSTRUMENT_ALIASES = _alias_index("instrument")
_FAULT_ALIASES = _alias_index("fault")


def _resolve(text: str, index: dict[str, VocabConcept]) -> VocabConcept | None:
    key = _normalize(text)
    if not key:
        return None
    concept = index.get(key)
    if concept is not None:
        return concept
    # Instrument/resource ids like "DMM-01", "OSC_2" → use the name prefix.
    match = _PREFIXED.match(key)
    if match:
        return index.get(match.group(1))
    return None


def concepts() -> tuple[VocabConcept, ...]:
    """All controlled-vocabulary concepts (instruments + faults)."""
    return _CONCEPTS


def resolve_instrument(text: str) -> VocabConcept | None:
    """Resolve any legacy instrument spelling to its canonical concept."""
    return _resolve(text, _INSTRUMENT_ALIASES)


def resolve_fault(text: str) -> VocabConcept | None:
    """Resolve any legacy fault spelling to its canonical concept."""
    return _resolve(text, _FAULT_ALIASES)


def canonical_instrument(text: str) -> str | None:
    """Canonical instrument ID for ``text``, or ``None`` when unrecognized."""
    concept = resolve_instrument(text)
    return concept.canonical if concept else None


def canonical_fault(text: str) -> str | None:
    """Canonical fault ID for ``text``, or ``None`` when unrecognized."""
    concept = resolve_fault(text)
    return concept.canonical if concept else None


__all__ = [
    "FaultCategory",
    "FaultKind",
    "InstrumentKind",
    "VocabConcept",
    "canonical_fault",
    "canonical_instrument",
    "concepts",
    "resolve_fault",
    "resolve_instrument",
]
