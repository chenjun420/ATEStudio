"""Pydantic schemas for the worker registry API.

Defines response models for the worker registry endpoints:
- ``WorkerInfo`` — a single registered worker's metadata.
- ``WorkerHealthResponse`` — online/offline status for a worker.
- ``WorkerListResponse`` — paginated-style list of all registered workers.

Worker metadata is stored in the ``ate-workers`` JetStream KV bucket
(key ``workers.{worker_id}``) with a per-key TTL of 30 seconds. The KV
entry's ``created`` timestamp serves as the last heartbeat time.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class WorkerInfo(BaseModel):
    """Metadata for a single registered worker.

    Attributes:
        worker_id: Unique worker identifier (derived from the KV key).
        hostname: Hostname of the machine running the worker.
        capabilities: List of capability tags (e.g., ``["script_execution"]``).
        max_concurrent_tasks: Maximum concurrent tasks the worker accepts.
        current_tasks: Number of tasks currently being processed.
        last_heartbeat: When the worker last wrote to the KV (None if unknown).
    """

    worker_id: str
    hostname: str
    capabilities: list[str]
    max_concurrent_tasks: int
    current_tasks: int
    last_heartbeat: datetime | None = None


class WorkerHealthResponse(BaseModel):
    """Health status response for a single worker.

    Attributes:
        status: ``online`` if the worker's KV key exists (heartbeated within
            the 30s TTL), ``offline`` if the key is missing or the bucket
            does not exist, ``unknown`` reserved for future use.
        worker_info: Full worker metadata if online, ``None`` otherwise.
        last_heartbeat_timestamp: Timestamp of the last heartbeat if online.
    """

    status: str = Field(
        ...,
        description="Worker health status: online, offline, or unknown",
    )
    worker_info: WorkerInfo | None = None
    last_heartbeat_timestamp: datetime | None = None


class WorkerListResponse(BaseModel):
    """Response listing all registered workers.

    Attributes:
        workers: List of registered workers (empty if none or bucket missing).
        total: Number of workers in the list.
    """

    workers: list[WorkerInfo]
    total: int


class WorkerHeartbeatResponse(BaseModel):
    """A single heartbeat snapshot from the ``worker_heartbeats`` table.

    Attributes:
        id: Unique record identifier (UUID).
        worker_id: Worker identifier.
        hostname: Hostname of the machine running the worker.
        status: ``online`` or ``offline`` at the time of recording.
        capabilities: List of capability tags.
        current_tasks: Number of tasks being processed at heartbeat time.
        recorded_at: Timestamp of the heartbeat observation.
        created_at: Timestamp when the DB row was inserted.
    """

    model_config = {"from_attributes": True}

    id: str
    worker_id: str
    hostname: str
    status: str
    capabilities: list[str]
    current_tasks: int
    recorded_at: datetime
    created_at: datetime
