"""Executions API endpoints for CRUD operations and SSE streaming.

This module provides REST API endpoints for execution management:
- POST /api/v1/executions - Start a new execution (creates PENDING record)
- GET /api/v1/executions - List executions with pagination
- POST /api/v1/executions/search - Advanced search with filters
- GET /api/v1/executions/{run_id} - Get execution status
- POST /api/v1/executions/{run_id}/abort - Abort a running execution
- GET /api/v1/executions/{run_id}/events - SSE stream for real-time events
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from ate_cloud.db import get_db
from ate_cloud.models.execution import Execution
from ate_cloud.nats.sse_bridge import SSEBridge
from ate_cloud.schemas.execution import (
    ExecutionAbortResponse,
    ExecutionCreate,
    ExecutionListItem,
    ExecutionResponse,
    ExecutionSearchRequest,
    ExecutionSearchResponse,
)

router = APIRouter(prefix="/executions", tags=["executions"])

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


def get_sse_bridge(request: Request) -> SSEBridge:
    """Get the SSEBridge instance from app state.

    Args:
        request: The incoming FastAPI request.

    Returns:
        SSEBridge instance attached to app.state.
    """
    return request.app.state.sse_bridge


@router.get("/{run_id}/events", response_class=EventSourceResponse)
async def stream_execution_events(
    run_id: str,
    request: Request,
    bridge: SSEBridge = Depends(get_sse_bridge),
) -> EventSourceResponse:
    """SSE endpoint for streaming execution events.

    Supports Last-Event-ID header for resumption — replays missed events
    from JetStream when NATS is available.

    Multi-client safe: reference counting ensures the queue is only
    removed when the last client disconnects.

    Args:
        run_id: The execution run identifier.
        request: The incoming HTTP request (used for disconnect detection).
        bridge: The SSEBridge instance.

    Returns:
        EventSourceResponse streaming ServerSentEvent objects.
    """
    queue = bridge.get_or_create_queue(run_id)

    # Start NATS subscription if available
    await bridge.start_subscription(run_id)

    async def event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        heartbeat_interval = 15.0  # seconds

        try:
            # Phase 1: Replay from JetStream if Last-Event-ID provided
            last_id = request.headers.get("Last-Event-ID")
            if last_id and bridge.nats_available:
                async for event in bridge.replay_from_jetstream(run_id, last_id):
                    yield ServerSentEvent(
                        data=json.dumps(event.get("data", {})),
                        event=event.get("category", "event"),
                        id=event.get("id"),
                    )

            # Phase 2: Stream live events from queue with keep-alive
            if bridge.nats_available:
                # NATS mode: use queue-based streaming with timeout
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=heartbeat_interval
                        )
                        yield ServerSentEvent(
                            data=json.dumps(event.get("data", {})),
                            event=event.get("category", "event"),
                            id=event.get("id"),
                        )
                    except asyncio.TimeoutError:
                        # Send keep-alive comment to prevent connection timeout
                        yield ServerSentEvent(data="", comment="keep-alive")
            else:
                # Local mode: race between queue events and heartbeat
                heartbeat_task = asyncio.create_task(asyncio.sleep(0))
                try:
                    while True:
                        if await request.is_disconnected():
                            break
                        # Race: next queue event vs heartbeat timer
                        heartbeat_task = asyncio.create_task(
                            asyncio.sleep(heartbeat_interval)
                        )
                        queue_task = asyncio.create_task(queue.get())
                        done, _ = await asyncio.wait(
                            [heartbeat_task, queue_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if heartbeat_task in done:
                            queue_task.cancel()
                            try:
                                await queue_task
                            except asyncio.CancelledError:
                                pass
                            yield ServerSentEvent(data="", comment="keep-alive")
                        else:
                            heartbeat_task.cancel()
                            try:
                                await heartbeat_task
                            except asyncio.CancelledError:
                                pass
                            event = queue_task.result()
                            yield ServerSentEvent(
                                data=json.dumps(event.get("data", {})),
                                event=event.get("category", "event"),
                                id=event.get("id"),
                            )
                finally:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass
        finally:
            # Cleanup when client disconnects (refcount-aware)
            bridge.remove_queue(run_id)

    return EventSourceResponse(event_generator())


@router.post("", response_model=ExecutionResponse, status_code=status.HTTP_201_CREATED)
async def create_execution(
    execution_data: ExecutionCreate,
    db: AsyncSession = Depends(get_db),
    bridge: SSEBridge = Depends(get_sse_bridge),
) -> ExecutionResponse:
    """Start a new execution.

    Creates an Execution record with PENDING status and publishes
    an EXECUTION_STARTED event via the SSE bridge.

    Note: Actual execution dispatch is NOT done here — that's Phase 3 (LoopExecutor).
    This endpoint creates the execution record and makes it available for SSE streaming.

    Args:
        execution_data: The execution creation data (sequence_id required).
        db: Database session.
        bridge: SSEBridge instance.

    Returns:
        ExecutionResponse: The created execution with generated run_id.
    """
    run_id = str(uuid.uuid4())
    execution = Execution(
        id=run_id,
        sequence_id=execution_data.sequence_id,
        status="PENDING",
        config=execution_data.config,
    )

    db.add(execution)
    await db.commit()
    await db.refresh(execution)

    # Publish EXECUTION_STARTED event via bridge
    await bridge.publish_event(
        run_id=run_id,
        event_type="EXECUTION_STARTED",
        data={"run_id": run_id, "sequence_id": execution_data.sequence_id, "status": "PENDING"},
    )

    return execution


def _extract_product_type(config: dict[str, Any] | None) -> str | None:
    """Extract product_type from execution config JSON.

    Args:
        config: Execution config dict (may be None).

    Returns:
        product_type string or None.
    """
    if config is None:
        return None
    val = config.get("product_type")
    if isinstance(val, str):
        return val
    return None


def _extract_pass_rate(result: dict[str, Any] | None) -> float | None:
    """Extract pass_rate from execution result JSON.

    Args:
        result: Execution result dict (may be None).

    Returns:
        pass_rate float or None.
    """
    if result is None:
        return None
    val = result.get("pass_rate")
    if isinstance(val, (int, float)):
        return float(val)
    return None


def _to_list_item(execution: Execution) -> ExecutionListItem:
    """Convert an Execution ORM row to a compact ExecutionListItem.

    Args:
        execution: SQLAlchemy Execution model instance.

    Returns:
        ExecutionListItem with flattened fields for table display.
    """
    return ExecutionListItem(
        id=execution.id,
        sequence_id=execution.sequence_id,
        status=execution.status,
        dut_serial=execution.dut_serial,
        product_type=_extract_product_type(execution.config),
        started_at=execution.started_at,
        completed_at=execution.completed_at,
        pass_rate=_extract_pass_rate(execution.result),
        error=execution.error,
    )


@router.get("", response_model=ExecutionSearchResponse)
async def list_executions(
    db: DBSession,
    skip: int = 0,
    limit: int = 50,
) -> ExecutionSearchResponse:
    """List executions with pagination, ordered by created_at DESC.

    Args:
        db: Database session.
        skip: Number of records to skip.
        limit: Maximum number of records to return.

    Returns:
        ExecutionSearchResponse with items and total count.
    """
    limit = min(max(limit, 1), 500)
    skip = max(skip, 0)

    count_result = await db.execute(select(func.count()).select_from(Execution))
    total: int = count_result.scalar() or 0

    result = await db.execute(
        select(Execution)
        .order_by(Execution.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    executions = result.scalars().all()

    return ExecutionSearchResponse(
        items=[_to_list_item(e) for e in executions],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.post("/search", response_model=ExecutionSearchResponse)
async def search_executions(
    search_data: ExecutionSearchRequest,
    db: DBSession,
) -> ExecutionSearchResponse:
    """Search executions with advanced filters.

    Supports filtering by:
    - serial_number: Partial match on dut_serial (case-insensitive).
    - product_type: Exact match on config->>'product_type'.
    - status: Exact match on execution status.
    - date_from / date_to: Range filter on started_at.

    Multiple filters are combined with AND logic.
    Results are ordered by created_at DESC with pagination.

    Args:
        search_data: Search request with optional filters.
        db: Database session.

    Returns:
        ExecutionSearchResponse with matching items and total count.
    """
    query = select(Execution)
    count_query = select(func.count()).select_from(Execution)

    conditions = []

    if search_data.serial_number:
        conditions.append(
            Execution.dut_serial.ilike(f"%{search_data.serial_number}%")
        )

    if search_data.product_type:
        # Filter by product_type stored in config JSON.
        # SQLite JSON: config->>'$.product_type'; PostgreSQL: config->>'product_type'
        # Using ilike on cast for cross-database compatibility.
        conditions.append(
            func.cast(Execution.config, str).ilike(
                f'%"product_type": "{search_data.product_type}"%'
            )
        )

    if search_data.status:
        conditions.append(Execution.status == search_data.status)

    if search_data.date_from:
        conditions.append(Execution.started_at >= search_data.date_from)

    if search_data.date_to:
        conditions.append(Execution.started_at <= search_data.date_to)

    if conditions:
        combined = conditions[0]
        for cond in conditions[1:]:
            combined = combined & cond
        query = query.where(combined)
        count_query = count_query.where(combined)

    # Count total matching
    count_result = await db.execute(count_query)
    total: int = count_result.scalar() or 0

    # Fetch page
    result = await db.execute(
        query
        .order_by(Execution.created_at.desc())
        .offset(search_data.skip)
        .limit(search_data.limit)
    )
    executions = result.scalars().all()

    return ExecutionSearchResponse(
        items=[_to_list_item(e) for e in executions],
        total=total,
        skip=search_data.skip,
        limit=search_data.limit,
    )


@router.get("/{run_id}", response_model=ExecutionResponse)
async def get_execution(
    run_id: str,
    db: AsyncSession = Depends(get_db),
) -> ExecutionResponse:
    """Get execution status by run_id.

    Args:
        run_id: The execution run identifier.
        db: Database session.

    Returns:
        ExecutionResponse: The execution data.

    Raises:
        HTTPException: 404 if execution not found.
    """
    result = await db.execute(select(Execution).where(Execution.id == run_id))
    execution = result.scalar_one_or_none()

    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    return execution


@router.post("/{run_id}/abort", response_model=ExecutionAbortResponse)
async def abort_execution(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    bridge: SSEBridge = Depends(get_sse_bridge),
) -> ExecutionAbortResponse:
    """Abort a running execution.

    Updates the execution status to ABORTED and publishes an
    EXECUTION_COMPLETED event with status=ABORTED.

    Args:
        run_id: The execution run identifier.
        db: Database session.
        bridge: SSEBridge instance.

    Returns:
        ExecutionAbortResponse: Confirmation with ABORTING status.

    Raises:
        HTTPException: 404 if execution not found.
        HTTPException: 409 if execution is already in a terminal state.
    """
    result = await db.execute(select(Execution).where(Execution.id == run_id))
    execution = result.scalar_one_or_none()

    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    terminal_states = {"COMPLETED", "FAILED", "ABORTED"}
    if execution.status in terminal_states:
        raise HTTPException(
            status_code=409,
            detail=f"Execution is already in terminal state: {execution.status}",
        )

    execution.status = "ABORTED"
    await db.commit()

    # Publish EXECUTION_COMPLETED event with ABORTED status
    await bridge.publish_event(
        run_id=run_id,
        event_type="EXECUTION_COMPLETED",
        data={"run_id": run_id, "status": "ABORTED"},
    )

    return ExecutionAbortResponse(id=run_id, status="ABORTING")
