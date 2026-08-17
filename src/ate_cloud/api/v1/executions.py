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
from ate_cloud.services.execution_dispatch import (
    ExecutionDispatchError,
    ExecutionDispatchService,
)
from ate_cloud.services.plan_materializer import (
    ExecutionPlanMaterializer,
    PlanMaterializeError,
)
from ate_cloud.schemas.execution import (
    ExecutionAbortResponse,
    ExecutionControlResponse,
    ExecutionCreate,
    ExecutionListItem,
    ExecutionResponse,
    ExecutionSearchRequest,
    ExecutionSearchResponse,
    SimulationRequest,
    SimulationResponse,
    SimulationResultEvent,
)
from ate_platform.simulation.dry_run_scheduler import DryRunScheduler
from ate_platform.simulation.full_chain_simulator import FullChainSimulator
from ate_platform.simulation.instrument_simulator import NoiseConfig, NoiseModel

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


# 拓扑运行时状态流（§8.3.6）：夹具/DUT 状态 SSE 推送。
# 静态路径，须先于 /{run_id} 等动态路径定义。
@router.get("/{run_id}/topology-stream", response_class=EventSourceResponse)
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
                except asyncio.TimeoutError:
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
            result = FullChainSimulator(
                noise_config=noise_config,
                fault_config=sim_data.fault_config,
            ).run(plan)
            for decision in result.dry_run_result.decisions:
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
            for meas in result.measurements:
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
            status = "passed" if result.all_passed else "failed"
            statistics = {
                "dry_run_passed": result.dry_run_result.passed,
                "dry_run_failed": result.dry_run_result.failed,
                "dry_run_skipped": result.dry_run_result.skipped,
                "measurements": len(result.measurements),
                "instrument_stats": result.instrument_stats,
                "summary": result.summary,
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
