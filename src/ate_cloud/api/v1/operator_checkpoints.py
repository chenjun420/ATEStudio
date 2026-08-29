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

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.db import get_db
from ate_cloud.models.execution import Execution
from ate_cloud.nats.sse_bridge import SSEBridge
from ate_cloud.schemas.operator_checkpoint import (
    OperatorCheckpointAckRequest,
    OperatorCheckpointAckResponse,
    OperatorCheckpointIdAckRequest,
    OperatorCheckpointRequest,
    OperatorCheckpointResponse,
    OperatorInteractionEvent,
)
from ate_platform.executor.checkpoint_handler import CheckpointHandler, PendingCheckpoint

router = APIRouter(prefix="/executions", tags=["operator_checkpoints"])

# RH-6 design-doc alias: POST /api/v1/checkpoints/{checkpoint_id}/ack.
checkpoint_alias_router = APIRouter(prefix="/checkpoints", tags=["operator_checkpoints"])

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


def _get_checkpoint_index(
    request: Request,
) -> tuple[dict[str, tuple[str, str]], dict[tuple[str, str], str]]:
    """Get (or lazily create) the checkpoint_id <-> (run_id, step_id) indexes.

    RH-6: a stable ``uuid4().hex`` id is assigned to each pending checkpoint
    the first time the cloud API observes it (via GET .../pending or the
    ``OPERATOR_CHECKPOINT`` SSE event). The id is stored on
    ``app.state.checkpoint_index``:

    - ``id -> (run_id, step_id)`` lets the alias endpoint
      ``POST /checkpoints/{checkpoint_id}/ack`` resolve the target.
    - ``(run_id, step_id) -> id`` keeps the assignment idempotent/stable
      across repeated GET .../pending observations (one id per pending
      checkpoint, not one per poll).

    Args:
        request: The incoming FastAPI request.

    Returns:
        A tuple of the two mutually-consistent index dicts (mutable; the
        caller updates both when assigning a new id).
    """
    by_id: dict[str, tuple[str, str]] = getattr(
        request.app.state, "checkpoint_index", {}
    )
    by_key: dict[tuple[str, str], str] = getattr(
        request.app.state, "checkpoint_index_by_key", {}
    )
    if not hasattr(request.app.state, "checkpoint_index"):
        request.app.state.checkpoint_index = by_id
        request.app.state.checkpoint_index_by_key = by_key
    return by_id, by_key


async def _observe_pending(
    request: Request,
    run_id: str,
    pending: PendingCheckpoint,
    bridge: SSEBridge,
) -> str:
    """Assign (or reuse) the stable checkpoint id for a pending checkpoint.

    On first observation of ``(run_id, step_id)`` a fresh ``uuid4().hex``
    is minted, stored in both indexes, and an ``OPERATOR_CHECKPOINT`` SSE
    event carrying the id is published (awaited, so the event is queued
    before the response returns) so the operator UI can open the modal
    and later ack by id. Repeated observations return the same id and do
    NOT republish (one event per pending checkpoint).

    Args:
        request: The incoming FastAPI request (for app state access).
        run_id: The execution run identifier.
        pending: The pending checkpoint observed via the handler.
        bridge: The SSEBridge used to publish the pending event.

    Returns:
        The stable checkpoint id (uuid4 hex string).
    """
    by_id, by_key = _get_checkpoint_index(request)
    key = (run_id, pending.step_id)
    checkpoint_id = by_key.get(key)
    if checkpoint_id is None:
        checkpoint_id = uuid.uuid4().hex
        by_id[checkpoint_id] = key
        by_key[key] = checkpoint_id
        event = OperatorInteractionEvent(
            run_id=run_id,
            step_id=pending.step_id,
            checkpoint=pending.checkpoint,
            created_at=pending.created_at,
            checkpoint_id=checkpoint_id,
        )
        await bridge.publish_event(
            run_id=run_id,
            event_type="OPERATOR_CHECKPOINT",
            data=event.model_dump(mode="json"),
        )
    return checkpoint_id


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
    bridge: SSEBridgeDep,
) -> OperatorCheckpointResponse:
    """GET /api/v1/executions/{run_id}/checkpoint/pending - Get pending checkpoint.

    Returns the currently pending operator checkpoint for a run, if any.
    The operator UI polls this endpoint (or listens on the SSE bridge
    for ``OPERATOR_CHECKPOINT`` events) to discover the prompt and
    render the appropriate modal dialog.

    RH-6: on first observation of a pending checkpoint a stable
    ``checkpoint_id`` (uuid4 hex) is assigned and published on the
    ``OPERATOR_CHECKPOINT`` SSE event; the id is stable across repeated
    polls and resolves via ``POST /checkpoints/{checkpoint_id}/ack``.

    Args:
        run_id: The execution run identifier.
        request: The HTTP request (for app state access).
        db: Database session.
        bridge: SSEBridge instance (publishes the pending event).

    Returns:
        OperatorCheckpointResponse with ``pending=True`` and the
        checkpoint definition (plus ``checkpoint_id``) if a checkpoint
        is awaiting a response, or ``pending=False`` otherwise.
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

    checkpoint_id = await _observe_pending(request, run_id, pending, bridge)

    return OperatorCheckpointResponse(
        run_id=run_id,
        pending=True,
        step_id=pending.step_id,
        checkpoint=pending.checkpoint,
        created_at=pending.created_at,
        checkpoint_id=checkpoint_id,
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


async def _ack_checkpoint(
    request: Request,
    db: AsyncSession,
    bridge: SSEBridge,
    run_id: str,
    step_id: str,
    operator: str,
    note: str | None,
) -> OperatorCheckpointAckResponse:
    """Shared operator-ack logic used by both the T42 path and the RH-6 alias.

    Records *who* acknowledged (``operator``) plus an optional ``note``,
    submits ``response="ok"`` to the pending checkpoint, and resumes the
    executor. The operator identity and note are carried in the
    checkpoint response ``extra`` bag and in the published
    ``OPERATOR_CHECKPOINT_RESOLVED`` SSE event so both routes emit the
    exact same payload.

    Gating is enforced server-side: acking requires an actually-pending
    checkpoint for ``run_id`` (409 otherwise), so the flow cannot be
    bypassed by the UI.

    Args:
        request: The HTTP request (for app state access).
        db: Database session.
        bridge: SSEBridge instance.
        run_id: The execution run identifier.
        step_id: The step identifier whose checkpoint is being acked.
        operator: Operator name/ID performing the acknowledgement.
        note: Optional free-form note recorded with the acknowledgement.

    Returns:
        OperatorCheckpointAckResponse with the acknowledgement metadata.

    Raises:
        HTTPException: 404 if execution or handler not found.
        HTTPException: 409 if no checkpoint is pending for the run, or the
            checkpoint was already resolved/cancelled in a race.
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
    if pending.step_id != step_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Pending checkpoint step_id '{pending.step_id}' does not "
                f"match submitted step_id '{step_id}'"
            ),
        )

    submitted = handler.submit_response(
        run_id=run_id,
        step_id=step_id,
        response="ok",
        reason=note,
        extra={"operator": operator, "note": note},
    )
    if not submitted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Checkpoint for step '{step_id}' was already resolved "
                f"or cancelled"
            ),
        )

    # Notify other UI clients (dashboard) that the checkpoint is resolved.
    await bridge.publish_event(
        run_id=run_id,
        event_type="OPERATOR_CHECKPOINT_RESOLVED",
        data={
            "run_id": run_id,
            "step_id": step_id,
            "response": "ok",
            "reason": note,
            "operator": operator,
            "note": note,
        },
    )

    return OperatorCheckpointAckResponse(
        run_id=run_id,
        step_id=step_id,
        operator=operator,
        note=note,
    )


@router.post(
    "/{run_id}/checkpoint/ack",
    response_model=OperatorCheckpointAckResponse,
    status_code=status.HTTP_200_OK,
)
async def acknowledge_checkpoint(
    run_id: str,
    body: OperatorCheckpointAckRequest,
    request: Request,
    db: DBSession,
    bridge: SSEBridgeDep,
) -> OperatorCheckpointAckResponse:
    """POST /api/v1/executions/{run_id}/checkpoint/ack - Operator acknowledgement.

    T42 operator-console flavoured submission: records *who* acknowledged
    (``operator``) plus an optional ``note``, submits ``response="ok"``
    to the pending checkpoint, and resumes the executor. Delegates to
    :func:`_ack_checkpoint`, which the RH-6 alias path also uses, so the
    response shape and ``OPERATOR_CHECKPOINT_RESOLVED`` SSE payload are
    identical across both routes.

    Args:
        run_id: The execution run identifier.
        body: The acknowledgement body (step_id, operator, note).
        request: The HTTP request (for app state access).
        db: Database session.
        bridge: SSEBridge instance.

    Returns:
        OperatorCheckpointAckResponse with the acknowledgement metadata.

    Raises:
        HTTPException: 404 if execution or handler not found.
        HTTPException: 409 if no checkpoint is pending for the step.
        HTTPException: 422 if operator name is empty (schema-level).
    """
    return await _ack_checkpoint(
        request=request,
        db=db,
        bridge=bridge,
        run_id=run_id,
        step_id=body.step_id,
        operator=body.operator,
        note=body.note,
    )


@checkpoint_alias_router.post(
    "/{checkpoint_id}/ack",
    response_model=OperatorCheckpointAckResponse,
    status_code=status.HTTP_200_OK,
)
async def acknowledge_checkpoint_by_id(
    checkpoint_id: str,
    body: OperatorCheckpointIdAckRequest,
    request: Request,
    db: DBSession,
    bridge: SSEBridgeDep,
) -> OperatorCheckpointAckResponse:
    """POST /api/v1/checkpoints/{checkpoint_id}/ack - Ack by stable id (RH-6).

    Design-doc path alias for the T42 ack endpoint. The
    ``checkpoint_id`` is the stable uuid assigned by the cloud API when
    it first observed the pending checkpoint (GET .../pending /
    ``OPERATOR_CHECKPOINT`` SSE event). The id resolves to
    ``(run_id, step_id)`` via ``app.state.checkpoint_index``; this route
    then delegates to the same :func:`_ack_checkpoint` logic the old
    path uses, so response shape and SSE payload are identical.

    Args:
        checkpoint_id: The stable checkpoint id (uuid4 hex).
        body: The acknowledgement body (operator, note -- no step_id).
        request: The HTTP request (for app state access).
        db: Database session.
        bridge: SSEBridge instance.

    Returns:
        OperatorCheckpointAckResponse with the acknowledgement metadata.

    Raises:
        HTTPException: 404 if the checkpoint id is unknown to the registry.
        HTTPException: 409 if the checkpoint was already acked/resolved.
        HTTPException: 422 if operator name is empty (schema-level).
    """
    by_id, _ = _get_checkpoint_index(request)
    target = by_id.get(checkpoint_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown checkpoint id '{checkpoint_id}'",
        )
    run_id, step_id = target

    return await _ack_checkpoint(
        request=request,
        db=db,
        bridge=bridge,
        run_id=run_id,
        step_id=step_id,
        operator=body.operator,
        note=body.note,
    )
