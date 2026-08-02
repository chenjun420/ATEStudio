"""Operator checkpoint API endpoints.

Provides:
- ``POST /api/v1/executions/{run_id}/checkpoint`` - Submit the operator's
  response to a pending checkpoint. Resumes the executor.
- ``GET /api/v1/executions/{run_id}/checkpoint/pending`` - Get the
  currently pending checkpoint for a run (or ``pending=False``).

Pending checkpoints are tracked by the :class:`CheckpointHandler`
attached to ``app.state.checkpoint_handlers`` (a ``dict[run_id, handler]``).
This mirrors the ``app.state.recorders`` pattern used by the recordings
API. When the executor (running in the same process, as in dev mode, or
reachable via the shared handler registry) reaches a step with an
``operator_checkpoint``, it registers a pending checkpoint on the handler
and blocks. The operator UI polls ``GET .../pending`` (or listens on the
SSE bridge for ``OPERATOR_CHECKPOINT`` events) to discover the prompt,
then submits a response via ``POST .../checkpoint``.

Per AGENTS.md §7: no silent degradation. If no handler is registered for
a run, the submit endpoint returns 404 and the pending endpoint returns
``pending=False`` (an empty state is a valid answer, not an error).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.execution import Execution
from ate_cloud.nats.sse_bridge import SSEBridge
from ate_cloud.schemas.operator_checkpoint import (
    OperatorCheckpointRequest,
    OperatorCheckpointResponse,
)
from ate_platform.executor.checkpoint_handler import CheckpointHandler

router = APIRouter(prefix="/executions", tags=["operator_checkpoints"])

# Type aliases for dependency injection (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


def _get_sse_bridge(request: Request) -> SSEBridge:
    """Get the SSEBridge instance from app state.

    The bridge is attached to ``app.state.sse_bridge`` by the cloud
    conftest (tests) or by ``main.py`` lifespan (production).

    Args:
        request: The incoming FastAPI request.

    Returns:
        The SSEBridge instance.
    """
    bridge: SSEBridge = request.app.state.sse_bridge
    return bridge


SSEBridgeDep = Annotated[SSEBridge, Depends(_get_sse_bridge)]


def _get_handler_registry(request: Request) -> dict[str, CheckpointHandler]:
    """Get (or lazily create) the per-run CheckpointHandler registry.

    Stored on ``app.state.checkpoint_handlers`` as a ``dict[run_id, handler]``.
    The executor registers its handler here when it starts processing a
    run, so the API endpoints can reach the same in-process handler.

    Args:
        request: The incoming FastAPI request.

    Returns:
        The mutable registry dict.
    """
    registry: dict[str, CheckpointHandler] = getattr(
        request.app.state, "checkpoint_handlers", {}
    )
    if not hasattr(request.app.state, "checkpoint_handlers"):
        request.app.state.checkpoint_handlers = registry
    return registry


def _get_handler(request: Request, run_id: str) -> CheckpointHandler:
    """Look up the CheckpointHandler for ``run_id``.

    Args:
        request: The incoming FastAPI request.
        run_id: The execution run identifier.

    Raises:
        HTTPException: 404 if no handler is registered for the run.
    """
    registry = _get_handler_registry(request)
    handler = registry.get(run_id)
    if handler is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active checkpoint handler for execution '{run_id}'",
        )
    return handler


async def _verify_execution_exists(
    run_id: str,
    db: AsyncSession,
) -> Execution:
    """Verify the execution exists and return it.

    Raises:
        HTTPException: 404 if execution not found.
    """
    result = await db.execute(select(Execution).where(Execution.id == run_id))
    execution = result.scalar_one_or_none()
    if execution is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Execution '{run_id}' not found",
        )
    return execution


@router.get(
    "/{run_id}/checkpoint/pending",
    response_model=OperatorCheckpointResponse,
    status_code=status.HTTP_200_OK,
)
async def get_pending_checkpoint(
    run_id: str,
    request: Request,
    db: DBSession,
) -> OperatorCheckpointResponse:
    """GET /api/v1/executions/{run_id}/checkpoint/pending - Get pending checkpoint.

    Returns the currently pending operator checkpoint for a run, if any.
    The operator UI polls this endpoint (or listens on the SSE bridge
    for ``OPERATOR_CHECKPOINT`` events) to discover the prompt and
    render the appropriate modal dialog.

    Args:
        run_id: The execution run identifier.
        request: The HTTP request (for app state access).
        db: Database session.

    Returns:
        OperatorCheckpointResponse with ``pending=True`` and the
        checkpoint definition if a checkpoint is awaiting a response,
        or ``pending=False`` otherwise.
    """
    await _verify_execution_exists(run_id, db)

    # No handler registered -> no pending checkpoint (empty state).
    registry = _get_handler_registry(request)
    handler = registry.get(run_id)
    if handler is None:
        return OperatorCheckpointResponse(run_id=run_id, pending=False)

    pending = handler.get_pending(run_id)
    if pending is None:
        return OperatorCheckpointResponse(run_id=run_id, pending=False)

    return OperatorCheckpointResponse(
        run_id=run_id,
        pending=True,
        step_id=pending.step_id,
        checkpoint=pending.checkpoint,
        created_at=pending.created_at,
    )


@router.post(
    "/{run_id}/checkpoint",
    response_model=OperatorCheckpointResponse,
    status_code=status.HTTP_200_OK,
)
async def submit_checkpoint_response(
    run_id: str,
    body: OperatorCheckpointRequest,
    request: Request,
    db: DBSession,
    bridge: SSEBridgeDep,
) -> OperatorCheckpointResponse:
    """POST /api/v1/executions/{run_id}/checkpoint - Submit operator response.

    Submits the operator's response to a pending checkpoint and resumes
    the executor. For ``visual_check`` with ``response == "fail"`` the
    executor will fail the step with the optional ``reason`` as the
    error message; for all other cases the response is treated as an
    acknowledgement and the step proceeds.

    Publishes an ``OPERATOR_CHECKPOINT_RESOLVED`` event on the SSE bridge
    so other UI clients (e.g. a dashboard) can dismiss the modal.

    Args:
        run_id: The execution run identifier.
        body: The request body (step_id, response, reason, extra).
        request: The HTTP request (for app state access).
        db: Database session.
        bridge: SSEBridge instance.

    Returns:
        OperatorCheckpointResponse describing the (now resolved)
        checkpoint. ``pending`` will be ``False`` after submission.

    Raises:
        HTTPException: 404 if execution or handler not found.
        HTTPException: 409 if no checkpoint is pending for the step.
    """
    await _verify_execution_exists(run_id, db)
    handler = _get_handler(request, run_id)

    # Validate that a checkpoint is pending for this step.
    pending = handler.get_pending(run_id)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No pending checkpoint for execution '{run_id}'",
        )
    if pending.step_id != body.step_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Pending checkpoint step_id '{pending.step_id}' does not "
                f"match submitted step_id '{body.step_id}'"
            ),
        )

    submitted = handler.submit_response(
        run_id=run_id,
        step_id=body.step_id,
        response=body.response,
        reason=body.reason,
        extra=body.extra,
    )
    if not submitted:
        # Race: the checkpoint was resolved/cancelled between our
        # get_pending and submit_response calls.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Checkpoint for step '{body.step_id}' was already resolved "
                f"or cancelled"
            ),
        )

    # Notify other UI clients (dashboard) that the checkpoint is resolved.
    await bridge.publish_event(
        run_id=run_id,
        event_type="OPERATOR_CHECKPOINT_RESOLVED",
        data={
            "run_id": run_id,
            "step_id": body.step_id,
            "response": body.response,
            "reason": body.reason,
        },
    )

    return OperatorCheckpointResponse(
        run_id=run_id,
        pending=False,
        step_id=body.step_id,
        checkpoint=pending.checkpoint,
    )
