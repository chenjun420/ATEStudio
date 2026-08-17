"""FixtureTopology / FixtureVersion / FixtureDeviceTemplate SQLAlchemy models.

设计文档 §9.4.1 工装拓扑表。

- ``fixture_topologies``: 工装配置主表，topology_data 为完整拓扑 JSON
  （instruments/fixtures/duts/links/routes），``UNIQUE(name, version)``。
- ``fixture_versions``: 工装版本历史，每次保存快照。
- ``fixture_device_templates``: 设备模板库（仪器/夹具/DUT 通用模板）。

topology_data 使用 :class:`shared.fixture_topology.FixtureTopology` 校验；
本模型仅负责存储（JSON 列），语义校验由共享层/API 层执行。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class FixtureTopology(Base):
    """Fixture topology main table (design doc §9.4.1).

    Attributes:
        id: Unique identifier (UUID as string).
        name: Topology name (e.g. 'PSU 产测工装').
        version: Version string (e.g. '1.0').
        description: Human-readable description.
        product_model: Target product model (e.g. 'comm_module_v2').
        topology_data: Full topology JSON (instruments/fixtures/duts/links/routes).
        created_by: Creating user identifier.
        tags: Tag list.
        created_at: Creation timestamp.
        updated_at: Last update timestamp.
    """

    __tablename__ = "fixture_topologies"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_fixture_topologies_name_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False, default="1.0")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    topology_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class FixtureVersion(Base):
    """Fixture topology version history (design doc §9.4.1).

    Attributes:
        id: Unique identifier (UUID as string).
        topology_id: FK to fixture_topologies.id.
        version: Version string.
        change_log: Human-readable change description.
        topology_data: Full topology JSON snapshot at this version.
        created_at: Creation timestamp.
    """

    __tablename__ = "fixture_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    topology_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("fixture_topologies.id"), nullable=False, index=True
    )
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    change_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    topology_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class FixtureDeviceTemplate(Base):
    """Device template library (design doc §9.4.1).

    Templates define reusable instrument/fixture/DUT building blocks used by
    the topology editor's device palette.

    Attributes:
        id: Unique identifier (UUID as string).
        category: Template category (instrument / fixture / dut).
        type: Device type (e.g. 'psu' / 'dmm' / 'eload').
        model: Device model (e.g. 'Chroma 62012P').
        manufacturer: Device manufacturer.
        spec_data: Spec JSON (channels/terminals/definition).
        icon: Icon identifier for the editor palette.
        created_at: Creation timestamp.
    """

    __tablename__ = "fixture_device_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    manufacturer: Mapped[str | None] = mapped_column(String(100), nullable=True)
    spec_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    icon: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
