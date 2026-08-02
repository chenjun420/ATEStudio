"""Multi-station workflow API endpoints.

Provides REST endpoints for managing multi-station test workflows:

- ``POST /api/v1/workflows`` - create/register a multi-station workflow.
- ``GET /api/v1/workflows/{workflow_id}`` - get a workflow definition.
- ``GET /api/v1/workflows/{workflow_id}/stations/{station_id}/status`` - get
  a station's handoff status for a given session (query param ``session_id``).

Workflows and handoffs are stored in the ``ate-handoffs`` JetStream KV
bucket (created on first access). The NATS client is expected on
``app.state.nc`` (set by the lifespan).

Per AGENTS.md section 7: if NATS or the KV bucket is unavailable, endpoints
return 502 Bad Gateway - no silent degradation.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ate_cloud.schemas.workflow import (
    StationConfigResponse,
    StationHandoffResponse,
    WorkflowCreate,
    WorkflowResponse,
)
from ate_platform.scheduler.station_orchestrator import StationOrchestrator
from shared.multi_station import (
    HandoffStatus,
    StationWorkflow,
    StationWorkflowConfig,
    handoff_to_dict,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])


def _get_orchestrator(request: Request) -> StationOrchestrator:
    """Dependency: create a StationOrchestrator bound to the app's NATS client.

    The NATS client is expected on ``app.state.nc`` (set by the lifespan).
    The orchestrator does not own the connection (caller manages lifecycle).
    """
    nc = getattr(request.app.state, "nc", None)
    if nc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NATS client not available",
        )
    return StationOrchestrator(station_id="api", nats_url="")


OrchestratorDep = Annotated[StationOrchestrator, Depends(_get_orchestrator)]


def _to_station_config_response(s: StationWorkflowConfig) -> StationConfigResponse:
    return StationConfigResponse(
        station_id=s.station_id,
        name=s.name,
        sequence_ref=s.sequence_ref,
        upstream_stations=list(s.upstream_stations),
        timeout=s.timeout,
    )


@router.post(
    "",
    response_model=WorkflowResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workflow(
    body: WorkflowCreate,
    orch: OrchestratorDep,
) -> WorkflowResponse:
    """POST /api/v1/workflows - create a multi-station workflow.

    Registers the workflow definition in the ``ate-handoffs`` KV bucket.
    If ``workflow_id`` is omitted, a UUID is generated.

    Raises:
        HTTPException: 502 if NATS/KV is unavailable.
    """
    workflow_id = body.workflow_id or str(uuid.uuid4())
    workflow = StationWorkflow(
        workflow_id=workflow_id,
        name=body.name,
        stations=[
            StationWorkflowConfig(
                station_id=s.station_id,
                name=s.name,
                sequence_ref=s.sequence_ref,
                upstream_stations=list(s.upstream_stations),
                timeout=s.timeout,
            )
            for s in body.stations
        ],
        handoff_rules=dict(body.handoff_rules),
    )
    try:
        await orch.connect()
        await orch.register_workflow(workflow)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    finally:
        await orch.disconnect()

    return WorkflowResponse(
        workflow_id=workflow.workflow_id,
        name=workflow.name,
        stations=[_to_station_config_response(s) for s in workflow.stations],
        handoff_rules=dict(workflow.handoff_rules),
        created_at=workflow.created_at,
    )


@router.get("/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: str,
    orch: OrchestratorDep,
) -> WorkflowResponse:
    """GET /api/v1/workflows/{workflow_id} - get a workflow definition.

    Raises:
        HTTPException: 404 if the workflow is not registered.
        HTTPException: 502 if NATS/KV is unavailable.
    """
    try:
        await orch.connect()
        workflow = await orch.get_workflow(workflow_id)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    finally:
        await orch.disconnect()

    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow '{workflow_id}' not found",
        )

    return WorkflowResponse(
        workflow_id=workflow.workflow_id,
        name=workflow.name,
        stations=[_to_station_config_response(s) for s in workflow.stations],
        handoff_rules=dict(workflow.handoff_rules),
        created_at=workflow.created_at,
    )


@router.get(
    "/{workflow_id}/stations/{station_id}/status",
    response_model=StationHandoffResponse,
)
async def get_station_handoff_status(
    workflow_id: str,
    station_id: str,
    session_id: Annotated[str, Query(..., description="Test session identifier")],
    orch: OrchestratorDep,
) -> StationHandoffResponse:
    """GET /api/v1/workflows/{workflow_id}/stations/{station_id}/status

    Get the handoff status for a specific station within a test session.

    The ``workflow_id`` path parameter scopes the lookup; the actual
    handoff record is keyed by ``session_id`` and ``station_id`` in KV.

    Query params:
        session_id: The test session (DUT flow) identifier.

    Returns:
        StationHandoffResponse with ``status`` one of:
        - ``pending``: no handoff record exists yet.
        - ``done``: handoff exists and ``pass_fail`` is true.
        - ``failed``: handoff exists and ``pass_fail`` is false.

    Raises:
        HTTPException: 502 if NATS/KV is unavailable.
    """
    try:
        await orch.connect()
        handoff = await orch.get_handoff(session_id, station_id)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        ) from e
    finally:
        await orch.disconnect()

    if handoff is None:
        return StationHandoffResponse(
            session_id=session_id,
            station_id=station_id,
            status=HandoffStatus.PENDING.value,
            handoff=None,
        )
    status_value = (
        HandoffStatus.DONE if handoff.pass_fail else HandoffStatus.FAILED
    )
    return StationHandoffResponse(
        session_id=session_id,
        station_id=station_id,
        status=status_value.value,
        handoff=handoff_to_dict(handoff),
    )
