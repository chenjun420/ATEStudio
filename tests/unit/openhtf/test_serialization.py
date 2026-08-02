"""Unit tests for OpenHTF TestRecord serialization (Todo 21).

Tests cover the ``as_base_types`` function and its three private helpers
(``_serialize_measurement``, ``_serialize_phase``, ``_serialize_attachment``)
which convert OpenHTF's attrs-based TestRecord tree into a plain dict of
JSON-serializable base types.

The fake objects use ``SimpleNamespace`` (matching the Todo 19 test pattern)
with exactly the attributes the serializer reads -- no openhtf runtime
dependency required.
"""

from __future__ import annotations

import json
from base64 import b64encode
from types import SimpleNamespace
from typing import Any

from ate_platform.openhtf import as_base_types
from ate_platform.openhtf.serialization import (
    _serialize_attachment,
    _serialize_measurement,
    _serialize_phase,
)


def _make_measurement(
    name: str,
    value: Any,
    outcome_name: str = "PASS",
    units: str | None = None,
) -> SimpleNamespace:
    """Build a fake Measurement with the attributes the serializer reads."""
    return SimpleNamespace(
        name=name,
        value=value,
        outcome=SimpleNamespace(name=outcome_name),
        units=units,
    )


def _make_attachment(
    data: bytes,
    mimetype: str = "application/octet-stream",
) -> SimpleNamespace:
    """Build a fake Attachment with the attributes the serializer reads."""
    return SimpleNamespace(
        data=data,
        mimetype=mimetype,
    )


def _make_phase(
    name: str,
    measurements: dict[str, SimpleNamespace] | None = None,
    attachments: dict[str, SimpleNamespace] | None = None,
    outcome_name: str = "PASS",
) -> SimpleNamespace:
    """Build a fake PhaseRecord with the attributes the serializer reads."""
    return SimpleNamespace(
        name=name,
        outcome=SimpleNamespace(name=outcome_name),
        start_time_millis=1000,
        end_time_millis=2000,
        measurements=measurements or {},
        attachments=attachments or {},
    )


def _make_test_record(
    phases: list[SimpleNamespace],
    outcome_name: str = "PASS",
    dut_id: str = "DUT-001",
) -> SimpleNamespace:
    """Build a fake TestRecord with the full field set the serializer reads."""
    return SimpleNamespace(
        dut_id=dut_id,
        station_id="STATION-01",
        start_time_millis=1000,
        end_time_millis=3000,
        outcome=SimpleNamespace(name=outcome_name),
        outcome_details=[
            SimpleNamespace(code="INFO", description="all good"),
        ],
        metadata={"version": "1.0"},
        phases=phases,
        marginal=False,
    )


class _CustomValue:
    """Non-base-type used to verify str() coercion of measurement values."""

    def __str__(self) -> str:
        return "custom-value"


class TestSerialization:
    """Verify as_base_types and helpers produce JSON-serializable dicts."""

    def test_measurement_to_base_types(self) -> None:
        """Measurements serialize to {value, unit, outcome} base-type dicts.

        Given: three measurements -- a numeric value with units, a None
            value (unset measurement), and a non-base-type value (custom
            object).
        When: each is passed to ``_serialize_measurement``.
        Then: the output dict has ``value`` (base type or coerced str),
            ``unit`` (str|None), and ``outcome`` (name string) -- all
            JSON-serializable.
        """
        # Numeric value with units.
        m_voltage = _make_measurement("voltage", 5.0, "PASS", "volts")
        assert _serialize_measurement(m_voltage) == {
            "value": 5.0,
            "unit": "volts",
            "outcome": "PASS",
        }

        # None value (unset measurement) -- edge case.
        m_unset = _make_measurement("unset", None, "FAIL", None)
        assert _serialize_measurement(m_unset) == {
            "value": None,
            "unit": None,
            "outcome": "FAIL",
        }

        # Non-base-type value coerced to str.
        m_custom = _make_measurement("custom", _CustomValue(), "PASS", None)
        result = _serialize_measurement(m_custom)
        assert result["value"] == "custom-value"
        assert result["unit"] is None
        assert result["outcome"] == "PASS"

    def test_phase_to_base_types(self) -> None:
        """Phases serialize to {name, outcome, timing, measurements, attachments}.

        Given: a phase with measurements and attachments, and a phase with
            no measurements and no attachments (edge case).
        When: each is passed to ``_serialize_phase``.
        Then: the output dict has ``name``, ``outcome`` (name string),
            ``start_time_millis``, ``end_time_millis``, ``measurements``
            (dict), and ``attachments`` (list). Empty phases produce empty
            containers, not None.
        """
        # Phase with measurements and attachments.
        phase_full = _make_phase(
            "power_rail",
            measurements={
                "voltage": _make_measurement("voltage", 5.0, "PASS", "volts"),
                "current": _make_measurement("current", 0.12, "PASS", "amps"),
            },
            attachments={
                "screenshot": _make_attachment(b"\x89PNGfake", "image/png"),
            },
        )
        result = _serialize_phase(phase_full)
        assert result["name"] == "power_rail"
        assert result["outcome"] == "PASS"
        assert result["start_time_millis"] == 1000
        assert result["end_time_millis"] == 2000
        assert set(result["measurements"].keys()) == {"voltage", "current"}
        assert result["measurements"]["voltage"] == {
            "value": 5.0,
            "unit": "volts",
            "outcome": "PASS",
        }
        assert result["measurements"]["current"] == {
            "value": 0.12,
            "unit": "amps",
            "outcome": "PASS",
        }
        assert len(result["attachments"]) == 1
        assert result["attachments"][0]["name"] == "screenshot"
        assert result["attachments"][0]["mime_type"] == "image/png"

        # Phase with no measurements and no attachments -- edge case.
        phase_empty = _make_phase("idle", measurements=None, attachments=None)
        result_empty = _serialize_phase(phase_empty)
        assert result_empty["measurements"] == {}
        assert result_empty["attachments"] == []

    def test_attachment_to_base_types(self) -> None:
        """Attachments serialize to {name, mime_type, size, data (base64)}.

        Given: a binary attachment with a mimetype, and an attachment with
            empty data (edge case).
        When: each is passed to ``_serialize_attachment``.
        Then: the output dict has ``name`` (from the parameter), ``mime_type``
            (from ``attachment.mimetype``), ``size`` (byte count), and
            ``data`` (base64-encoded ASCII string). Empty data yields
            ``size=0`` and ``data=""``.
        """
        # Binary attachment.
        binary = b"\x89PNG\r\n\x1a\nfake-screenshot"
        att = _make_attachment(binary, "image/png")
        result = _serialize_attachment("capture.png", att)
        assert result["name"] == "capture.png"
        assert result["mime_type"] == "image/png"
        assert result["size"] == len(binary)
        assert result["data"] == b64encode(binary).decode("ascii")

        # Empty data attachment -- edge case.
        empty = _make_attachment(b"", "text/plain")
        result_empty = _serialize_attachment("empty.log", empty)
        assert result_empty["name"] == "empty.log"
        assert result_empty["mime_type"] == "text/plain"
        assert result_empty["size"] == 0
        assert result_empty["data"] == ""

    def test_full_testrecord_to_dict(self) -> None:
        """Full TestRecord serializes to a JSON-serializable base-type dict.

        Given: a TestRecord with two phases (setup with a measurement and an
            attachment; measure with a measurement only), outcome_details,
            metadata, and a marginal flag.
        When: passed to ``as_base_types``.
        Then: the output dict mirrors every top-level field (dut_id,
            station_id, timing, outcome name string, outcome_details,
            metadata, marginal), every phase is serialized with its
            measurements and attachments, and the entire dict is
            JSON-serializable (``json.dumps`` does not raise).
        """
        record = _make_test_record(
            phases=[
                _make_phase(
                    "setup",
                    measurements={
                        "voltage": _make_measurement("voltage", 5.0, "PASS", "volts"),
                    },
                    attachments={
                        "log": _make_attachment(b"hello world", "text/plain"),
                    },
                ),
                _make_phase(
                    "measure",
                    measurements={
                        "frequency": _make_measurement(
                            "frequency", 16_000_000, "PASS", "Hz"
                        ),
                    },
                ),
            ],
            outcome_name="PASS",
            dut_id="DUT-42",
        )

        result = as_base_types(record)

        # Top-level fields.
        assert result["dut_id"] == "DUT-42"
        assert result["station_id"] == "STATION-01"
        assert result["start_time_millis"] == 1000
        assert result["end_time_millis"] == 3000
        assert result["outcome"] == "PASS"
        assert result["marginal"] is False
        assert result["metadata"] == {"version": "1.0"}
        assert result["outcome_details"] == [
            {"code": "INFO", "description": "all good"},
        ]

        # Phases.
        assert len(result["phases"]) == 2

        phase_setup = result["phases"][0]
        assert phase_setup["name"] == "setup"
        assert phase_setup["outcome"] == "PASS"
        assert phase_setup["measurements"]["voltage"] == {
            "value": 5.0,
            "unit": "volts",
            "outcome": "PASS",
        }
        assert len(phase_setup["attachments"]) == 1
        att = phase_setup["attachments"][0]
        assert att["name"] == "log"
        assert att["mime_type"] == "text/plain"
        assert att["size"] == 11
        assert att["data"] == b64encode(b"hello world").decode("ascii")

        phase_measure = result["phases"][1]
        assert phase_measure["name"] == "measure"
        assert phase_measure["measurements"]["frequency"] == {
            "value": 16_000_000,
            "unit": "Hz",
            "outcome": "PASS",
        }
        assert phase_measure["attachments"] == []

        # JSON-serializable: every value is a base type.
        json.dumps(result)
