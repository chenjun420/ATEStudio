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
import logging
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import ColumnElement, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette import EventSourceResponse, ServerSentEvent

from ate_cloud.config import settings
from ate_cloud.db import get_db
from ate_cloud.models.breakpoint import Breakpoint as BreakpointModel
from ate_cloud.models.execution import Execution
from ate_cloud.models.fault_event import SOURCE_LINK, SOURCE_MANUAL, FaultEvent
from ate_cloud.nats.sse_bridge import SSEBridge
from ate_cloud.schemas.execution import (
    BreakpointCreateRequest,
    BreakpointDeleteResponse,
    BreakpointListResponse,
    BreakpointResponse,
    BreakpointUpdateRequest,
    ExecutionAbortResponse,
    ExecutionControlResponse,
    ExecutionCreate,
    ExecutionListItem,
    ExecutionResponse,
    ExecutionSearchRequest,
    ExecutionSearchResponse,
    FaultInjectionRequest,
    FaultInjectionResponse,
    ManualFaultRequest,
    ManualFaultResponse,
    SimulationRequest,
    SimulationResponse,
    SimulationResultEvent,
    StepControlRequest,
    StepControlResponse,
)
from ate_cloud.services.breakpoint_registry import (
    BreakpointRegistry,
    TypedBreakpoint,
    new_breakpoint_id,
    validate_breakpoint,
)
from ate_cloud.services.execution_dispatch import (
    ExecutionDispatchError,
    ExecutionDispatchService,
)
from ate_cloud.services.plan_materializer import (
    ExecutionPlanMaterializer,
    PlanMaterializeError,
)
from ate_cloud.services.simulation_report import build_simulation_report
from ate_platform.simulation.diff import ExecutionDiff
from ate_platform.simulation.dry_run_scheduler import DryRunScheduler
from ate_platform.simulation.full_chain_simulator import FullChainSimulator
from ate_platform.simulation.instrument_simulator import NoiseConfig, NoiseModel
from ate_platform.simulation.recording import RecordingInterceptor

router = APIRouter(prefix="/executions", tags=["executions"])

#: Ticket-authenticated mount for the SSE streams (RH-3). Mirrors the
#: main router's prefix; mounted in router.py WITHOUT the mount-level JWT
#: guard (native EventSource cannot send headers) and WITH a mount-level
#: ``require_sse_user`` dependency instead. Routes stay defined here so
#: handler code, tags and path segments live next to their siblings.
sse_router = APIRouter(prefix="/executions", tags=["executions"])

logger = logging.getLogger(__name__)

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


async def _persist_fault_event(
    db: AsyncSession,
    *,
    run_id: str,
    link_id: str,
    fault_type: str,
    source: str,
    detail: dict[str, Any],
) -> None:
    """Persist one FaultEvent row (RH-1, v41-remaining-hardening #1).

    Best-effort by contract: called AFTER the NATS control publish and the
    topology SSE event so persistence can neither delay nor fail the
    injection path. Any DB error downgrades to a warning log — the request
    still returns 200 (mirrors the NATS-outage tolerance above).

    Args:
        db: Database session.
        run_id: Execution run identifier.
        link_id: Topology link id (empty string for non-link manual scopes).
        fault_type: Fault type string.
        source: ``link`` | ``manual`` | ``scheduler``.
        detail: JSON payload (fault_id/scope/layer/target_id/params).
    """
    try:
        db.add(
            FaultEvent(
                id=str(uuid.uuid4()),
                run_id=run_id,
                link_id=link_id,
                fault_type=fault_type,
                source=source,
                detail=detail,
            )
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — non-blocking by contract
        await db.rollback()
        logger.warning(
            "fault event persistence failed for run %s (non-blocking): %s",
            run_id,
            exc,
        )


def get_sse_bridge(request: Request) -> SSEBridge:
    """Get the SSEBridge instance from app state.

    Args:
        request: The incoming FastAPI request.

    Returns:
        SSEBridge instance attached to app.state.
    """
    bridge = getattr(request.app.state, "sse_bridge", None)
    if not isinstance(bridge, SSEBridge):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SSE bridge not available",
        )
    return bridge


# Type alias for SSEBridge dependency (avoids B008 ruff warning).
# Defined after get_sse_bridge so the Annotated alias can reference it.
BridgeDep = Annotated[SSEBridge, Depends(get_sse_bridge)]


def get_breakpoint_registry(request: Request) -> BreakpointRegistry:
    """Get (lazily creating) the typed breakpoint registry from app state (T39).

    Args:
        request: The incoming FastAPI request.

    Returns:
        BreakpointRegistry instance attached to app.state.
    """
    registry = getattr(request.app.state, "breakpoint_registry", None)
    if not isinstance(registry, BreakpointRegistry):
        registry = BreakpointRegistry()
        request.app.state.breakpoint_registry = registry
    return registry


# Type alias for the breakpoint registry dependency (avoids B008).
RegistryDep = Annotated[BreakpointRegistry, Depends(get_breakpoint_registry)]


@sse_router.get("/{run_id}/events", response_class=EventSourceResponse)
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
                    except TimeoutError:
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


# 拓扑运行时状态流（§8.3.6）：夹具/DUT 状态 SSE 推送。
# 静态路径，须先于 /{run_id} 等动态路径定义。
@sse_router.get("/{run_id}/topology-stream", response_class=EventSourceResponse)
async def stream_topology_state(
    run_id: str,
    request: Request,
    bridge: SSEBridge = Depends(get_sse_bridge),
) -> EventSourceResponse:
    """SSE endpoint streaming fixture/DUT topology runtime state.

    事件类型（§8.3.6）：instrument / link / relay / measurement / fixture / fault。
    使用独立的 "topology" 流队列，与 /events 主队列隔离，多客户端无竞争。

    Args:
        run_id: The execution run identifier.
        request: The incoming HTTP request (used for disconnect detection).
        bridge: The SSEBridge instance.

    Returns:
        EventSourceResponse streaming topology state events.
    """
    queue = bridge.get_stream_queue(run_id, "topology")

    async def event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        heartbeat_interval = 15.0  # seconds
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=heartbeat_interval
                    )
                    yield ServerSentEvent(
                        data=json.dumps(event.get("data", {})),
                        event=event.get("type", "event"),
                        id=event.get("id"),
                    )
                except TimeoutError:
                    yield ServerSentEvent(data="", comment="keep-alive")
        finally:
            bridge.remove_stream_queue(run_id, "topology")

    return EventSourceResponse(event_generator())


@router.post("/{run_id}/simulate", response_model=SimulationResponse)
async def simulate_execution(
    run_id: str,
    sim_data: SimulationRequest,
    db: DBSession,
) -> SimulationResponse:
    """Run a simulation for the execution (设计文档 §7 三层仿真 + §8.4 仿真控制台).

    云侧仿真入口：materialize 执行序列的 YamlPlan，然后按层级执行：
    - ``driver``:  驱动级仿真（仪器 SIM 驱动器，测量生成含噪声）——使用 FullChainSimulator。
    - ``dry_run``: 调度器空跑（DryRunScheduler），无测量。
    - ``full``:    全链路仿真（驱动仿真 + 调度空跑 + 噪声模型）。

    Args:
        run_id: The execution run identifier.
        sim_data: Simulation tier + noise configuration.
        db: Database session.

    Returns:
        SimulationResponse with events (decisions + measurements) and statistics.

    Raises:
        HTTPException: 404 if the execution/sequence cannot be materialized.
    """
    materializer = ExecutionPlanMaterializer(db)
    try:
        plan = await materializer.materialize(run_id)
    except PlanMaterializeError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None

    import time as _time

    start = _time.monotonic()
    events: list[SimulationResultEvent] = []
    status = "passed"
    statistics: dict[str, Any] = {}

    try:
        if sim_data.tier == "dry_run":
            result = DryRunScheduler().dry_run(plan)
            for decision in result.decisions:
                events.append(
                    SimulationResultEvent(
                        step_id=decision.step_id,
                        timestamp=decision.timestamp,
                        event_type="decision",
                        data={
                            "decision": str(decision.decision),
                            "reason": decision.reason,
                            "condition_met": decision.condition_met,
                            "resources_acquired": decision.resources_acquired,
                        },
                    )
                )
            status = "passed" if result.all_passed else "failed"
            statistics = {
                "passed": result.passed,
                "failed": result.failed,
                "skipped": result.skipped,
                "blocked": result.blocked,
                "errors": result.errors,
                "not_reached": result.not_reached,
                "total_steps": result.total_steps,
                "deadlock_detected": result.deadlock_detected,
            }
        else:
            # driver / full 共用 FullChainSimulator（driver 无噪声模型差异）
            noise_config = NoiseConfig(
                model=NoiseModel(sim_data.noise_model),
                noise_sigma=sim_data.noise_sigma,
                drift_rate=sim_data.drift_rate,
                bias=sim_data.bias,
                seed=sim_data.seed,
            )
            full_result = FullChainSimulator(
                noise_config=noise_config,
                fault_config=sim_data.fault_config,
            ).run(plan)
            for decision in full_result.dry_run_result.decisions:
                events.append(
                    SimulationResultEvent(
                        step_id=decision.step_id,
                        timestamp=decision.timestamp,
                        event_type="decision",
                        data={
                            "decision": str(decision.decision),
                            "reason": decision.reason,
                            "condition_met": decision.condition_met,
                        },
                    )
                )
            for meas in full_result.measurements:
                events.append(
                    SimulationResultEvent(
                        step_id=meas.step_id,
                        timestamp=meas.timestamp,
                        event_type="measurement",
                        data={
                            "instrument_type": meas.instrument_type,
                            "true_value": meas.true_value,
                            "simulated_value": meas.simulated_value,
                            "noise_error": meas.noise_error,
                            "noise_applied": meas.noise_applied,
                        },
                    )
                )
            status = "passed" if full_result.all_passed else "failed"
            statistics = {
                "dry_run_passed": full_result.dry_run_result.passed,
                "dry_run_failed": full_result.dry_run_result.failed,
                "dry_run_skipped": full_result.dry_run_result.skipped,
                "measurements": len(full_result.measurements),
                "instrument_stats": full_result.instrument_stats,
                "summary": full_result.summary,
            }
    except Exception as e:  # noqa: BLE001
        status = "error"
        statistics["error"] = str(e)

    duration = _time.monotonic() - start
    return SimulationResponse(
        session_id=run_id,
        tier=sim_data.tier,
        status=status,
        events=events,
        duration_seconds=duration,
        statistics=statistics,
    )


@router.post("", response_model=ExecutionResponse, status_code=status.HTTP_201_CREATED)
async def create_execution(
    execution_data: ExecutionCreate,
    db: AsyncSession = Depends(get_db),
    bridge: SSEBridge = Depends(get_sse_bridge),
) -> ExecutionResponse:
    """Start a new execution.

    Creates an Execution record with PENDING status, materializes the
    sequence into a YamlPlan, dispatches it to the worker via JetStream
    (``ate.tasks.{run_id}``), and publishes an EXECUTION_STARTED event via
    the SSE bridge.

    If NATS is unavailable or dispatch fails, the endpoint returns 503 and
    the Execution record stays PENDING (retryable).

    Args:
        execution_data: The execution creation data (sequence_id required).
        db: Database session.
        bridge: SSEBridge instance.

    Returns:
        ExecutionResponse: The created execution with generated run_id.

    Raises:
        HTTPException: 503 if the plan cannot be dispatched to NATS.
    """
    run_id = str(uuid.uuid4())

    # Calibration gate: block execution when any referenced instrument has an
    # EXPIRED calibration (HTTP 409). No Execution row is created in that case.
    # Calibration is opt-in per instrument — instruments with no record are
    # UNKNOWN and pass through.
    instrument_ids = (execution_data.config or {}).get("instrument_ids") or []
    if instrument_ids:
        from ate_cloud.services.calibration_manager import CalibrationManager

        cal_manager = CalibrationManager(db)
        expired = [
            str(iid)
            for iid in instrument_ids
            if await cal_manager.is_expired(str(iid))
        ]
        if expired:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Calibration expired for instrument(s): "
                    f"{', '.join(expired)}. Blocking execution until recalibrated."
                ),
            )

    execution = Execution(
        id=run_id,
        sequence_id=execution_data.sequence_id,
        status="PENDING",
        config=execution_data.config,
    )

    db.add(execution)
    await db.commit()
    await db.refresh(execution)

    # Materialize + dispatch to the worker. A NATS outage or dispatch
    # failure surfaces as 503; the PENDING record stays for retry.
    try:
        from ate_cloud.main import get_nats

        nc = get_nats()
    except RuntimeError:
        nc = None

    if nc is not None:
        try:
            materializer = ExecutionPlanMaterializer(db)
            plan = await materializer.materialize(run_id)
            dispatcher = ExecutionDispatchService(nc)
            await dispatcher.dispatch(run_id, plan)
        except (PlanMaterializeError, ExecutionDispatchError) as e:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Dispatch failed: {e}",
            ) from e

    # Publish EXECUTION_STARTED event via bridge
    await bridge.publish_event(
        run_id=run_id,
        event_type="EXECUTION_STARTED",
        data={"run_id": run_id, "sequence_id": execution_data.sequence_id, "status": "PENDING"},
    )

    return ExecutionResponse.model_validate(execution)


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

    conditions: list[ColumnElement[bool]] = []

    if search_data.serial_number:
        conditions.append(
            Execution.dut_serial.ilike(f"%{search_data.serial_number}%")
        )

    if search_data.product_type:
        # Filter by product_type stored in config JSON.
        # SQLite JSON: config->>'$.product_type'; PostgreSQL: config->>'product_type'
        # Using ilike on cast for cross-database compatibility.
        conditions.append(
            func.cast(Execution.config, String).ilike(
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

    return ExecutionResponse.model_validate(execution)


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


# Control action → (target status, SSE event type) mapping.
_CONTROL_ACTIONS: dict[str, tuple[str, str]] = {
    "pause": ("PAUSING", "EXECUTION_PAUSED"),
    "resume": ("RESUMING", "EXECUTION_STARTED"),
    "force_next": ("FORCE_NEXT", "EXTERNAL_CMD"),
}
_TERMINAL_STATES = {"COMPLETED", "FAILED", "ABORTED"}


async def _control_execution(
    run_id: str,
    action: str,
    db: AsyncSession,
    bridge: SSEBridge,
) -> ExecutionControlResponse:
    """Apply a runtime control action (pause/resume/force_next).

    Persists the target status, publishes a Core NATS control message to
    ``ate.control.{run_id}`` (``{action, run_id}`` payload) so the worker
    can act on it, and broadcasts the corresponding SSE event. A NATS
    outage is non-fatal — the API still returns 200 (worker-independent
    control surface).

    Args:
        run_id: The execution run identifier.
        action: One of "pause"/"resume"/"force_next".
        db: Database session.
        bridge: SSEBridge instance.

    Returns:
        ExecutionControlResponse with id/action/status.

    Raises:
        HTTPException: 404 if execution not found.
        HTTPException: 409 if execution is in a terminal state.
    """
    target_status, sse_type = _CONTROL_ACTIONS[action]

    result = await db.execute(select(Execution).where(Execution.id == run_id))
    execution = result.scalar_one_or_none()

    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    if execution.status in _TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"Execution is already in terminal state: {execution.status}",
        )

    execution.status = target_status
    await db.commit()

    # Publish Core NATS control message (non-fatal on NATS outage).
    try:
        from ate_cloud.main import get_nats

        nc = get_nats()
        if nc is not None:
            payload = json.dumps({"action": action, "run_id": run_id}).encode()
            await nc.publish(f"ate.control.{run_id}", payload)
    except RuntimeError:
        pass

    await bridge.publish_event(
        run_id=run_id,
        event_type=sse_type,
        data={"run_id": run_id, "action": action, "status": target_status},
    )

    return ExecutionControlResponse(id=run_id, action=action, status=target_status)


@router.post("/{run_id}/pause", response_model=ExecutionControlResponse)
async def pause_execution(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    bridge: SSEBridge = Depends(get_sse_bridge),
) -> ExecutionControlResponse:
    """Pause a running/pending execution.

    Args:
        run_id: The execution run identifier.
        db: Database session.
        bridge: SSEBridge instance.

    Returns:
        ExecutionControlResponse: Confirmation with PAUSING status.

    Raises:
        HTTPException: 404 if execution not found.
        HTTPException: 409 if execution is already in a terminal state.
    """
    return await _control_execution(run_id, "pause", db, bridge)


@router.post("/{run_id}/resume", response_model=ExecutionControlResponse)
async def resume_execution(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    bridge: SSEBridge = Depends(get_sse_bridge),
) -> ExecutionControlResponse:
    """Resume a paused execution.

    Args:
        run_id: The execution run identifier.
        db: Database session.
        bridge: SSEBridge instance.

    Returns:
        ExecutionControlResponse: Confirmation with RESUMING status.

    Raises:
        HTTPException: 404 if execution not found.
        HTTPException: 409 if execution is already in a terminal state.
    """
    return await _control_execution(run_id, "resume", db, bridge)


@router.post("/{run_id}/force_next", response_model=ExecutionControlResponse)
async def force_next_execution(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    bridge: SSEBridge = Depends(get_sse_bridge),
) -> ExecutionControlResponse:
    """Force the next step in a running execution.

    Args:
        run_id: The execution run identifier.
        db: Database session.
        bridge: SSEBridge instance.

    Returns:
        ExecutionControlResponse: Confirmation with FORCE_NEXT status.

    Raises:
        HTTPException: 404 if execution not found.
        HTTPException: 409 if execution is already in a terminal state.
    """
    return await _control_execution(run_id, "force_next", db, bridge)


@router.post("/{run_id}/step-control", response_model=StepControlResponse)
async def step_control_execution(
    run_id: str,
    body: StepControlRequest,
    db: DBSession,
) -> StepControlResponse:
    """Send a debugger step-mode command to a paused execution (T40，§8.4).

    断点暂停（T39）后，SimulationConsole 工具栏把 over/into/out/
    run_to_cursor 指令转发给边端调度器的单步状态机：复用既有控制主题
    ``ate.control.{run_id}``（``{action: "step_control", mode, ...}``），
    worker 经 request-reply 校验模式/目标后武装 ``arm_step_mode``。
    执行级状态不变（仍是暂停态），故不写 DB、不发执行 SSE。

    A NATS outage is non-fatal — the API still returns 200 (consistent
    with pause/resume/force_next control surface).

    Args:
        run_id: The execution run identifier.
        body: Step mode (+ target step for run_to_cursor).
        db: Database session.

    Returns:
        StepControlResponse with ok=true and the accepted mode.

    Raises:
        HTTPException: 404 if execution not found.
        HTTPException: 409 if execution has no active (non-terminal) run.
    """
    result = await db.execute(select(Execution).where(Execution.id == run_id))
    execution = result.scalar_one_or_none()

    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    if execution.status in _TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Execution has no active execution to step through "
                f"(status: {execution.status})"
            ),
        )

    # Forward via the control subject (non-fatal on NATS outage).
    try:
        from ate_cloud.main import get_nats

        nc = get_nats()
        if nc is not None:
            payload = json.dumps({
                "action": "step_control",
                "run_id": run_id,
                "mode": body.mode,
                "target_step_id": body.target_step_id,
            }).encode()
            await nc.publish(f"ate.control.{run_id}", payload)
    except RuntimeError:
        pass

    return StepControlResponse(
        run_id=run_id,
        mode=body.mode,
        target_step_id=body.target_step_id,
    )


@router.get("/{run_id}/diff")
async def diff_execution(
    run_id: str,
    baseline: str,
    db: DBSession,
) -> dict[str, Any]:
    """Compare a run against a baseline run (T37, v41-gap-analysis #37).

    Loads both JSONL recordings from ``settings.recordings_dir`` using the
    T10 finalize convention (``<recordings_dir>/<run_id>.jsonl``) and returns
    the :meth:`ate_platform.simulation.diff.ExecutionDiff.compare` summary
    verbatim (schema documented in that module docstring), enveloped with
    ``run_id`` / ``baseline`` identity. ``exec_a`` is the baseline stream,
    ``exec_b`` the candidate — so deltas read "candidate vs baseline".

    Args:
        run_id: Candidate execution run identifier.
        baseline: Baseline execution run identifier.
        db: Database session.

    Returns:
        ExecutionDiff summary dict plus ``run_id`` / ``baseline`` keys.

    Raises:
        HTTPException: 404 if either run is unknown or either recording
            file is missing on disk.
    """
    for rid in (run_id, baseline):
        result = await db.execute(select(Execution).where(Execution.id == rid))
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Execution '{rid}' not found",
            )

    recordings = Path(settings.recordings_dir)
    baseline_path = recordings / f"{baseline}.jsonl"
    candidate_path = recordings / f"{run_id}.jsonl"
    for path in (baseline_path, candidate_path):
        if not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recording not found: {path}",
            )

    summary = ExecutionDiff.compare(
        RecordingInterceptor.load(baseline_path),
        RecordingInterceptor.load(candidate_path),
    )
    return {"run_id": run_id, "baseline": baseline, **summary}


@router.get("/{run_id}/simulation-report")
async def get_simulation_report(
    run_id: str,
    db: DBSession,
) -> dict[str, Any]:
    """Consolidated end-of-run simulation report (T41, v41-gap-analysis #41).

    Composes three already-computed sources via
    :func:`ate_cloud.services.simulation_report.build_simulation_report`:
    coverage (T14 ``SimulationCoverage.report`` over the materialized plan's
    compiled DAG + recorded step events), contention (T13 analyzer fed the
    recording's lock events), and the execution's recorded fault records.
    Missing data degrades to per-section ``available: false`` entries listed
    in ``warnings`` — an aborted/partial run still renders.

    Args:
        run_id: The execution run identifier.
        db: Database session.

    Returns:
        Report envelope (schema in the service module docstring).

    Raises:
        HTTPException: 404 if execution not found.
    """
    result = await db.execute(select(Execution).where(Execution.id == run_id))
    execution = result.scalar_one_or_none()
    if not execution:
        raise HTTPException(status_code=404, detail=f"Execution '{run_id}' not found")
    return await build_simulation_report(db, run_id, execution)


@router.post("/{run_id}/fault-injection", response_model=FaultInjectionResponse)
async def inject_link_fault(
    run_id: str,
    fault_data: FaultInjectionRequest,
    db: DBSession,
    bridge: BridgeDep,
) -> FaultInjectionResponse:
    """Inject a link fault into a running execution (T44，设计文档 §8.3).

    Accepts ``{link_id, fault_type}`` from the FixtureDesigner right-click
    menu and forwards a T5-style ``inject_fault`` control message to the
    worker (``ate.control.{run_id}``, ``{action, run_id, rule}``). The rule
    is a §7.7.2 DSL dict targeting the link at the network layer, so the
    worker's ``FaultInjector.load([rule_cfg])`` validates it through the
    same path as YAML-declared rules. Also publishes a topology-stream SSE
    ``fault`` event so the frontend paints the link fault-red (§8.3.7).

    A NATS outage is non-fatal — the API still returns 200 (consistent with
    pause/resume/force_next control surface).

    Args:
        run_id: The execution run identifier.
        fault_data: Link id + fault type (+ optional params).
        db: Database session.
        bridge: SSEBridge instance.

    Returns:
        FaultInjectionResponse with ok=true and the generated fault_id.

    Raises:
        HTTPException: 404 if execution not found.
        HTTPException: 409 if execution has no active (non-terminal) run.
    """
    result = await db.execute(select(Execution).where(Execution.id == run_id))
    execution = result.scalar_one_or_none()

    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    if execution.status in _TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Execution has no active execution to inject into "
                f"(status: {execution.status})"
            ),
        )

    # §7.7.2 DSL 规则：网络层、target=link_id、首次调用即命中、一次性。
    # fault_id 确定性生成，便于前端/日志与规则对账。
    fault_id = f"link-{fault_data.link_id}-{fault_data.fault_type}"
    action: dict[str, Any] = {"type": fault_data.fault_type}
    if fault_data.params:
        action.update(fault_data.params)
    rule_cfg: dict[str, Any] = {
        "fault_id": fault_id,
        "layer": "network",
        "target": fault_data.link_id,
        "trigger": {"type": "count", "value": 1},
        "action": action,
        "once": True,
    }

    # Forward via the T5 control subject (non-fatal on NATS outage).
    try:
        from ate_cloud.main import get_nats

        nc = get_nats()
        if nc is not None:
            payload = json.dumps(
                {"action": "inject_fault", "run_id": run_id, "rule": rule_cfg}
            ).encode()
            await nc.publish(f"ate.control.{run_id}", payload)
    except RuntimeError:
        pass

    # Topology SSE fault event — FixtureDesigner paints the link red (§8.3.7).
    await bridge.publish_stream_event(
        run_id=run_id,
        stream="topology",
        event_type="fault",
        data={
            "run_id": run_id,
            "link_id": fault_data.link_id,
            "fault_type": fault_data.fault_type,
            "fault_id": fault_id,
            "status": "active",
        },
    )

    # RH-1: persist AFTER NATS publish + SSE event (best-effort, non-blocking).
    await _persist_fault_event(
        db,
        run_id=run_id,
        link_id=fault_data.link_id,
        fault_type=fault_data.fault_type,
        source=SOURCE_LINK,
        detail={
            "fault_id": fault_id,
            "layer": "network",
            "params": dict(fault_data.params or {}),
        },
    )

    return FaultInjectionResponse(
        ok=True,
        run_id=run_id,
        link_id=fault_data.link_id,
        fault_type=fault_data.fault_type,
        fault_id=fault_id,
    )


# T38 手动故障注入：scope → §7.7.1 注入层映射。
MANUAL_SCOPE_LAYERS: dict[str, str] = {
    "link": "network",
    "instrument": "instrument",
    "step": "scheduler",
    "scheduler": "scheduler",
    "protocol": "protocol",
}

# T38 每个 scope 允许的故障类型集合（与 FaultAction.is_exception 的
# 非异常类动作及 §8.3/§7.7 词汇对齐；越界返回 422）。
MANUAL_SCOPE_FAULT_TYPES: dict[str, frozenset[str]] = {
    "link": frozenset({
        "open_circuit", "short_circuit", "contact_resistance", "noise",
        "delay", "packet_loss", "reorder",
    }),
    "instrument": frozenset({
        "measurement_out_of_range", "over_voltage", "over_current",
        "communication", "selftest_failed", "noise", "value_override",
    }),
    "step": frozenset({"timeout", "force_fail", "skip_step", "value_override"}),
    "scheduler": frozenset({"resource_deadlock", "timeout", "force_fail"}),
    "protocol": frozenset({"scpi_error", "truncated_data", "checksum_error"}),
}


@router.post("/{run_id}/manual-fault", response_model=ManualFaultResponse)
async def inject_manual_fault(
    run_id: str,
    fault_data: ManualFaultRequest,
    db: DBSession,
    bridge: BridgeDep,
) -> ManualFaultResponse:
    """Inject an operator-composed fault into a running execution (T38).

    Manual fault injection panel backend: unlike the DSL-driven path the
    operator composes a rule here without waiting for a YAML trigger. The
    ``scope`` maps to a §7.7.1 layer (link→network, instrument→instrument,
    step/scheduler→scheduler, protocol→protocol) and the composed rule is
    forwarded via the same T5 ``inject_fault`` control subject as T44.
    Also publishes a topology-stream SSE ``fault`` event.

    A NATS outage is non-fatal — the API still returns 200 (consistent
    with the T44 link-fault endpoint and pause/resume control surface).

    Args:
        run_id: The execution run identifier.
        fault_data: Scope + target + fault type (+ optional params).
        db: Database session.
        bridge: SSEBridge instance.

    Returns:
        ManualFaultResponse with ok=true, mapped layer and generated fault_id.

    Raises:
        HTTPException: 404 if execution not found.
        HTTPException: 409 if execution has no active (non-terminal) run.
        HTTPException: 422 if fault_type is not allowed for the scope.
    """
    result = await db.execute(select(Execution).where(Execution.id == run_id))
    execution = result.scalar_one_or_none()

    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    if execution.status in _TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Execution has no active execution to inject into "
                f"(status: {execution.status})"
            ),
        )

    layer = MANUAL_SCOPE_LAYERS[fault_data.scope]
    allowed = MANUAL_SCOPE_FAULT_TYPES[fault_data.scope]
    if fault_data.fault_type not in allowed:
        raise HTTPException(
            status_code=422,
            detail=(
                f"fault_type '{fault_data.fault_type}' is not allowed for "
                f"scope '{fault_data.scope}'. Allowed: {sorted(allowed)}"
            ),
        )

    # §7.7.2 DSL 规则：首次调用即命中、一次性（手动注入语义）。
    fault_id = (
        f"manual-{fault_data.scope}-{fault_data.target_id}-"
        f"{fault_data.fault_type}"
    )
    action: dict[str, Any] = {"type": fault_data.fault_type}
    if fault_data.params:
        action.update(fault_data.params)
    rule_cfg: dict[str, Any] = {
        "fault_id": fault_id,
        "layer": layer,
        "target": fault_data.target_id,
        "trigger": {"type": "count", "value": 1},
        "action": action,
        "once": True,
    }

    # Forward via the T5 control subject (non-fatal on NATS outage).
    try:
        from ate_cloud.main import get_nats

        nc = get_nats()
        if nc is not None:
            payload = json.dumps(
                {"action": "inject_fault", "run_id": run_id, "rule": rule_cfg}
            ).encode()
            await nc.publish(f"ate.control.{run_id}", payload)
    except RuntimeError:
        pass

    # Topology SSE fault event (§8.3.7 consumers; scope-aware payload).
    await bridge.publish_stream_event(
        run_id=run_id,
        stream="topology",
        event_type="fault",
        data={
            "run_id": run_id,
            "scope": fault_data.scope,
            "target_id": fault_data.target_id,
            "fault_type": fault_data.fault_type,
            "fault_id": fault_id,
            "status": "active",
        },
    )

    # RH-1: persist AFTER NATS publish + SSE event (best-effort, non-blocking).
    # 非 link 域的 target 不是拓扑链路——存空串让热力图聚合自然跳过。
    await _persist_fault_event(
        db,
        run_id=run_id,
        link_id=fault_data.target_id if fault_data.scope == "link" else "",
        fault_type=fault_data.fault_type,
        source=SOURCE_MANUAL,
        detail={
            "fault_id": fault_id,
            "scope": fault_data.scope,
            "layer": layer,
            "target_id": fault_data.target_id,
            "params": dict(fault_data.params or {}),
        },
    )

    return ManualFaultResponse(
        ok=True,
        run_id=run_id,
        scope=fault_data.scope,
        layer=layer,
        target_id=fault_data.target_id,
        fault_type=fault_data.fault_type,
        fault_id=fault_id,
    )


# ---------------------------------------------------------------------------
# T39 typed simulation breakpoints (§8.4)
#
# Durable persistence: every typed breakpoint is stored in the ``breakpoints``
# table (session_id == run_id, kind/target populated, legacy debug columns
# empty). The DB is the source of truth — it survives restarts and is what the
# edge will later receive (task 20). The in-memory BreakpointRegistry is a
# write-through cache used ONLY for live in-process hit detection
# (handle_status_event → SSE BREAKPOINT_HIT + NATS pause); it is never read by
# the CRUD endpoints below.
# ---------------------------------------------------------------------------


def _model_to_typed(row: BreakpointModel) -> TypedBreakpoint:
    """Map a persisted T39 breakpoint row to its live registry value."""
    return TypedBreakpoint(
        id=row.id,
        run_id=row.session_id,
        kind=row.kind or "",
        target=row.target or "",
        condition=row.condition,
        enabled=row.enabled,
    )


def _typed_to_model(bp: TypedBreakpoint) -> BreakpointModel:
    """Map a T39 breakpoint value to its persisted row (debug columns empty)."""
    return BreakpointModel(
        id=bp.id,
        session_id=bp.run_id,
        kind=bp.kind,
        target=bp.target,
        step_id="",
        node_id="",
        line_number=0,
        condition=bp.condition,
        enabled=bp.enabled,
        node_data=None,
    )


async def _get_persisted_breakpoint(
    db: AsyncSession,
    run_id: str,
    bp_id: str,
) -> BreakpointModel | None:
    """Fetch a T39 breakpoint row scoped to ``run_id``.

    Legacy debugpy rows (``kind IS NULL``) are excluded so the two breakpoint
    shapes never collide on the shared table.
    """
    result = await db.execute(
        select(BreakpointModel).where(
            BreakpointModel.id == bp_id,
            BreakpointModel.session_id == run_id,
            BreakpointModel.kind.is_not(None),
        )
    )
    return result.scalar_one_or_none()


async def _require_active_execution(db: AsyncSession, run_id: str) -> None:
    """404 on unknown run, 409 on a terminal-state run (breakpoints need a live run)."""
    result = await db.execute(select(Execution).where(Execution.id == run_id))
    execution = result.scalar_one_or_none()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if execution.status in _TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Execution has no active execution to arm breakpoints on "
                f"(status: {execution.status})"
            ),
        )


@router.post("/{run_id}/breakpoints", response_model=BreakpointResponse)
async def create_breakpoint(
    run_id: str,
    bp_data: BreakpointCreateRequest,
    db: DBSession,
    registry: RegistryDep,
) -> BreakpointResponse:
    """Register a typed breakpoint on a running execution (T39, §8.4).

    Persists to the ``breakpoints`` table (durable) and write-through caches
    the value in the in-memory registry for live hit detection. Four kinds:
    ``step`` (step id) | ``instrument_call`` (resource.method) |
    ``variable_change`` (scope.key) | ``condition`` (simpleeval-subset
    expression evaluated server-side only). Validation errors surface as 422;
    unknown runs 404; terminal-state runs 409.

    Args:
        run_id: The execution run identifier.
        bp_data: kind + target (+ condition for the condition kind).
        db: Database session.
        registry: Typed breakpoint registry (live hit cache).

    Returns:
        BreakpointResponse with the generated breakpoint id.

    Raises:
        HTTPException: 404 unknown run / 409 terminal state / 422 validation.
    """
    await _require_active_execution(db, run_id)

    try:
        validate_breakpoint(bp_data.kind, bp_data.target, bp_data.condition)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    bp = TypedBreakpoint(
        id=new_breakpoint_id(),
        run_id=run_id,
        kind=bp_data.kind,
        target=bp_data.target.strip(),
        condition=(bp_data.condition or "").strip() or None,
    )

    # Durable store first; only cache for live hits once persisted.
    db.add(_typed_to_model(bp))
    await db.commit()
    registry.add(bp)
    return BreakpointResponse(**bp.to_dict())


@router.get("/{run_id}/breakpoints", response_model=BreakpointListResponse)
async def list_breakpoints(
    run_id: str,
    db: DBSession,
) -> BreakpointListResponse:
    """List typed breakpoints persisted for a run (T39, durable source of truth).

    Args:
        run_id: The execution run identifier.
        db: Database session.

    Returns:
        BreakpointListResponse with items + total.
    """
    result = await db.execute(
        select(BreakpointModel)
        .where(
            BreakpointModel.session_id == run_id,
            BreakpointModel.kind.is_not(None),
        )
        .order_by(BreakpointModel.created_at)
    )
    rows = result.scalars().all()
    items = [BreakpointResponse(**_model_to_typed(row).to_dict()) for row in rows]
    return BreakpointListResponse(items=items, total=len(items))


@router.get("/{run_id}/breakpoints/{bp_id}", response_model=BreakpointResponse)
async def get_breakpoint(
    run_id: str,
    bp_id: str,
    db: DBSession,
) -> BreakpointResponse:
    """Get a single typed breakpoint by id (T39).

    Args:
        run_id: The execution run identifier.
        bp_id: The breakpoint identifier.
        db: Database session.

    Returns:
        BreakpointResponse for the breakpoint.

    Raises:
        HTTPException: 404 when no such breakpoint exists for the run.
    """
    row = await _get_persisted_breakpoint(db, run_id, bp_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Breakpoint not found")
    return BreakpointResponse(**_model_to_typed(row).to_dict())


@router.put("/{run_id}/breakpoints/{bp_id}", response_model=BreakpointResponse)
async def update_breakpoint(
    run_id: str,
    bp_id: str,
    bp_data: BreakpointUpdateRequest,
    db: DBSession,
    registry: RegistryDep,
) -> BreakpointResponse:
    """Update a typed breakpoint's condition and/or enabled flag (T39).

    ``kind`` / ``target`` are immutable identity attributes; only the
    ``condition`` expression and the active toggle may change. A disabled
    breakpoint never fires. Changes persist to the DB and write-through to the
    live hit registry.

    Args:
        run_id: The execution run identifier.
        bp_id: The breakpoint identifier.
        bp_data: Partial update (condition and/or enabled).
        db: Database session.
        registry: Typed breakpoint registry (live hit cache).

    Returns:
        BreakpointResponse for the updated breakpoint.

    Raises:
        HTTPException: 404 unknown breakpoint / 422 invalid condition.
    """
    row = await _get_persisted_breakpoint(db, run_id, bp_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Breakpoint not found")

    if bp_data.condition is not None:
        new_condition = bp_data.condition.strip() or None
        try:
            validate_breakpoint(row.kind or "", row.target or "", new_condition)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        row.condition = new_condition
    if bp_data.enabled is not None:
        row.enabled = bp_data.enabled

    await db.commit()
    await db.refresh(row)
    typed = _model_to_typed(row)
    registry.add(typed)  # replace the cached live value
    return BreakpointResponse(**typed.to_dict())


@router.post(
    "/{run_id}/breakpoints/{bp_id}/{action}",
    response_model=BreakpointResponse,
)
async def set_breakpoint_enabled(
    run_id: str,
    bp_id: str,
    action: Literal["enable", "disable"],
    db: DBSession,
    registry: RegistryDep,
) -> BreakpointResponse:
    """Enable or disable a typed breakpoint (T39 enable/disable action).

    ``POST .../disable`` sets ``enabled=False`` — a disabled breakpoint does
    not fire; ``POST .../enable`` re-arms it. Persisted and write-through to
    the live hit registry.

    Args:
        run_id: The execution run identifier.
        bp_id: The breakpoint identifier.
        action: ``enable`` or ``disable`` (other values → 422).
        db: Database session.
        registry: Typed breakpoint registry (live hit cache).

    Returns:
        BreakpointResponse for the toggled breakpoint.

    Raises:
        HTTPException: 404 when no such breakpoint exists for the run.
    """
    row = await _get_persisted_breakpoint(db, run_id, bp_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Breakpoint not found")

    row.enabled = action == "enable"
    await db.commit()
    await db.refresh(row)
    typed = _model_to_typed(row)
    registry.add(typed)  # replace the cached live value
    return BreakpointResponse(**typed.to_dict())


@router.delete("/{run_id}/breakpoints/{bp_id}", response_model=BreakpointDeleteResponse)
async def delete_breakpoint(
    run_id: str,
    bp_id: str,
    db: DBSession,
    registry: RegistryDep,
) -> BreakpointDeleteResponse:
    """Remove a typed breakpoint — idempotent (T39).

    Deletes the durable row (when present) and evicts it from the live hit
    registry. Deleting an unknown breakpoint still returns 200 with
    removed=false so client retries never fail.

    Args:
        run_id: The execution run identifier.
        bp_id: The breakpoint to remove.
        db: Database session.
        registry: Typed breakpoint registry (live hit cache).

    Returns:
        BreakpointDeleteResponse with removed flag.
    """
    row = await _get_persisted_breakpoint(db, run_id, bp_id)
    if row is not None:
        await db.delete(row)
        await db.commit()
    registry.remove(run_id, bp_id)
    return BreakpointDeleteResponse(ok=True, removed=row is not None)
