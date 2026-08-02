"""Pydantic schemas for human and robot resource management.

Defines request/response models for the resource allocation API:
- OperatorCreate / OperatorResponse — human operator with skill matrix and availability
- RobotCreate / RobotResponse — robot workstation with type, capabilities, speed, status
- OperatorListResponse / RobotListResponse — list responses with total count

Operators and robots are modelled as CP-SAT cumulative resources in the
scheduler. The skill matrix maps skill names to proficiency levels; the
availability windows define when each resource is active.
"""

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class OperatorCreate(BaseModel):
    """Request body for registering a human operator.

    Attributes:
        name: Human-readable operator name.
        skills: List of skill identifiers this operator possesses
            (e.g., ``["soldering", "rf_calibration"]``).
        max_concurrent_tasks: Maximum tasks the operator can handle
            simultaneously (capacity for the cumulative constraint).
        available_from: Earliest time unit the operator is available.
        available_to: Latest time unit the operator is available.
    """

    name: str = Field(..., min_length=1, max_length=255)
    skills: list[str] = Field(default_factory=list)
    max_concurrent_tasks: int = Field(1, ge=1, le=10)
    available_from: int = Field(0, ge=0)
    available_to: int = Field(10000, ge=1)


class OperatorResponse(BaseModel):
    """Response model for a registered human operator.

    Attributes:
        id: Unique operator identifier (UUID).
        name: Human-readable operator name.
        skills: List of skill identifiers.
        max_concurrent_tasks: Maximum concurrent task capacity.
        available_from: Earliest available time unit.
        available_to: Latest available time unit.
        status: Current operator status (``available`` / ``busy``).
        assigned_task_ids: List of task IDs currently assigned.
        created_at: Timestamp of creation.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    skills: list[str]
    max_concurrent_tasks: int
    available_from: int
    available_to: int
    status: str = "available"
    assigned_task_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = {"from_attributes": True}


class OperatorListResponse(BaseModel):
    """Response listing all registered operators.

    Attributes:
        operators: List of registered operators.
        total: Number of operators in the list.
    """

    operators: list[OperatorResponse]
    total: int


class RobotCreate(BaseModel):
    """Request body for registering a robot workstation.

    Attributes:
        name: Human-readable robot name.
        robot_type: Robot type identifier (e.g., ``"pick_place"``,
            ``"handler"``, ``"tester"``).
        capabilities: List of capability tags this robot supports
            (e.g., ``["pick", "place", "scan"]``).
        speed: Speed factor (1 = normal, 2 = double speed, etc.).
        max_concurrent_tasks: Maximum concurrent task capacity.
        available_from: Earliest time unit the robot is available.
        available_to: Latest time unit the robot is available.
    """

    name: str = Field(..., min_length=1, max_length=255)
    robot_type: str = Field(..., min_length=1, max_length=255)
    capabilities: list[str] = Field(default_factory=list)
    speed: float = Field(1.0, gt=0, le=10.0)
    max_concurrent_tasks: int = Field(1, ge=1, le=10)
    available_from: int = Field(0, ge=0)
    available_to: int = Field(10000, ge=1)

    @field_validator("speed")
    @classmethod
    def validate_speed(cls, v: float) -> float:
        """Ensure speed is a positive finite number."""
        if v <= 0:
            raise ValueError("speed must be positive")
        return v


class RobotResponse(BaseModel):
    """Response model for a registered robot workstation.

    Attributes:
        id: Unique robot identifier (UUID).
        name: Human-readable robot name.
        robot_type: Robot type identifier.
        capabilities: List of capability tags.
        speed: Speed factor.
        max_concurrent_tasks: Maximum concurrent task capacity.
        available_from: Earliest available time unit.
        available_to: Latest available time unit.
        status: Current robot status (``available`` / ``busy`` / ``maintenance``).
        assigned_task_ids: List of task IDs currently assigned.
        created_at: Timestamp of creation.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    robot_type: str
    capabilities: list[str]
    speed: float
    max_concurrent_tasks: int
    available_from: int
    available_to: int
    status: str = "available"
    assigned_task_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = {"from_attributes": True}


class RobotListResponse(BaseModel):
    """Response listing all registered robots.

    Attributes:
        robots: List of registered robots.
        total: Number of robots in the list.
    """

    robots: list[RobotResponse]
    total: int
