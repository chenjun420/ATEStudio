"""Recordings API endpoints - execution recording and replay control.

Provides:
- ``POST /api/v1/executions/{id}/record`` - Start recording events for
  an execution session. Creates an ExecutionRecorder bound to the
  session and attaches it to app.state for later retrieval.
- ``POST /api/v1/executions/{id}/replay`` - Start replaying recorded
  events. Reads events from JetStream and returns them in timestamp
  order with optional time acceleration.
- ``GET /api/v1/executions/{id}/recordings`` - List recorded events
  for a session (read-only, no replay delays).
- ``POST /api/v1/executions/{id}/replay/diff`` - Compute a diff between
  two event sequences (original vs replayed).

The recorder/replay executor use the ``ATE_EXECUTION_EVENTS`` JetStream
stream with subject ``ate.execution.{session_id}.events``.

Per AGENTS.md §7: if NATS is unavailable, endpoints return 503 - no
silent degradation.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from nats.aio.client import Client as NatsClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from ate_cloud.db import get_db
from ate_cloud.models.execution import Execution
from ate_cloud.schemas.recording import (
    RecordedEventResponse,
    RecordingStatusResponse,
    RecordStartRequest,
    RecordStartResponse,
    ReplayControlResponse,
    ReplayDiffEntry,
    ReplayDiffResponse,
    ReplayDiffSummary,
    ReplayResultResponse,
    ReplayStartRequest,
)
from ate_platform.recorder import ExecutionRecorder, ReplayExecutor
from ate_platform.recorder.types import RecordedEvent

router = APIRouter(prefix="/executions", tags=["recordings"])

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


def _get_nats_client(request: Request) -> NatsClient:
    """Get the NATS client from app state.

    Raises:
        HTTPException: 503 if NATS client is not available on app.state.
    """
    nc: NatsClient | None = getattr(request.app.state, "nc", None)
    if nc is None:
        # Fall back to the global NATS client (set by lifespan in main.py)
        try:
            from ate_cloud.main import get_nats

            nc = get_nats()
        except RuntimeError as e:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"NATS client not available: {e}",
            ) from e
    if nc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NATS client not available",
        )
    return nc


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


@router.post(
    "/{run_id}/record",
    response_model=RecordStartResponse,
    status_code=status.HTTP_200_OK,
)
async def start_recording(
    run_id: str,
    request: Request,
    db: DBSession,
    _body: RecordStartRequest | None = None,
) -> RecordStartResponse:
    """POST /api/v1/executions/{run_id}/record - Start recording execution events.

    Creates an :class:`ExecutionRecorder` bound to the session and starts
    its background flush task. The recorder writes JSONL events to the
    ``ate.execution.{run_id}.events`` JetStream subject.

    If recording is already active for this session, returns the existing
    recorder's status without starting a new one.

    Args:
        run_id: The execution run identifier.
        request: The HTTP request (for app state access).
        db: Database session.
        _body: Optional request body with recording options.

    Returns:
        RecordStartResponse with the session_id, subject, and status.
    """
    await _verify_execution_exists(run_id, db)
    nc = _get_nats_client(request)

    # Check if a recorder is already active for this session
    recorders: dict[str, ExecutionRecorder] = getattr(request.app.state, "recorders", {})
    if run_id in recorders and recorders[run_id].is_running:
        existing = recorders[run_id]
        return RecordStartResponse(
            session_id=existing.session_id,
            subject=existing.subject,
            status="recording",
        )

    recorder = ExecutionRecorder(session_id=run_id, nats_client=nc)
    try:
        await recorder.start()
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        ) from e

    # Store the recorder on app.state
    if not hasattr(request.app.state, "recorders"):
        request.app.state.recorders = {}
    request.app.state.recorders[run_id] = recorder

    return RecordStartResponse(
        session_id=recorder.session_id,
        subject=recorder.subject,
        status="recording",
    )


@router.post(
    "/{run_id}/replay",
    response_model=ReplayResultResponse,
    status_code=status.HTTP_200_OK,
)
async def start_replay(
    run_id: str,
    request: Request,
    body: ReplayStartRequest,
    db: DBSession,
) -> ReplayResultResponse:
    """POST /api/v1/executions/{run_id}/replay - Start replaying recorded events.

    Reads all recorded events from the ``ate.execution.{run_id}.events``
    JetStream subject, sorts them by timestamp, and returns them with
    optional time acceleration. The replay is synchronous - the response
    includes all replayed events.

    For interactive replay with UI updates, use the SSE endpoint or the
    frontend ReplayDiffViewer component.

    Args:
        run_id: The execution run identifier.
        request: The HTTP request.
        body: Replay options (speed_multiplier, max_events).
        db: Database session.

    Returns:
        ReplayResultResponse with the replayed events and timing summary.
    """
    await _verify_execution_exists(run_id, db)
    nc = _get_nats_client(request)

    executor = ReplayExecutor(session_id=run_id, nats_client=nc)
    start_time = time.monotonic()

    try:
        events = await executor.replay(speed_multiplier=body.speed_multiplier)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        ) from e

    duration = time.monotonic() - start_time

    # Apply max_events limit if specified
    total = len(events)
    if body.max_events is not None:
        events = events[: body.max_events]

    event_responses = [
        RecordedEventResponse(
            timestamp=e.timestamp,
            event_type=e.event_type,
            session_id=e.session_id,
            step_id=e.step_id,
            data=e.data,
        )
        for e in events
    ]

    return ReplayResultResponse(
        session_id=run_id,
        status="completed",
        events_replayed=len(events),
        events_total=total,
        speed_multiplier=body.speed_multiplier,
        duration_seconds=round(duration, 4),
        events=event_responses,
    )


@router.get(
    "/{run_id}/recordings",
    response_model=list[RecordedEventResponse],
    status_code=status.HTTP_200_OK,
)
async def list_recordings(
    run_id: str,
    request: Request,
    db: DBSession,
) -> list[RecordedEventResponse]:
    """GET /api/v1/executions/{run_id}/recordings - List recorded events.

    Reads all recorded events from JetStream and returns them in
    timestamp order (no replay delays). This is a read-only endpoint
    for inspecting the recorded event log.

    Args:
        run_id: The execution run identifier.
        request: The HTTP request.
        db: Database session.

    Returns:
        List of RecordedEventResponse in timestamp order.
    """
    await _verify_execution_exists(run_id, db)
    nc = _get_nats_client(request)

    executor = ReplayExecutor(session_id=run_id, nats_client=nc)
    try:
        events = await executor._load_events_from_jetstream()  # noqa: SLF001
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        ) from e

    sorted_events = ReplayExecutor._sort_events(events)  # noqa: SLF001
    return [
        RecordedEventResponse(
            timestamp=e.timestamp,
            event_type=e.event_type,
            session_id=e.session_id,
            step_id=e.step_id,
            data=e.data,
        )
        for e in sorted_events
    ]


@router.post(
    "/{run_id}/replay/diff",
    response_model=ReplayDiffResponse,
    status_code=status.HTTP_200_OK,
)
async def compute_replay_diff(
    run_id: str,
    request: Request,
    original_events: list[dict[str, Any]],
    db: DBSession,
) -> ReplayDiffResponse:
    """POST /api/v1/executions/{run_id}/replay/diff - Compute diff between sequences.

    Compares a caller-provided original event sequence with the events
    currently recorded on the JetStream stream for this session. Returns
    added/removed/changed entries for the frontend ReplayDiffViewer.

    Args:
        run_id: The execution run identifier.
        request: The HTTP request.
        original_events: The original event sequence as a list of dicts
            (JSON-compatible RecordedEvent dicts).
        db: Database session.

    Returns:
        ReplayDiffResponse with summary and entries.
    """
    await _verify_execution_exists(run_id, db)
    nc = _get_nats_client(request)

    # Parse the original events from the request body
    try:
        original = [RecordedEvent.model_validate(e) for e in original_events]
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Failed to parse original_events: {e}",
        ) from e

    # Load the current (replayed) events from JetStream
    executor = ReplayExecutor(session_id=run_id, nats_client=nc)
    try:
        replayed = await executor._load_events_from_jetstream()  # noqa: SLF001
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        ) from e

    diff = ReplayExecutor.compute_diff(original, replayed)

    entries: list[ReplayDiffEntry] = []
    for item in diff["added"]:
        entries.append(ReplayDiffEntry(
            kind="added",
            step_id=item.get("step_id", "") or "",
            event_type=item.get("event_type", ""),
            original=None,
            replayed=item,
        ))
    for item in diff["removed"]:
        entries.append(ReplayDiffEntry(
            kind="removed",
            step_id=item.get("step_id", "") or "",
            event_type=item.get("event_type", ""),
            original=item,
            replayed=None,
        ))
    for item in diff["changed"]:
        entries.append(ReplayDiffEntry(
            kind="changed",
            step_id=(item.get("original", {}) or {}).get("step_id", "") or "",
            event_type=(item.get("original", {}) or {}).get("event_type", ""),
            original=item.get("original"),
            replayed=item.get("replayed"),
        ))

    summary = diff["summary"]
    return ReplayDiffResponse(
        session_id=run_id,
        summary=ReplayDiffSummary(
            original_count=summary["original_count"],
            replayed_count=summary["replayed_count"],
            added=summary["added"],
            removed=summary["removed"],
            changed=summary["changed"],
        ),
        entries=entries,
    )


def _get_replay_executor(request: Request, run_id: str) -> ReplayExecutor:
    """Retrieve an active ReplayExecutor from app state for a session.

    Replay executors are stored on ``app.state.replay_executors`` when
    a streaming replay is started. This helper looks up the executor
    for the given run_id so control endpoints (pause/resume) can act
    on the live replay.

    Raises:
        HTTPException: 404 if no active replay executor exists for run_id.
    """
    executors: dict[str, ReplayExecutor] = getattr(
        request.app.state, "replay_executors", {}
    )
    executor = executors.get(run_id)
    if executor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active replay for execution '{run_id}'",
        )
    return executor


@router.get(
    "/{run_id}/recording",
    response_model=RecordingStatusResponse,
    status_code=status.HTTP_200_OK,
)
async def get_recording_status(
    run_id: str,
    request: Request,
    db: DBSession,
) -> RecordingStatusResponse:
    """GET /api/v1/executions/{run_id}/recording - Get recording status.

    Returns whether recording is active for the session, the number of
    events published so far, and the JetStream subject.

    Args:
        run_id: The execution run identifier.
        request: The HTTP request (for app state access).
        db: Database session.

    Returns:
        RecordingStatusResponse with session_id, is_recording, event_count.
    """
    await _verify_execution_exists(run_id, db)

    recorders: dict[str, ExecutionRecorder] = getattr(
        request.app.state, "recorders", {}
    )
    recorder = recorders.get(run_id)
    if recorder is None:
        return RecordingStatusResponse(
            session_id=run_id,
            is_recording=False,
            event_count=0,
            subject="",
        )
    return RecordingStatusResponse(
        session_id=recorder.session_id,
        is_recording=recorder.is_running,
        event_count=recorder.event_count,
        subject=recorder.subject,
    )


@router.post(
    "/{run_id}/replay/pause",
    response_model=ReplayControlResponse,
    status_code=status.HTTP_200_OK,
)
async def pause_replay(
    run_id: str,
    request: Request,
    db: DBSession,
) -> ReplayControlResponse:
    """POST /api/v1/executions/{run_id}/replay/pause - Pause an active replay.

    Pauses the streaming replay (if any) for this execution. The replay
    blocks after the current event until :meth:`resume` is called.

    Args:
        run_id: The execution run identifier.
        request: The HTTP request.
        db: Database session.

    Returns:
        ReplayControlResponse with action=pause, status=paused.

    Raises:
        HTTPException: 404 if execution or active replay not found.
    """
    await _verify_execution_exists(run_id, db)
    executor = _get_replay_executor(request, run_id)
    executor.pause()
    return ReplayControlResponse(
        session_id=run_id,
        action="pause",
        status="paused",
    )


@router.post(
    "/{run_id}/replay/resume",
    response_model=ReplayControlResponse,
    status_code=status.HTTP_200_OK,
)
async def resume_replay(
    run_id: str,
    request: Request,
    db: DBSession,
) -> ReplayControlResponse:
    """POST /api/v1/executions/{run_id}/replay/resume - Resume a paused replay.

    Resumes the streaming replay (if any) for this execution.

    Args:
        run_id: The execution run identifier.
        request: The HTTP request.
        db: Database session.

    Returns:
        ReplayControlResponse with action=resume, status=resumed.

    Raises:
        HTTPException: 404 if execution or active replay not found.
    """
    await _verify_execution_exists(run_id, db)
    executor = _get_replay_executor(request, run_id)
    executor.resume()
    return ReplayControlResponse(
        session_id=run_id,
        action="resume",
        status="resumed",
    )


@router.get("/{run_id}/replay/stream", response_class=EventSourceResponse)
async def stream_replay(
    run_id: str,
    request: Request,
    db: DBSession,
    speed: float = 1.0,
) -> EventSourceResponse:
    """GET /api/v1/executions/{run_id}/replay/stream - SSE stream of replayed events.

    Reads recorded events from JetStream, sorts by timestamp, and streams
    them as SSE events with time-accurate delays scaled by ``speed``.
    Each SSE event carries the recorded event type on the ``event:`` line
    and the full event payload (including step_id) as JSON ``data:``,
    enabling the frontend GraphContainer to highlight edges in real time.

    Supports pause/resume via the control endpoints. The ReplayExecutor
    is stored on ``app.state.replay_executors`` for the lifetime of the
    stream and removed on disconnect.

    Args:
        run_id: The execution run identifier.
        request: The HTTP request.
        db: Database session.
        speed: Time acceleration factor (query param, default 1.0).

    Returns:
        EventSourceResponse streaming replayed events as SSE.
    """
    await _verify_execution_exists(run_id, db)
    nc = _get_nats_client(request)

    executor = ReplayExecutor(session_id=run_id, nats_client=nc)

    # Register the executor so pause/resume endpoints can reach it.
    if not hasattr(request.app.state, "replay_executors"):
        request.app.state.replay_executors = {}
    replay_executors: dict[str, ReplayExecutor] = request.app.state.replay_executors
    replay_executors[run_id] = executor

    async def event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        try:
            async for sse_dict in executor.replay_sse(speed_multiplier=speed):
                if await request.is_disconnected():
                    executor.cancel()
                    break
                yield ServerSentEvent(
                    data=sse_dict["data"],
                    event=sse_dict["event"],
                    id=sse_dict["id"],
                )
        finally:
            executors: dict[str, ReplayExecutor] = getattr(
                request.app.state, "replay_executors", {}
            )
            executors.pop(run_id, None)

    return EventSourceResponse(
        event_generator(),
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )
