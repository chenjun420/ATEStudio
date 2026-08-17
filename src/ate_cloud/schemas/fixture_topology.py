"""Pydantic schemas for fixture topology resources.

设计文档 §9.2 工装拓扑 API 与 §8.3.2 数据模型。

- FixtureTopologyCreate: 创建工装配置（topology_data 为完整拓扑 JSON）
- FixtureTopologyUpdate: 部分更新（全字段可选）
- FixtureTopologyResponse: API 响应（含系统管理字段）
- FixtureVersionResponse: 版本历史条目
- FixtureDeviceTemplateCreate/Response: 设备模板库
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from shared.fixture_topology import FixtureTopology as SharedFixtureTopology


class FixtureTopologyCreate(BaseModel):
    """Schema for creating a new fixture topology.

    Attributes:
        name: 拓扑名称（必填）。
        version: 版本号（默认 "1.0"）。
        description: 描述。
        product_model: 适配产品型号。
        topology_data: 完整拓扑 JSON（instruments/fixtures/duts/links/routes）。
        created_by: 创建人。
        tags: 标签。
    """

    name: str = Field(..., min_length=1, max_length=200)
    version: str = Field("1.0", min_length=1, max_length=50)
    description: str | None = None
    product_model: str | None = Field(None, max_length=100)
    topology_data: dict[str, Any]
    created_by: str | None = Field(None, max_length=100)
    tags: list[str] = Field(default_factory=list)

    @field_validator("topology_data")
    @classmethod
    def validate_topology_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        """用共享 FixtureTopology 模型严格校验拓扑数据。"""
        SharedFixtureTopology.model_validate(value)
        return value


class FixtureTopologyUpdate(BaseModel):
    """Schema for updating an existing fixture topology.

    All fields optional for partial updates. The ``version`` field allows
    explicit version bumping; when omitted the service auto-increments.
    """

    name: str | None = Field(None, min_length=1, max_length=200)
    version: str | None = Field(None, min_length=1, max_length=50)
    description: str | None = None
    product_model: str | None = Field(None, max_length=100)
    topology_data: dict[str, Any] | None = None
    tags: list[str] | None = None

    @field_validator("topology_data")
    @classmethod
    def validate_topology_data(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return value
        SharedFixtureTopology.model_validate(value)
        return value


class FixtureTopologyResponse(BaseModel):
    """Schema for fixture topology API responses."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    version: str
    description: str | None = None
    product_model: str | None = None
    topology_data: dict[str, Any]
    created_by: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = {"from_attributes": True}


class FixtureVersionResponse(BaseModel):
    """Schema for fixture topology version history entries."""

    id: str
    topology_id: str
    version: str
    change_log: str | None = None
    topology_data: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class FixtureDeviceTemplateCreate(BaseModel):
    """Schema for creating a device template."""

    category: str = Field(..., min_length=1, max_length=50)
    type: str = Field(..., min_length=1, max_length=50)
    model: str = Field(..., min_length=1, max_length=100)
    manufacturer: str | None = Field(None, max_length=100)
    spec_data: dict[str, Any] = Field(default_factory=dict)
    icon: str | None = Field(None, max_length=50)


class FixtureDeviceTemplateResponse(BaseModel):
    """Schema for device template API responses."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    category: str
    type: str
    model: str
    manufacturer: str | None = None
    spec_data: dict[str, Any] = Field(default_factory=dict)
    icon: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = {"from_attributes": True}
