"""Node-flow binding API endpoints.

Provides REST endpoints for managing bindings between workers (NATS KV)
and sequences (DB), plus an execution trigger from a bound node:

- ``POST /api/v1/node-flow-bindings`` — create a binding.
- ``GET /api/v1/node-flow-bindings`` — list all bindings.
- ``GET /api/v1/node-flow-bindings/{binding_id}`` — get a single binding.
- ``PUT /api/v1/node-flow-bindings/{binding_id}`` — update a binding.
- ``DELETE /api/v1/node-flow-bindings/{binding_id}`` — delete a binding.
- ``GET /api/v1/node-flow-bindings/by-worker/{worker_id}`` — list bindings for a worker.
- ``POST /api/v1/node-flow-bindings/{binding_id}/execute`` — trigger an execution.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.execution import Execution
from ate_cloud.models.node_flow_binding import NodeFlowBinding
from ate_cloud.models.sequence import Sequence
from ate_cloud.nats.sse_bridge import SSEBridge
from ate_cloud.schemas.node_flow_binding import (
    NodeFlowBindingCreate,
    NodeFlowBindingListResponse,
    NodeFlowBindingResponse,
    NodeFlowBindingUpdate,
)
from ate_cloud.services.worker_registry import WorkerRegistryService

router = APIRouter(prefix="/node-flow-bindings", tags=["node-flow-bindings"])

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


def _get_worker_service(request: Request) -> WorkerRegistryService:
    """Dependency: extract WorkerRegistryService from app state.

    The NATS client is expected on ``app.state.nc`` (set by the lifespan).
    """
    nc = getattr(request.app.state, "nc", None)
    if nc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NATS client not available",
        )
    return WorkerRegistryService(nc)


def _get_sse_bridge(request: Request) -> SSEBridge:
    """Dependency: get the SSEBridge instance from app state."""
    bridge = getattr(request.app.state, "sse_bridge", None)
    if bridge is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SSE bridge not available",
        )
    return bridge


def _to_response(binding: NodeFlowBinding, sequence_name: str | None = None) -> NodeFlowBindingResponse:
    """Convert a NodeFlowBinding ORM row to a response schema.

    Args:
        binding: SQLAlchemy NodeFlowBinding model instance.
        sequence_name: Optional sequence name (from a joined query).

    Returns:
        NodeFlowBindingResponse with all fields populated.
    """
    return NodeFlowBindingResponse(
        id=binding.id,
        worker_id=binding.worker_id,
        sequence_id=binding.sequence_id,
        is_active=binding.is_active,
        priority=binding.priority,
        config=binding.config,
        sequence_name=sequence_name,
        created_at=binding.created_at,
        updated_at=binding.updated_at,
    )


@router.post("", response_model=NodeFlowBindingResponse, status_code=status.HTTP_201_CREATED)
async def create_binding(
    binding_data: NodeFlowBindingCreate,
    db: DBSession,
    worker_service: Annotated[WorkerRegistryService, Depends(_get_worker_service)],
) -> NodeFlowBindingResponse:
    """Create a new node-flow binding.

    Verifies the worker exists in NATS KV and the sequence exists in the DB
    before creating the binding record.

    Args:
        binding_data: The binding creation data.
        db: Database session.
        worker_service: Worker registry service for NATS KV lookup.

    Returns:
        NodeFlowBindingResponse: The created binding.

    Raises:
        HTTPException: 404 if worker or sequence not found.
        HTTPException: 502 if NATS KV is unreachable.
    """
    # Verify worker exists in NATS KV
    try:
        worker = await worker_service.get_worker(binding_data.worker_id)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worker '{binding_data.worker_id}' not found",
        )

    # Verify sequence exists in DB
    seq_result = await db.execute(
        select(Sequence).where(Sequence.id == binding_data.sequence_id)
    )
    sequence = seq_result.scalar_one_or_none()
    if sequence is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sequence '{binding_data.sequence_id}' not found",
        )

    binding = NodeFlowBinding(
        id=str(uuid.uuid4()),
        worker_id=binding_data.worker_id,
        sequence_id=binding_data.sequence_id,
        is_active=binding_data.is_active,
        priority=binding_data.priority,
        config=binding_data.config,
    )

    db.add(binding)
    await db.commit()
    await db.refresh(binding)

    return _to_response(binding, sequence_name=sequence.name)


@router.get("", response_model=NodeFlowBindingListResponse)
async def list_bindings(
    db: DBSession,
    skip: int = 0,
    limit: int = 100,
) -> NodeFlowBindingListResponse:
    """List all node-flow bindings with sequence names.

    Args:
        db: Database session.
        skip: Number of records to skip.
        limit: Maximum number of records to return.

    Returns:
        NodeFlowBindingListResponse with items and total count.
    """
    limit = min(max(limit, 1), 500)
    skip = max(skip, 0)

    count_result = await db.execute(select(func.count()).select_from(NodeFlowBinding))
    total: int = count_result.scalar() or 0

    result = await db.execute(
        select(NodeFlowBinding, Sequence.name.label("sequence_name"))
        .join(Sequence, NodeFlowBinding.sequence_id == Sequence.id, isouter=True)
        .order_by(NodeFlowBinding.priority.asc(), NodeFlowBinding.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    rows = result.all()

    items = [_to_response(binding, seq_name) for binding, seq_name in rows]
    return NodeFlowBindingListResponse(items=items, total=total)


@router.get("/by-worker/{worker_id}", response_model=NodeFlowBindingListResponse)
async def list_bindings_by_worker(
    worker_id: str,
    db: DBSession,
) -> NodeFlowBindingListResponse:
    """List all bindings for a specific worker.

    Args:
        worker_id: The worker identifier.
        db: Database session.

    Returns:
        NodeFlowBindingListResponse with items and total count.
    """
    count_result = await db.execute(
        select(func.count())
        .select_from(NodeFlowBinding)
        .where(NodeFlowBinding.worker_id == worker_id)
    )
    total: int = count_result.scalar() or 0

    result = await db.execute(
        select(NodeFlowBinding, Sequence.name.label("sequence_name"))
        .join(Sequence, NodeFlowBinding.sequence_id == Sequence.id, isouter=True)
        .where(NodeFlowBinding.worker_id == worker_id)
        .order_by(NodeFlowBinding.priority.asc(), NodeFlowBinding.created_at.desc())
    )
    rows = result.all()

    items = [_to_response(binding, seq_name) for binding, seq_name in rows]
    return NodeFlowBindingListResponse(items=items, total=total)


@router.get("/{binding_id}", response_model=NodeFlowBindingResponse)
async def get_binding(
    binding_id: str,
    db: DBSession,
) -> NodeFlowBindingResponse:
    """Get a single node-flow binding by ID.

    Args:
        binding_id: The unique binding identifier.
        db: Database session.

    Returns:
        NodeFlowBindingResponse: The binding data.

    Raises:
        HTTPException: 404 if binding not found.
    """
    result = await db.execute(
        select(NodeFlowBinding, Sequence.name.label("sequence_name"))
        .join(Sequence, NodeFlowBinding.sequence_id == Sequence.id, isouter=True)
        .where(NodeFlowBinding.id == binding_id)
    )
    row = result.first()

    if row is None:
        raise HTTPException(status_code=404, detail="Binding not found")

    binding, seq_name = row
    return _to_response(binding, seq_name)


@router.put("/{binding_id}", response_model=NodeFlowBindingResponse)
async def update_binding(
    binding_id: str,
    binding_data: NodeFlowBindingUpdate,
    db: DBSession,
) -> NodeFlowBindingResponse:
    """Update an existing node-flow binding.

    Args:
        binding_id: The unique binding identifier.
        binding_data: Partial update data (is_active, priority, config).
        db: Database session.

    Returns:
        NodeFlowBindingResponse: The updated binding.

    Raises:
        HTTPException: 404 if binding not found.
    """
    result = await db.execute(
        select(NodeFlowBinding).where(NodeFlowBinding.id == binding_id)
    )
    binding = result.scalar_one_or_none()

    if binding is None:
        raise HTTPException(status_code=404, detail="Binding not found")

    update_data = binding_data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(binding, key, value)

    await db.commit()
    await db.refresh(binding)

    # Fetch sequence name for response
    seq_result = await db.execute(
        select(Sequence.name).where(Sequence.id == binding.sequence_id)
    )
    seq_name = seq_result.scalar_one_or_none()

    return _to_response(binding, sequence_name=seq_name)


@router.delete("/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_binding(
    binding_id: str,
    db: DBSession,
) -> None:
    """Delete a node-flow binding.

    Args:
        binding_id: The unique binding identifier.
        db: Database session.

    Raises:
        HTTPException: 404 if binding not found.
    """
    result = await db.execute(
        select(NodeFlowBinding).where(NodeFlowBinding.id == binding_id)
    )
    binding = result.scalar_one_or_none()

    if binding is None:
        raise HTTPException(status_code=404, detail="Binding not found")

    await db.delete(binding)
    await db.commit()


@router.post("/{binding_id}/execute")
async def execute_binding(
    binding_id: str,
    db: DBSession,
    bridge: Annotated[SSEBridge, Depends(_get_sse_bridge)],
) -> dict[str, Any]:
    """Trigger an execution from a bound node.

    Creates an Execution record with the sequence_id from the binding,
    merges any override config from the binding, and publishes an
    EXECUTION_STARTED event via the SSE bridge.

    Args:
        binding_id: The unique binding identifier.
        db: Database session.
        bridge: SSEBridge instance for event publishing.

    Returns:
        dict: Execution ID and status.

    Raises:
        HTTPException: 404 if binding not found or binding is inactive.
    """
    result = await db.execute(
        select(NodeFlowBinding).where(NodeFlowBinding.id == binding_id)
    )
    binding = result.scalar_one_or_none()

    if binding is None:
        raise HTTPException(status_code=404, detail="Binding not found")

    if not binding.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Binding is inactive",
        )

    run_id = str(uuid.uuid4())
    execution = Execution(
        id=run_id,
        sequence_id=binding.sequence_id,
        status="PENDING",
        config=binding.config,
    )

    db.add(execution)
    await db.commit()
    await db.refresh(execution)

    # Publish EXECUTION_STARTED event via SSE bridge
    await bridge.publish_event(
        run_id=run_id,
        event_type="EXECUTION_STARTED",
        data={
            "run_id": run_id,
            "sequence_id": binding.sequence_id,
            "worker_id": binding.worker_id,
            "binding_id": binding_id,
            "status": "PENDING",
        },
    )

    return {"execution_id": run_id, "status": "PENDING"}
