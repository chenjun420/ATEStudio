"""Product changeover API endpoints.

Provides:
- GET  /api/v1/changeover/matrix              - Get the full changeover cost matrix
- PUT  /api/v1/changeover/{product_a}/{product_b} - Register or update a transition cost
- DELETE /api/v1/changeover/{product_a}/{product_b} - Remove a transition cost
- POST /api/v1/changeover/optimize            - Optimize a product sequence
- GET  /api/v1/changeover/products            - List all known product types

The changeover matrix is stored in-memory (no DB table). The optimizer
is a module-level singleton shared across requests within the same process.
"""

from fastapi import APIRouter, HTTPException, status

from ate_cloud.schemas.changeover import (
    ChangeoverCostCreate,
    ChangeoverCostResponse,
    ChangeoverMatrixEntry,
    ChangeoverMatrixResponse,
    ChangeoverTransitionResponse,
    OptimizeRequest,
    OptimizeResponse,
)
from ate_platform.scheduler.changeover_optimizer import (
    ChangeoverOptimizer,
    ChangeoverResult,
)

router = APIRouter(prefix="/changeover", tags=["changeover"])

# Module-level singleton optimizer instance.
# In-memory storage — no DB table needed for this task.
_optimizer = ChangeoverOptimizer()


def _get_optimizer() -> ChangeoverOptimizer:
    """Return the shared ChangeoverOptimizer singleton."""
    return _optimizer


@router.get("/matrix", response_model=ChangeoverMatrixResponse)
async def get_changeover_matrix() -> ChangeoverMatrixResponse:
    """Get the full changeover cost matrix.

    Returns:
        ChangeoverMatrixResponse with all known products and transition costs.
    """
    opt = _get_optimizer()
    raw_matrix = opt.get_changeover_matrix()
    products = opt.get_products()

    entries: list[ChangeoverMatrixEntry] = []
    for from_p in sorted(raw_matrix.keys()):
        row = raw_matrix[from_p]
        for to_p in sorted(row.keys()):
            cost_entry = row[to_p]
            entries.append(
                ChangeoverMatrixEntry(
                    from_product=from_p,
                    to_product=to_p,
                    cost=cost_entry.cost if cost_entry else None,
                    time_minutes=cost_entry.time_minutes if cost_entry else None,
                )
            )

    return ChangeoverMatrixResponse(products=products, entries=entries)


@router.get("/products", response_model=list[str])
async def get_products() -> list[str]:
    """List all known product types in the changeover matrix.

    Returns:
        Sorted list of product type identifiers.
    """
    return _get_optimizer().get_products()


@router.put(
    "/{product_a}/{product_b}",
    response_model=ChangeoverCostResponse,
    status_code=status.HTTP_201_CREATED,
)
async def set_changeover_cost(
    product_a: str,
    product_b: str,
    cost_data: ChangeoverCostCreate,
) -> ChangeoverCostResponse:
    """Register or update the transition cost from product_a to product_b.

    Args:
        product_a: Source product type (path parameter).
        product_b: Target product type (path parameter).
        cost_data: Transition cost and time.

    Returns:
        ChangeoverCostResponse with the registered cost.

    Raises:
        HTTPException: 400 if product_a == product_b or cost is invalid.
    """
    opt = _get_optimizer()
    try:
        opt.add_changeover_cost(
            product_a=product_a,
            product_b=product_b,
            cost=cost_data.cost,
            time_minutes=cost_data.time_minutes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    return ChangeoverCostResponse(
        from_product=product_a,
        to_product=product_b,
        cost=cost_data.cost,
        time_minutes=cost_data.time_minutes,
    )


@router.delete(
    "/{product_a}/{product_b}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_changeover_cost(
    product_a: str,
    product_b: str,
) -> None:
    """Remove a registered transition cost.

    Args:
        product_a: Source product type.
        product_b: Target product type.

    Raises:
        HTTPException: 404 if the transition cost is not registered.
    """
    opt = _get_optimizer()
    if not opt.remove_changeover_cost(product_a, product_b):
        raise HTTPException(
            status_code=404,
            detail=f"Changeover cost not found: {product_a}→{product_b}",
        )


@router.post("/optimize", response_model=OptimizeResponse)
async def optimize_sequence(
    request: OptimizeRequest,
) -> OptimizeResponse:
    """Optimize a product sequence to minimize total changeover cost.

    Args:
        request: Optimization request with product list and optional constraints.

    Returns:
        OptimizeResponse with the optimal sequence, total cost, and transitions.

    Raises:
        HTTPException: 400 if input is invalid or transitions are missing.
        HTTPException: 503 if the solver is unavailable or times out.
    """
    opt = _get_optimizer()

    try:
        result: ChangeoverResult | None = opt.optimize_sequence(
            products=request.products,
            start_product=request.start_product,
            time_limit=request.time_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    if result is None:
        raise HTTPException(
            status_code=503,
            detail="Solver unavailable or timed out — could not optimize sequence",
        )

    return OptimizeResponse(
        sequence=result.sequence,
        total_cost=result.total_cost,
        total_time_minutes=result.total_time_minutes,
        transitions=[
            ChangeoverTransitionResponse(
                from_product=t.from_product,
                to_product=t.to_product,
                cost=t.cost,
                time_minutes=t.time_minutes,
            )
            for t in result.transitions
        ],
    )
