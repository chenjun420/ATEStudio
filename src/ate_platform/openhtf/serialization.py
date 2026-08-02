"""OpenHTF TestRecord serialization to base Python types.

Converts OpenHTF's attrs-based TestRecord, PhaseRecord, Measurement, and
Attachment objects into plain dicts containing only JSON-serializable base
types (str, int, float, bool, list, dict). Required for cross-process
communication (Todo 22 spawn context) where the raw attrs objects are not
picklable across spawn boundaries.

This module complements the ``_extract_*`` helpers in ``step_executor.py``
(which retain raw enum objects for in-process inspection). ``as_base_types``
produces fully serializable output: enums -> name strings, bytes -> base64
strings.
"""

from __future__ import annotations

import base64
from typing import Any

__all__ = ["as_base_types"]


def as_base_types(record: Any) -> dict[str, Any]:
    """Convert an OpenHTF TestRecord to a dict of base Python types.

    Walks the full TestRecord tree: top-level fields, outcome_details,
    metadata, and every phase (with its measurements and attachments).
    All enum outcomes are converted to their name strings; all attachment
    payloads are base64-encoded; all measurement values are coerced to
    JSON-serializable base types.

    Args:
        record: The OpenHTF TestRecord (attrs-based). Fields are read via
            ``getattr`` with defaults, so missing attributes degrade
            gracefully to ``None`` / empty containers rather than raising.

    Returns:
        Dict with only str/int/float/bool/list/dict values, suitable for
        ``json.dumps`` or pickling across a ``multiprocessing`` spawn
        boundary.
    """
    outcome = getattr(record, "outcome", None)
    outcome_details = [
        {
            "code": getattr(d, "code", None),
            "description": getattr(d, "description", None),
        }
        for d in getattr(record, "outcome_details", []) or []
    ]
    phases = [_serialize_phase(p) for p in getattr(record, "phases", []) or []]
    return {
        "dut_id": getattr(record, "dut_id", None),
        "station_id": getattr(record, "station_id", None),
        "start_time_millis": getattr(record, "start_time_millis", None),
        "end_time_millis": getattr(record, "end_time_millis", None),
        "outcome": _serialize_outcome(outcome),
        "outcome_details": outcome_details,
        "metadata": dict(getattr(record, "metadata", {}) or {}),
        "phases": phases,
        "marginal": getattr(record, "marginal", None),
    }


def _serialize_phase(phase: Any) -> dict[str, Any]:
    """Convert a PhaseRecord to a dict of base Python types.

    Captures the phase's name, outcome (as name string), timing, the full
    measurements dict (name -> serialized measurement), and the list of
    attachments (each serialized with base64-encoded data).

    Args:
        phase: The OpenHTF PhaseRecord (attrs-based).

    Returns:
        Dict with ``name``, ``outcome``, ``start_time_millis``,
        ``end_time_millis``, ``measurements`` (dict), and ``attachments``
        (list).
    """
    outcome = getattr(phase, "outcome", None)
    measurements = getattr(phase, "measurements", {}) or {}
    attachments = getattr(phase, "attachments", {}) or {}
    return {
        "name": getattr(phase, "name", None),
        "outcome": _serialize_outcome(outcome),
        "start_time_millis": getattr(phase, "start_time_millis", None),
        "end_time_millis": getattr(phase, "end_time_millis", None),
        "measurements": {
            name: _serialize_measurement(meas) for name, meas in measurements.items()
        },
        "attachments": [
            _serialize_attachment(name, attachment)
            for name, attachment in attachments.items()
        ],
    }


def _serialize_measurement(meas: Any) -> dict[str, Any]:
    """Convert a Measurement to a dict of base Python types.

    Reads the measured value via ``getattr(meas, "value", None)`` (avoids
    coupling to the ``MeasuredValue`` vs ``DimensionedMeasuredValue``
    internal shape), the units string, and the outcome enum.

    Args:
        meas: The OpenHTF Measurement (attrs-based).

    Returns:
        Dict with ``value`` (coerced to a base type), ``unit`` (str|None),
        and ``outcome`` (name string|None).
    """
    outcome = getattr(meas, "outcome", None)
    return {
        "value": _serialize_value(getattr(meas, "value", None)),
        "unit": getattr(meas, "units", None),
        "outcome": _serialize_outcome(outcome),
    }


def _serialize_attachment(name: str, attachment: Any) -> dict[str, Any]:
    """Convert an Attachment to a dict of base Python types.

    The OpenHTF Attachment stores its MIME type under the ``mimetype``
    attribute (no underscore); the serialized output key is ``mime_type``
    for consistency with common JSON conventions. Binary ``data`` is
    base64-encoded into an ASCII string.

    Args:
        name: The attachment's key in the phase's attachments dict.
        attachment: The OpenHTF Attachment (attrs-based) with ``data``
            (bytes) and ``mimetype`` (str) attributes.

    Returns:
        Dict with ``name``, ``mime_type``, ``size`` (byte count), and
        ``data`` (base64-encoded ASCII string).
    """
    raw_data: Any = getattr(attachment, "data", b"") or b""
    if isinstance(raw_data, str):
        data_bytes = raw_data.encode("utf-8", errors="replace")
    elif isinstance(raw_data, (bytes, bytearray)):
        data_bytes = bytes(raw_data)
    else:
        data_bytes = b""
    return {
        "name": name,
        "mime_type": getattr(attachment, "mimetype", None),
        "size": len(data_bytes),
        "data": base64.b64encode(data_bytes).decode("ascii"),
    }


def _serialize_outcome(outcome: Any) -> str | None:
    """Convert an OpenHTF Outcome enum to its name string.

    Handles ``None`` (unset outcome), enum-like objects with a ``.name``
    attribute, and bare strings (returned as-is via ``str()``).

    Args:
        outcome: The OpenHTF Outcome enum, ``None``, or a string.

    Returns:
        The outcome's name as a string, or ``None`` if outcome is ``None``.
    """
    if outcome is None:
        return None
    name = getattr(outcome, "name", None)
    return str(name) if name is not None else str(outcome)


def _serialize_value(value: Any) -> Any:
    """Coerce a measurement value to a JSON-serializable base type.

    Base types (bool, int, float, str) are returned unchanged. Lists and
    dicts are recursed into so that nested non-base types are also coerced.
    Any other type (e.g. ``Decimal``, ``datetime``, custom objects) is
    converted via ``str()``.

    Args:
        value: The measurement value (may be ``None``).

    Returns:
        A JSON-serializable value, or ``None``.
    """
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _serialize_value(v) for k, v in value.items()}
    return str(value)
