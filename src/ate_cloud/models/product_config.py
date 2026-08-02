"""ProductConfig SQLAlchemy model.

This module defines the ProductConfig database model for storing
product test configuration templates. Each product type has exactly one
config that defines which test sequence to run, what limits apply, which
instruments are needed, and what checkpoints exist.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class ProductConfig(Base):
    """ProductConfig database model.

    产品配置模板数据库模型 -- 存储产品类型的测试配置模板。
    Product configs are reference data (templates), NOT execution records.

    Attributes:
        id: Unique identifier (UUID as string).
        product_type: Unique product type identifier (e.g. 'comm_module_v2').
        test_sequence_ref: Reference to the test sequence to run.
        test_limits: JSON list of test limit references.
        instrument_assignments: JSON dict mapping instrument role to identifier.
        checkpoints: JSON list of checkpoint identifiers.
        created_at: Timestamp of creation.
        updated_at: Timestamp of last update.
    """

    __tablename__ = "product_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_type: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    test_sequence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    test_limits: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    instrument_assignments: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    checkpoints: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
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
