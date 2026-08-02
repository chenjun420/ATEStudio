"""Tests for station-level configuration schema (config_schema.py).

Validates:
- YAML → Pydantic parse round-trip (parse → serialize → parse equality)
- Required field validation (missing fields raise ValidationError)
- Extra field rejection (extra='forbid')
- ISA-95 location format validation
- ISO date validation for calibration_due
- Example YAML constants validate successfully
- ConfigManifest round-trip with multiple stations
- Export availability via shared package __init__
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from shared.config_schema import (
    EXAMPLE_CONFIG_MANIFEST_YAML,
    EXAMPLE_STATION_CONFIG_YAML,
    ConfigManifest,
    InstrumentConfig,
    StationConfig,
    parse_config_manifest,
    parse_station_config,
    serialize_config_manifest,
    serialize_station_config,
)

# ---------------------------------------------------------------------------
# InstrumentConfig
# ---------------------------------------------------------------------------


class TestInstrumentConfig:
    """Tests for InstrumentConfig model."""

    def test_valid_instrument(self) -> None:
        """InstrumentConfig accepts all required fields."""
        inst = InstrumentConfig(
            instrument_id="DMM-01",
            instrument_type="digital_multimeter",
            address="TCPIP::192.168.1.10::inst0::INSTR",
            calibration_due="2025-12-31",
        )
        assert inst.instrument_id == "DMM-01"
        assert inst.instrument_type == "digital_multimeter"
        assert inst.address == "TCPIP::192.168.1.10::inst0::INSTR"
        assert inst.calibration_due == "2025-12-31"

    def test_calibration_due_optional(self) -> None:
        """calibration_due defaults to None."""
        inst = InstrumentConfig(
            instrument_id="OSC-01",
            instrument_type="oscilloscope",
            address="USB0::0x1234::INSTR",
        )
        assert inst.calibration_due is None

    def test_missing_required_field_raises(self) -> None:
        """Missing instrument_id raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            InstrumentConfig(
                instrument_type="oscilloscope",
                address="USB0::0x1234::INSTR",
            )
        assert "instrument_id" in str(exc_info.value)

    def test_missing_address_raises(self) -> None:
        """Missing address raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            InstrumentConfig(
                instrument_id="OSC-01",
                instrument_type="oscilloscope",
            )
        assert "address" in str(exc_info.value)

    def test_extra_field_rejected(self) -> None:
        """Unknown field is rejected by extra='forbid'."""
        with pytest.raises(ValidationError) as exc_info:
            InstrumentConfig(
                instrument_id="OSC-01",
                instrument_type="oscilloscope",
                address="USB0::INSTR",
                unknown_field="bad",
            )
        assert "extra" in str(exc_info.value).lower()

    def test_empty_string_id_rejected(self) -> None:
        """Empty instrument_id is rejected by min_length=1."""
        with pytest.raises(ValidationError):
            InstrumentConfig(
                instrument_id="",
                instrument_type="oscilloscope",
                address="USB0::INSTR",
            )

    def test_invalid_iso_date_rejected(self) -> None:
        """Non-ISO date string for calibration_due raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            InstrumentConfig(
                instrument_id="DMM-01",
                instrument_type="digital_multimeter",
                address="TCPIP::192.168.1.10::INSTR",
                calibration_due="2025/12/31",
            )
        assert "calibration_due" in str(exc_info.value)

    def test_iso_date_with_time_rejected(self) -> None:
        """Full datetime string is rejected; only date format (YYYY-MM-DD) accepted."""
        with pytest.raises(ValidationError):
            InstrumentConfig(
                instrument_id="DMM-01",
                instrument_type="digital_multimeter",
                address="TCPIP::INSTR",
                calibration_due="2025-12-31T10:00:00",
            )

    def test_valid_iso_date_accepted(self) -> None:
        """Valid ISO date (YYYY-MM-DD) is accepted."""
        inst = InstrumentConfig(
            instrument_id="DMM-01",
            instrument_type="digital_multimeter",
            address="TCPIP::INSTR",
            calibration_due="2025-01-15",
        )
        assert inst.calibration_due == "2025-01-15"


# ---------------------------------------------------------------------------
# StationConfig
# ---------------------------------------------------------------------------


class TestStationConfig:
    """Tests for StationConfig model."""

    def _make_valid_station(self) -> StationConfig:
        """Create a valid StationConfig for testing."""
        return StationConfig(
            station_id="ST-001",
            station_name="Final Test Station A",
            location="acme.shanghai.plant_a.final_test.station_01",
            capabilities=["rf_test", "power_test"],
            assigned_products=["prod_comm_module_v2"],
            instrument_configs=[
                InstrumentConfig(
                    instrument_id="DMM-01",
                    instrument_type="digital_multimeter",
                    address="TCPIP::192.168.1.10::inst0::INSTR",
                    calibration_due="2025-12-31",
                ),
            ],
            test_limits_version="limits_v2.1.0",
        )

    def test_valid_station(self) -> None:
        """StationConfig accepts all fields correctly."""
        station = self._make_valid_station()
        assert station.station_id == "ST-001"
        assert station.station_name == "Final Test Station A"
        assert station.location == "acme.shanghai.plant_a.final_test.station_01"
        assert station.capabilities == ["rf_test", "power_test"]
        assert station.assigned_products == ["prod_comm_module_v2"]
        assert len(station.instrument_configs) == 1
        assert station.test_limits_version == "limits_v2.1.0"

    def test_defaults_empty_lists(self) -> None:
        """Optional list fields default to empty lists."""
        station = StationConfig(
            station_id="ST-002",
            station_name="Minimal Station",
            location="ent.site.area.line.station",
        )
        assert station.capabilities == []
        assert station.assigned_products == []
        assert station.instrument_configs == []
        assert station.test_limits_version is None

    def test_missing_station_id_raises(self) -> None:
        """Missing station_id raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            StationConfig(
                station_name="Test Station",
                location="ent.site.area.line.station",
            )
        assert "station_id" in str(exc_info.value)

    def test_missing_station_name_raises(self) -> None:
        """Missing station_name raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            StationConfig(
                station_id="ST-001",
                location="ent.site.area.line.station",
            )
        assert "station_name" in str(exc_info.value)

    def test_missing_location_raises(self) -> None:
        """Missing location raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            StationConfig(
                station_id="ST-001",
                station_name="Test Station",
            )
        assert "location" in str(exc_info.value)

    def test_extra_field_rejected(self) -> None:
        """Unknown field is rejected by extra='forbid'."""
        with pytest.raises(ValidationError) as exc_info:
            StationConfig(
                station_id="ST-001",
                station_name="Test Station",
                location="ent.site.area.line.station",
                unknown_field="bad",
            )
        assert "extra" in str(exc_info.value).lower()

    def test_location_too_few_segments_rejected(self) -> None:
        """Location with fewer than 5 dot-separated levels is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            StationConfig(
                station_id="ST-001",
                station_name="Test Station",
                location="enterprise.site.area",
            )
        assert "ISA-95" in str(exc_info.value)

    def test_location_empty_segment_rejected(self) -> None:
        """Location with empty segment between dots is rejected."""
        with pytest.raises(ValidationError):
            StationConfig(
                station_id="ST-001",
                station_name="Test Station",
                location="enterprise..area.line.station",
            )

    def test_location_exactly_5_segments_accepted(self) -> None:
        """Location with exactly 5 dot-separated levels is accepted."""
        station = StationConfig(
            station_id="ST-001",
            station_name="Test Station",
            location="enterprise.site.area.line.station",
        )
        assert station.location == "enterprise.site.area.line.station"

    def test_location_more_than_5_segments_accepted(self) -> None:
        """Location with more than 5 levels is accepted (ISA-95 allows extension)."""
        station = StationConfig(
            station_id="ST-001",
            station_name="Test Station",
            location="enterprise.site.area.line.cell.station",
        )
        assert station.location == "enterprise.site.area.line.cell.station"


# ---------------------------------------------------------------------------
# ConfigManifest
# ---------------------------------------------------------------------------


class TestConfigManifest:
    """Tests for ConfigManifest model."""

    def test_valid_manifest(self) -> None:
        """ConfigManifest accepts version and stations list."""
        station = StationConfig(
            station_id="ST-001",
            station_name="Station A",
            location="enterprise.site.area.line.station",
        )
        manifest = ConfigManifest(version="1.0.0", stations=[station])
        assert manifest.version == "1.0.0"
        assert len(manifest.stations) == 1

    def test_empty_stations_default(self) -> None:
        """stations defaults to empty list."""
        manifest = ConfigManifest(version="1.0.0")
        assert manifest.stations == []

    def test_missing_version_raises(self) -> None:
        """Missing version raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            ConfigManifest(stations=[])
        assert "version" in str(exc_info.value)

    def test_extra_field_rejected(self) -> None:
        """Unknown field is rejected by extra='forbid'."""
        with pytest.raises(ValidationError) as exc_info:
            ConfigManifest(version="1.0.0", stations=[], extra_field="bad")
        assert "extra" in str(exc_info.value).lower()

    def test_multiple_stations(self) -> None:
        """ConfigManifest accepts multiple stations."""
        manifest = ConfigManifest(
            version="2.0.0",
            stations=[
                StationConfig(
                    station_id="ST-001",
                    station_name="Station A",
                    location="ent.site.area.line.station1",
                ),
                StationConfig(
                    station_id="ST-002",
                    station_name="Station B",
                    location="ent.site.area.line.station2",
                ),
            ],
        )
        assert len(manifest.stations) == 2
        assert manifest.stations[0].station_id == "ST-001"
        assert manifest.stations[1].station_id == "ST-002"


# ---------------------------------------------------------------------------
# Parse / serialize round-trip
# ---------------------------------------------------------------------------


class TestStationConfigRoundTrip:
    """Round-trip tests: parse → serialize → parse equality for StationConfig."""

    def test_round_trip_basic(self) -> None:
        """parse → serialize → parse produces identical StationConfig."""
        original = StationConfig(
            station_id="ST-001",
            station_name="Final Test Station A",
            location="acme.shanghai.plant_a.final_test.station_01",
            capabilities=["rf_test", "power_test"],
            assigned_products=["prod_comm_module_v2"],
            instrument_configs=[
                InstrumentConfig(
                    instrument_id="DMM-01",
                    instrument_type="digital_multimeter",
                    address="TCPIP::192.168.1.10::inst0::INSTR",
                    calibration_due="2025-12-31",
                ),
            ],
            test_limits_version="limits_v2.1.0",
        )
        yaml_str = serialize_station_config(original)
        reparsed = parse_station_config(yaml_str)
        assert reparsed == original

    def test_round_trip_with_none_fields(self) -> None:
        """Round-trip preserves None values for optional fields."""
        original = StationConfig(
            station_id="ST-002",
            station_name="Minimal Station",
            location="enterprise.site.area.line.station",
            instrument_configs=[
                InstrumentConfig(
                    instrument_id="OSC-01",
                    instrument_type="oscilloscope",
                    address="USB0::0x1234::INSTR",
                    calibration_due=None,
                ),
            ],
            test_limits_version=None,
        )
        yaml_str = serialize_station_config(original)
        reparsed = parse_station_config(yaml_str)
        assert reparsed == original
        assert reparsed.test_limits_version is None
        assert reparsed.instrument_configs[0].calibration_due is None

    def test_round_trip_empty_lists(self) -> None:
        """Round-trip preserves empty lists."""
        original = StationConfig(
            station_id="ST-003",
            station_name="Empty Station",
            location="enterprise.site.area.line.station",
        )
        yaml_str = serialize_station_config(original)
        reparsed = parse_station_config(yaml_str)
        assert reparsed == original
        assert reparsed.capabilities == []
        assert reparsed.assigned_products == []
        assert reparsed.instrument_configs == []

    def test_round_trip_multiple_instruments(self) -> None:
        """Round-trip with multiple instrument configs."""
        original = StationConfig(
            station_id="ST-001",
            station_name="Multi-Instrument Station",
            location="acme.shanghai.plant_a.final_test.station_01",
            capabilities=["rf_test", "power_test", "functional_test"],
            assigned_products=["prod_a", "prod_b", "prod_c"],
            instrument_configs=[
                InstrumentConfig(
                    instrument_id="DMM-01",
                    instrument_type="digital_multimeter",
                    address="TCPIP::192.168.1.10::inst0::INSTR",
                    calibration_due="2025-12-31",
                ),
                InstrumentConfig(
                    instrument_id="OSC-01",
                    instrument_type="oscilloscope",
                    address="TCPIP::192.168.1.11::inst0::INSTR",
                    calibration_due="2026-06-30",
                ),
                InstrumentConfig(
                    instrument_id="PSU-01",
                    instrument_type="power_supply",
                    address="USB0::0x1234::INSTR",
                    calibration_due=None,
                ),
            ],
            test_limits_version="limits_v3.0.0",
        )
        yaml_str = serialize_station_config(original)
        reparsed = parse_station_config(yaml_str)
        assert reparsed == original
        assert len(reparsed.instrument_configs) == 3

    def test_serialize_produces_valid_yaml(self) -> None:
        """serialize_station_config produces parseable YAML."""
        station = StationConfig(
            station_id="ST-001",
            station_name="Test Station",
            location="enterprise.site.area.line.station",
        )
        yaml_str = serialize_station_config(station)
        # Should be parseable by yaml.safe_load
        data = yaml.safe_load(yaml_str)
        assert isinstance(data, dict)
        assert data["station_id"] == "ST-001"

    def test_serialize_preserves_field_order(self) -> None:
        """Serialized YAML preserves field definition order (sort_keys=False)."""
        station = StationConfig(
            station_id="ST-001",
            station_name="Test Station",
            location="enterprise.site.area.line.station",
        )
        yaml_str = serialize_station_config(station)
        lines = [line for line in yaml_str.strip().split("\n") if not line.startswith(" ")]
        # station_id should be the first top-level key
        assert lines[0].startswith("station_id")


class TestConfigManifestRoundTrip:
    """Round-trip tests: parse → serialize → parse equality for ConfigManifest."""

    def test_round_trip_single_station(self) -> None:
        """Manifest round-trip with a single station."""
        original = ConfigManifest(
            version="1.0.0",
            stations=[
                StationConfig(
                    station_id="ST-001",
                    station_name="Station A",
                    location="enterprise.site.area.line.station",
                    capabilities=["rf_test"],
                    instrument_configs=[
                        InstrumentConfig(
                            instrument_id="DMM-01",
                            instrument_type="digital_multimeter",
                            address="TCPIP::192.168.1.10::INSTR",
                            calibration_due="2025-12-31",
                        ),
                    ],
                ),
            ],
        )
        yaml_str = serialize_config_manifest(original)
        reparsed = parse_config_manifest(yaml_str)
        assert reparsed == original

    def test_round_trip_multiple_stations(self) -> None:
        """Manifest round-trip with multiple stations."""
        original = ConfigManifest(
            version="2.1.0",
            stations=[
                StationConfig(
                    station_id="ST-001",
                    station_name="Station A",
                    location="ent.site.area.line.s1",
                    capabilities=["rf_test"],
                ),
                StationConfig(
                    station_id="ST-002",
                    station_name="Station B",
                    location="ent.site.area.line.s2",
                    capabilities=["thermal_test"],
                    instrument_configs=[
                        InstrumentConfig(
                            instrument_id="PSU-01",
                            instrument_type="power_supply",
                            address="USB0::INSTR",
                            calibration_due=None,
                        ),
                    ],
                    test_limits_version="limits_v1.0",
                ),
            ],
        )
        yaml_str = serialize_config_manifest(original)
        reparsed = parse_config_manifest(yaml_str)
        assert reparsed == original
        assert len(reparsed.stations) == 2

    def test_round_trip_empty_manifest(self) -> None:
        """Manifest round-trip with no stations."""
        original = ConfigManifest(version="0.1.0", stations=[])
        yaml_str = serialize_config_manifest(original)
        reparsed = parse_config_manifest(yaml_str)
        assert reparsed == original
        assert reparsed.stations == []


# ---------------------------------------------------------------------------
# Example YAML constants
# ---------------------------------------------------------------------------


class TestExampleYaml:
    """Tests that the module-level example YAML constants validate."""

    def test_example_station_config_yaml_validates(self) -> None:
        """EXAMPLE_STATION_CONFIG_YAML parses into a valid StationConfig."""
        station = parse_station_config(EXAMPLE_STATION_CONFIG_YAML)
        assert station.station_id == "ST-001"
        assert station.station_name == "Final Test Station A"
        assert station.location == "acme.shanghai.plant_a.final_test.station_01"
        assert "rf_test" in station.capabilities
        assert len(station.instrument_configs) == 2
        assert station.instrument_configs[0].instrument_id == "DMM-01"
        assert station.instrument_configs[0].calibration_due == "2025-12-31"
        assert station.instrument_configs[1].calibration_due is None
        assert station.test_limits_version == "limits_v2.1.0"

    def test_example_station_config_yaml_round_trip(self) -> None:
        """EXAMPLE_STATION_CONFIG_YAML survives parse → serialize → parse."""
        original = parse_station_config(EXAMPLE_STATION_CONFIG_YAML)
        yaml_str = serialize_station_config(original)
        reparsed = parse_station_config(yaml_str)
        assert reparsed == original

    def test_example_config_manifest_yaml_validates(self) -> None:
        """EXAMPLE_CONFIG_MANIFEST_YAML parses into a valid ConfigManifest."""
        manifest = parse_config_manifest(EXAMPLE_CONFIG_MANIFEST_YAML)
        assert manifest.version == "1.0.0"
        assert len(manifest.stations) == 2
        assert manifest.stations[0].station_id == "ST-001"
        assert manifest.stations[1].station_id == "ST-002"
        assert manifest.stations[1].instrument_configs == []

    def test_example_config_manifest_yaml_round_trip(self) -> None:
        """EXAMPLE_CONFIG_MANIFEST_YAML survives parse → serialize → parse."""
        original = parse_config_manifest(EXAMPLE_CONFIG_MANIFEST_YAML)
        yaml_str = serialize_config_manifest(original)
        reparsed = parse_config_manifest(yaml_str)
        assert reparsed == original


# ---------------------------------------------------------------------------
# Parse error cases
# ---------------------------------------------------------------------------


class TestParseErrors:
    """Tests for YAML parse and validation errors."""

    def test_parse_invalid_yaml_raises(self) -> None:
        """Malformed YAML raises yaml.YAMLError."""
        with pytest.raises(yaml.YAMLError):
            parse_station_config("station_id: [unclosed")

    def test_parse_missing_required_field_raises(self) -> None:
        """YAML missing station_id raises ValidationError."""
        yaml_str = "station_name: Test\nlocation: ent.site.area.line.station\n"
        with pytest.raises(ValidationError) as exc_info:
            parse_station_config(yaml_str)
        assert "station_id" in str(exc_info.value)

    def test_parse_extra_field_raises(self) -> None:
        """YAML with extra unknown field raises ValidationError."""
        yaml_str = (
            "station_id: ST-001\n"
            "station_name: Test\n"
            "location: ent.site.area.line.station\n"
            "rogue_field: bad\n"
        )
        with pytest.raises(ValidationError) as exc_info:
            parse_station_config(yaml_str)
        assert "extra" in str(exc_info.value).lower()

    def test_parse_manifest_invalid_station_raises(self) -> None:
        """Manifest YAML with invalid station raises ValidationError."""
        yaml_str = (
            "version: '1.0.0'\n"
            "stations:\n"
            "  - station_name: Missing ID\n"
            "    location: ent.site.area.line.station\n"
        )
        with pytest.raises(ValidationError) as exc_info:
            parse_config_manifest(yaml_str)
        assert "station_id" in str(exc_info.value)

    def test_parse_empty_string_raises(self) -> None:
        """Empty YAML string raises ValidationError (None data)."""
        with pytest.raises(ValidationError):
            parse_station_config("")

    def test_parse_manifest_empty_string_raises(self) -> None:
        """Empty YAML string raises ValidationError for manifest."""
        with pytest.raises(ValidationError):
            parse_config_manifest("")


# ---------------------------------------------------------------------------
# Package exports
# ---------------------------------------------------------------------------


class TestPackageExports:
    """Tests that symbols are exported from the shared package."""

    def test_import_from_shared_package(self) -> None:
        """All config_schema symbols are importable from the shared package."""
        from shared import (  # noqa: F401
            ConfigManifest,
            InstrumentConfig,
            StationConfig,
            parse_config_manifest,
            parse_station_config,
            serialize_config_manifest,
            serialize_station_config,
        )

    def test_symbols_in_all(self) -> None:
        """config_schema symbols appear in shared.__all__."""
        import shared

        expected = {
            "InstrumentConfig",
            "StationConfig",
            "ConfigManifest",
            "parse_station_config",
            "serialize_station_config",
            "parse_config_manifest",
            "serialize_config_manifest",
        }
        assert expected.issubset(set(shared.__all__))
