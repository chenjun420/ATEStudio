"""Product configuration template schema for ATE Studio.

This module defines Pydantic v2 models for product test configuration templates:
- ProductConfig: Defines which test sequence, limits, instruments, and checkpoints
  apply to a given product type.
- ProductConfigList: Wraps multiple ProductConfig entries for batch transport.

产品配置模板 -- 定义某个产品类型对应的测试序列、测试限值、仪器分配和检查点。
Product configs are reference data (templates), NOT execution records.

All models use ``extra='forbid'`` for strict validation -- unknown YAML keys
are rejected rather than silently ignored, preventing configuration drift.
"""

from __future__ import annotations

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ProductConfig",
    "ProductConfigList",
    "parse_product_config",
    "serialize_product_config",
    "parse_product_config_list",
    "serialize_product_config_list",
]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ProductConfig(BaseModel):
    """Test configuration template for a single product type.

    产品配置模板 -- 定义某个产品类型的测试参数模板。

    Attributes:
        product_type: Unique product type identifier (e.g. ``"comm_module_v2"``).
            One config per product type -- the unique constraint is enforced
            at the database layer.
        test_sequence_ref: Reference to the test sequence to run for this product
            (e.g. a sequence name or path).
        test_limits: List of test limit references (e.g. limit version tags or
            limit definition paths).
        instrument_assignments: Mapping of instrument role to instrument
            identifier (e.g. ``{"dmm": "DMM-01", "osc": "OSC-01"}``).
        checkpoints: List of checkpoint identifiers where intermediate results
            are captured or operator interaction is required.
    """

    model_config = ConfigDict(extra="forbid")

    product_type: str = Field(..., min_length=1, description="Unique product type identifier")
    test_sequence_ref: str = Field(..., min_length=1, description="Reference to the test sequence to run")
    test_limits: list[str] = Field(
        default_factory=list, description="List of test limit references"
    )
    instrument_assignments: dict[str, str] = Field(
        default_factory=dict, description="Mapping of instrument role to instrument identifier"
    )
    checkpoints: list[str] = Field(
        default_factory=list, description="List of checkpoint identifiers"
    )


class ProductConfigList(BaseModel):
    """Wrapper for a collection of ProductConfig entries.

    用于批量传输的产品配置列表。

    Attributes:
        configs: List of ProductConfig entries.
    """

    model_config = ConfigDict(extra="forbid")

    configs: list[ProductConfig] = Field(default_factory=list, description="Product configuration entries")


# ---------------------------------------------------------------------------
# Parse / serialize functions
# ---------------------------------------------------------------------------


def parse_product_config(yaml_str: str) -> ProductConfig:
    """Parse a YAML string into a ProductConfig.

    Args:
        yaml_str: YAML content representing a single product configuration.

    Returns:
        Validated ProductConfig instance.

    Raises:
        yaml.YAMLError: If the YAML is malformed.
        pydantic.ValidationError: If the parsed data fails schema validation.
    """
    data = yaml.safe_load(yaml_str)
    return ProductConfig.model_validate(data)


def serialize_product_config(config: ProductConfig) -> str:
    """Serialize a ProductConfig to a YAML string.

    Uses ``sort_keys=False`` to preserve field definition order for
    deterministic, human-readable output.

    Args:
        config: ProductConfig instance to serialize.

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


def parse_product_config_list(yaml_str: str) -> ProductConfigList:
    """Parse a YAML string into a ProductConfigList.

    Args:
        yaml_str: YAML content representing a list of product configurations.

    Returns:
        Validated ProductConfigList instance.

    Raises:
        yaml.YAMLError: If the YAML is malformed.
        pydantic.ValidationError: If the parsed data fails schema validation.
    """
    data = yaml.safe_load(yaml_str)
    return ProductConfigList.model_validate(data)


def serialize_product_config_list(config_list: ProductConfigList) -> str:
    """Serialize a ProductConfigList to a YAML string.

    Uses ``sort_keys=False`` to preserve field definition order for
    deterministic, human-readable output.

    Args:
        config_list: ProductConfigList instance to serialize.

    Returns:
        YAML string representation.
    """
    result: str = yaml.safe_dump(
        config_list.model_dump(mode="json"),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return result
