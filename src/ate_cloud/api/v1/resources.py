"""Human and robot resource management API endpoints.

Provides CRUD endpoints for managing operator and robot resource
registrations used by the CP-SAT scheduler:

- ``POST /api/v1/resources/humans`` — register a human operator.
- ``GET  /api/v1/resources/humans`` — list all registered operators.
- ``GET  /api/v1/resources/humans/{operator_id}`` — get a single operator.
- ``DELETE /api/v1/resources/humans/{operator_id}`` — remove an operator.

- ``POST /api/v1/resources/robots`` — register a robot workstation.
- ``GET  /api/v1/resources/robots`` — list all registered robots.
- ``GET  /api/v1/resources/robots/{robot_id}`` — get a single robot.
- ``DELETE /api/v1/resources/robots/{robot_id}`` — remove a robot.

Resources are stored in-memory (module-level dicts). This mirrors the
changeover optimizer's singleton pattern — no DB table is needed for
resource registration in this iteration.
"""

from fastapi import APIRouter, HTTPException, status

from ate_cloud.schemas.resource import (
    OperatorCreate,
    OperatorListResponse,
    OperatorResponse,
    RobotCreate,
    RobotListResponse,
    RobotResponse,
)

router = APIRouter(prefix="/resources", tags=["resources"])

# ---------------------------------------------------------------------------
# In-memory storage (module-level singletons, same pattern as changeover)
# ---------------------------------------------------------------------------

_operators: dict[str, OperatorResponse] = {}
_robots: dict[str, RobotResponse] = {}


def _get_operator_store() -> dict[str, OperatorResponse]:
    """Return the in-memory operator store."""
    return _operators


def _get_robot_store() -> dict[str, RobotResponse]:
    """Return the in-memory robot store."""
    return _robots


# ---------------------------------------------------------------------------
# Operator endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/humans",
    response_model=OperatorResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_operator(
    operator_data: OperatorCreate,
) -> OperatorResponse:
    """Register a new human operator.

    Args:
        operator_data: Operator registration data (name, skills, capacity).

    Returns:
        OperatorResponse with generated ID and timestamps.

    Raises:
        HTTPException: 409 if an operator with the same name already exists.
    """
    store = _get_operator_store()

    # Check for duplicate name
    for existing in store.values():
        if existing.name == operator_data.name:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Operator with name '{operator_data.name}' already exists",
            )

    response = OperatorResponse(
        name=operator_data.name,
        skills=operator_data.skills,
        max_concurrent_tasks=operator_data.max_concurrent_tasks,
        available_from=operator_data.available_from,
        available_to=operator_data.available_to,
    )
    store[response.id] = response
    return response


@router.get("/humans", response_model=OperatorListResponse)
async def list_operators() -> OperatorListResponse:
    """List all registered human operators.

    Returns:
        OperatorListResponse with operators list and total count.
    """
    store = _get_operator_store()
    operators = list(store.values())
    return OperatorListResponse(operators=operators, total=len(operators))


@router.get("/humans/{operator_id}", response_model=OperatorResponse)
async def get_operator(operator_id: str) -> OperatorResponse:
    """Get a single human operator by ID.

    Args:
        operator_id: Unique operator identifier.

    Returns:
        OperatorResponse for the requested operator.

    Raises:
        HTTPException: 404 if the operator is not found.
    """
    store = _get_operator_store()
    operator = store.get(operator_id)
    if operator is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Operator '{operator_id}' not found",
        )
    return operator


@router.delete("/humans/{operator_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_operator(operator_id: str) -> None:
    """Remove a registered human operator.

    Args:
        operator_id: Unique operator identifier.

    Raises:
        HTTPException: 404 if the operator is not found.
    """
    store = _get_operator_store()
    if operator_id not in store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Operator '{operator_id}' not found",
        )
    del store[operator_id]


# ---------------------------------------------------------------------------
# Robot endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/robots",
    response_model=RobotResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_robot(
    robot_data: RobotCreate,
) -> RobotResponse:
    """Register a new robot workstation.

    Args:
        robot_data: Robot registration data (name, type, capabilities, speed).

    Returns:
        RobotResponse with generated ID and timestamps.

    Raises:
        HTTPException: 409 if a robot with the same name already exists.
    """
    store = _get_robot_store()

    for existing in store.values():
        if existing.name == robot_data.name:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Robot with name '{robot_data.name}' already exists",
            )

    response = RobotResponse(
        name=robot_data.name,
        robot_type=robot_data.robot_type,
        capabilities=robot_data.capabilities,
        speed=robot_data.speed,
        max_concurrent_tasks=robot_data.max_concurrent_tasks,
        available_from=robot_data.available_from,
        available_to=robot_data.available_to,
    )
    store[response.id] = response
    return response


@router.get("/robots", response_model=RobotListResponse)
async def list_robots() -> RobotListResponse:
    """List all registered robot workstations.

    Returns:
        RobotListResponse with robots list and total count.
    """
    store = _get_robot_store()
    robots = list(store.values())
    return RobotListResponse(robots=robots, total=len(robots))


@router.get("/robots/{robot_id}", response_model=RobotResponse)
async def get_robot(robot_id: str) -> RobotResponse:
    """Get a single robot workstation by ID.

    Args:
        robot_id: Unique robot identifier.

    Returns:
        RobotResponse for the requested robot.

    Raises:
        HTTPException: 404 if the robot is not found.
    """
    store = _get_robot_store()
    robot = store.get(robot_id)
    if robot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Robot '{robot_id}' not found",
        )
    return robot


@router.delete("/robots/{robot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_robot(robot_id: str) -> None:
    """Remove a registered robot workstation.

    Args:
        robot_id: Unique robot identifier.

    Raises:
        HTTPException: 404 if the robot is not found.
    """
    store = _get_robot_store()
    if robot_id not in store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Robot '{robot_id}' not found",
        )
    del store[robot_id]
