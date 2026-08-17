"""Software-defined configuration schema for ATE Platform stations.

This module defines Pydantic v2 models for station-level YAML configuration:
- InstrumentConfig: Single instrument definition (VISA address, calibration)
- StationConfig: Station-level config (identity, capabilities, products, instruments)
- ConfigManifest: Manifest wrapping a version + list of StationConfig entries

Worker 启动时读取本地 station_config.yml；云端可以 override 整个 ConfigManifest。

All models use ``extra='forbid'`` for strict validation -- unknown YAML keys
are rejected rather than silently ignored, preventing misconfiguration drift
between local and cloud-managed station definitions.
"""

from __future__ import annotations

from datetime import date

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "InstrumentConfig",
    "StationConfig",
    "ConfigManifest",
    "parse_station_config",
    "serialize_station_config",
    "parse_config_manifest",
    "serialize_config_manifest",
    "EXAMPLE_STATION_CONFIG_YAML",
    "EXAMPLE_CONFIG_MANIFEST_YAML",
]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class InstrumentConfig(BaseModel):
    """Configuration for a single physical or simulated instrument.

    仪器配置 -- 定义工位上连接的一台仪器（示波器、万用表、电源等）。

    Attributes:
        instrument_id: Unique identifier within the station (e.g. ``"DMM-01"``)
        instrument_type: Instrument category (e.g. ``"digital_multimeter"``)
        address: VISA resource string (e.g. ``"TCPIP::192.168.1.10::inst0::INSTR"``)
        calibration_due: ISO 8601 date (``YYYY-MM-DD``) when calibration expires, or ``None``
    """

    model_config = ConfigDict(extra="forbid")

    instrument_id: str = Field(..., min_length=1, description="Unique instrument identifier within the station")
    instrument_type: str = Field(..., min_length=1, description="Instrument category")
    address: str = Field(..., min_length=1, description="VISA resource address string")
    calibration_due: str | None = Field(default=None, description="ISO 8601 date (YYYY-MM-DD) when calibration expires")

    @field_validator("calibration_due")
    @classmethod
    def _validate_iso_date(cls, v: str | None) -> str | None:
        """Ensure calibration_due is a valid ISO 8601 date string when present."""
        if v is None:
            return None
        # Raises ValueError on invalid format -- Pydantic converts to ValidationError
        date.fromisoformat(v)
        return v


class StationConfig(BaseModel):
    """Configuration for a single test station (edge worker).

    工位配置 -- 描述一个产线工位的身份、能力、分配产品和仪器清单。
    Worker 启动时从本地 ``station_config.yml`` 读取此配置；云端可下发 override。

    Attributes:
        station_id: Unique station identifier (e.g. ``"ST-001"``)
        station_name: Human-readable station name
        location: ISA-95 hierarchy path (e.g. ``"enterprise.site.area.line.station"``)
        capabilities: List of capability tags this station supports (e.g. ``["rf_test"]``)
        assigned_products: List of product type references assigned to this station
        instrument_configs: Instrument configurations for instruments connected to this station
        test_limits_version: Version ref of test limits applied at this station, or ``None``
    """

    model_config = ConfigDict(extra="forbid")

    station_id: str = Field(..., min_length=1, description="Unique station identifier")
    station_name: str = Field(..., min_length=1, description="Human-readable station name")
    location: str = Field(
        ...,
        min_length=1,
        description="ISA-95 hierarchy path: enterprise.site.area.line.station",
    )
    capabilities: list[str] = Field(default_factory=list, description="Capability tags supported by this station")
    assigned_products: list[str] = Field(
        default_factory=list, description="Product type references assigned to this station"
    )
    instrument_configs: list[InstrumentConfig] = Field(
        default_factory=list, description="Instrument configurations for connected instruments"
    )
    test_limits_version: str | None = Field(
        default=None, description="Version ref of test limits applied at this station"
    )

    @field_validator("location")
    @classmethod
    def _validate_isa95_hierarchy(cls, v: str) -> str:
        """Ensure location follows ISA-95 dotted hierarchy with at least 5 levels.

        ISA-95 defines: enterprise.site.area.line.cell (or station).
        We enforce at least 5 dot-separated segments to match the canonical form.
        """
        segments = v.split(".")
        if len(segments) < 5:
            raise ValueError(
                f"location must be an ISA-95 hierarchy with at least 5 dot-separated levels "
                f"(e.g. 'enterprise.site.area.line.station'), got {len(segments)} levels: '{v}'"
            )
        if any(not seg.strip() for seg in segments):
            raise ValueError(f"location segments must be non-empty, got: '{v}'")
        return v


class ConfigManifest(BaseModel):
    """Manifest wrapping a configuration version and a list of station configs.

    配置清单 -- 云端下发的完整配置包，包含版本号和工位配置列表。
    云端可以下发整个 ConfigManifest 来 override 工位本地配置。

    Attributes:
        version: Configuration manifest version (e.g. ``"1.0.0"``)
        stations: List of StationConfig entries in this manifest
    """

    model_config = ConfigDict(extra="forbid")

    version: str = Field(..., min_length=1, description="Configuration manifest version")
    stations: list[StationConfig] = Field(default_factory=list, description="Station configurations in this manifest")


# ---------------------------------------------------------------------------
# Parse / serialize functions
# ---------------------------------------------------------------------------


def parse_station_config(yaml_str: str) -> StationConfig:
    """Parse a YAML string into a StationConfig.

    Args:
        yaml_str: YAML content representing a single station configuration.

    Returns:
        Validated StationConfig instance.

    Raises:
        yaml.YAMLError: If the YAML is malformed.
        pydantic.ValidationError: If the parsed data fails schema validation.
    """
    data = yaml.safe_load(yaml_str)
    return StationConfig.model_validate(data)


def serialize_station_config(config: StationConfig) -> str:
    """Serialize a StationConfig to a YAML string.

    Uses ``sort_keys=False`` to preserve field definition order for
    deterministic, human-readable output.

    Args:
        config: StationConfig instance to serialize.

    Returns:
        YAML string representation.
    """
    result: str = yaml.safe_dump(
        config.model_dump(mode="json"),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return result


def parse_config_manifest(yaml_str: str) -> ConfigManifest:
    """Parse a YAML string into a ConfigManifest.

    Args:
        yaml_str: YAML content representing a configuration manifest.

    Returns:
        Validated ConfigManifest instance.

    Raises:
        yaml.YAMLError: If the YAML is malformed.
        pydantic.ValidationError: If the parsed data fails schema validation.
    """
    data = yaml.safe_load(yaml_str)
    return ConfigManifest.model_validate(data)


def serialize_config_manifest(manifest: ConfigManifest) -> str:
    """Serialize a ConfigManifest to a YAML string.

    Uses ``sort_keys=False`` to preserve field definition order for
    deterministic, human-readable output.

    Args:
        manifest: ConfigManifest instance to serialize.

    Returns:
        YAML string representation.
    """
    result: str = yaml.safe_dump(
        manifest.model_dump(mode="json"),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return result


# ---------------------------------------------------------------------------
# Example YAML constants
# ---------------------------------------------------------------------------


EXAMPLE_STATION_CONFIG_YAML = """\
station_id: ST-001
station_name: Final Test Station A
location: acme.shanghai.plant_a.final_test.station_01
capabilities:
  - rf_test
  - power_test
  - functional_test
assigned_products:
  - prod_comm_module_v2
  - prod_server_board_x1
instrument_configs:
  - instrument_id: DMM-01
    instrument_type: digital_multimeter
    address: TCPIP::192.168.1.10::inst0::INSTR
    calibration_due: '2025-12-31'
  - instrument_id: OSC-01
    instrument_type: oscilloscope
    address: TCPIP::192.168.1.11::inst0::INSTR
    calibration_due: null
test_limits_version: limits_v2.1.0
"""


EXAMPLE_CONFIG_MANIFEST_YAML = """\
version: '1.0.0'
stations:
  - station_id: ST-001
    station_name: Final Test Station A
    location: acme.shanghai.plant_a.final_test.station_01
    capabilities:
      - rf_test
      - power_test
    assigned_products:
      - prod_comm_module_v2
    instrument_configs:
      - instrument_id: DMM-01
        instrument_type: digital_multimeter
        address: TCPIP::192.168.1.10::inst0::INSTR
        calibration_due: '2025-12-31'
    test_limits_version: limits_v2.1.0
  - station_id: ST-002
    station_name: Burn-in Station B
    location: acme.shanghai.plant_a.burn_in.station_02
    capabilities:
      - thermal_cycle
      - stress_test
    assigned_products:
      - prod_server_board_x1
    instrument_configs: []
    test_limits_version: null
"""
