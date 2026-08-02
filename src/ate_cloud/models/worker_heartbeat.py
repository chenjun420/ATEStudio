"""WorkerHeartbeat SQLAlchemy model.

Stores historical heartbeat snapshots persisted by the HealthMonitorService
background task. Each row represents a single heartbeat observation of a
JetStreamWorker at a point in time, enabling dashboard time-series display.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class WorkerHeartbeat(Base):
    """WorkerHeartbeat database model.

    工作站心跳历史记录 -- 由 HealthMonitorService 后台任务定期轮询
    ``ate-workers`` KV bucket 并持久化。每条记录代表某一时刻对某个
    worker 的心跳观测快照。

    Attributes:
        id: Unique identifier (UUID as string).
        worker_id: Worker identifier (derived from the KV key).
        hostname: Hostname of the machine running the worker.
        status: ``online`` if heartbeat was within threshold, ``offline`` otherwise.
        capabilities: JSON list of capability tags.
        current_tasks: Number of tasks being processed at heartbeat time.
        recorded_at: Timestamp of the heartbeat (from the KV entry's ``created`` field).
        created_at: Timestamp when this DB row was inserted.
    """

    __tablename__ = "worker_heartbeats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    capabilities: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    current_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
