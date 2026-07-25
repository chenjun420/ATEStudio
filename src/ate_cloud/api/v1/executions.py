"""Executions API endpoints for CRUD operations and SSE streaming.

This module provides REST API endpoints for execution management:
- POST /api/v1/executions - Start a new execution (creates PENDING record)
- GET /api/v1/executions/{run_id} - Get execution status
- POST /api/v1/executions/{run_id}/abort - Abort a running execution
- GET /api/v1/executions/{run_id}/events - SSE stream for real-time events
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from ate_cloud.db import get_db
from ate_cloud.models.execution import Execution
from ate_cloud.nats.sse_bridge import SSEBridge
from ate_cloud.schemas.execution import (
    ExecutionAbortResponse,
    ExecutionCreate,
    ExecutionResponse,
)

router = APIRouter(prefix="/executions", tags=["executions"])


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
        # Phase 1: Replay from JetStream if Last-Event-ID provided
        last_id = request.headers.get("Last-Event-ID")
        if last_id and bridge.nats_available:
            async for event in bridge.replay_from_jetstream(run_id, last_id):
                yield ServerSentEvent(
                    data=json.dumps(event.get("data", {})),
                    event=event.get("type", "update"),
                    id=event.get("id"),
                )

        # Phase 2: Stream live events from queue
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield ServerSentEvent(
                    data=json.dumps(event.get("data", {})),
                    event=event.get("type", "update"),
                    id=event.get("id"),
                )
            except asyncio.TimeoutError:
                # Send keep-alive comment to prevent connection timeout
                yield ServerSentEvent(data="", comment="keep-alive")

        # Cleanup when client disconnects
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
