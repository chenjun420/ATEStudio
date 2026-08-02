"""Pydantic schemas for changeover configuration and optimization.

Defines request/response models for the product changeover API:
- ChangeoverCostCreate/Update: Register or update transition costs
- ChangeoverCostResponse: Single transition cost response
- ChangeoverMatrixResponse: Full cost matrix response
- OptimizeRequest: Sequence optimization request
- ChangeoverTransitionResponse: Single transition in a result
- OptimizeResponse: Optimization result with optimal sequence
"""

from pydantic import BaseModel, Field


class ChangeoverCostCreate(BaseModel):
    """Request body for registering a changeover cost.

    Attributes:
        cost: Resource/monetary cost of the transition (non-negative).
        time_minutes: Time required for the transition in minutes (non-negative).
    """

    cost: int = Field(..., ge=0, description="Transition cost (non-negative)")
    time_minutes: int = Field(0, ge=0, description="Transition time in minutes")


class ChangeoverCostResponse(BaseModel):
    """Response model for a single changeover cost entry.

    Attributes:
        from_product: Source product type.
        to_product: Target product type.
        cost: Transition cost.
        time_minutes: Transition time in minutes.
    """

    from_product: str
    to_product: str
    cost: int
    time_minutes: int


class ChangeoverMatrixEntry(BaseModel):
    """A single cell in the changeover matrix.

    Attributes:
        from_product: Source product type.
        to_product: Target product type.
        cost: Transition cost, or null if no transition registered.
        time_minutes: Transition time in minutes, or null if no transition.
    """

    from_product: str
    to_product: str
    cost: int | None = None
    time_minutes: int | None = None


class ChangeoverMatrixResponse(BaseModel):
    """Full changeover cost matrix.

    Attributes:
        products: List of all known product types.
        entries: Flat list of all matrix entries (including null transitions).
    """

    products: list[str] = Field(default_factory=list)
    entries: list[ChangeoverMatrixEntry] = Field(default_factory=list)


class OptimizeRequest(BaseModel):
    """Request body for sequence optimization.

    Attributes:
        products: List of product types to sequence.
        start_product: Optional constraint for the first product in the sequence.
        time_limit: Solver time limit in seconds (default 5.0).
    """

    products: list[str] = Field(..., min_length=1, description="Product types to sequence")
    start_product: str | None = Field(None, description="Force sequence to start with this product")
    time_limit: float = Field(5.0, gt=0, description="Solver time limit in seconds")


class ChangeoverTransitionResponse(BaseModel):
    """A single transition in an optimized sequence.

    Attributes:
        from_product: Source product type.
        to_product: Target product type.
        cost: Transition cost.
        time_minutes: Transition time in minutes.
    """

    from_product: str
    to_product: str
    cost: int
    time_minutes: int


class OptimizeResponse(BaseModel):
    """Response model for sequence optimization.

    Attributes:
        sequence: Optimal product ordering.
        total_cost: Sum of all transition costs.
        total_time_minutes: Sum of all transition times.
        transitions: Detailed list of transitions.
    """

    sequence: list[str]
    total_cost: int
    total_time_minutes: int
    transitions: list[ChangeoverTransitionResponse]
