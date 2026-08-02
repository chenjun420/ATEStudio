# mypy: ignore-errors
"""Changeover optimizer for flexible production line scheduling.

Stores a matrix of transition costs (time + resource reset cost) between
product types and finds the optimal production sequence that minimizes
total changeover cost.

In flexible production lines, switching from product A to product B incurs:
- Time cost (minutes for reconfiguring instruments, fixtures, etc.)
- Resource cost (calibration resets, fixture swaps, software reloads)

The optimizer uses a TSP-like formulation via OR-Tools CP-SAT to find
the sequence of product batches that minimizes total changeover cost.

Usage:
    from ate_platform.scheduler.changeover_optimizer import ChangeoverOptimizer

    optimizer = ChangeoverOptimizer()
    optimizer.add_changeover_cost("product_a", "product_b", cost=100, time_minutes=30)
    optimizer.add_changeover_cost("product_b", "product_a", cost=80, time_minutes=25)

    result = optimizer.optimize_sequence(["product_a", "product_b", "product_a"])
    # result: ChangeoverResult with optimal order, total_cost, total_time, transitions
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OR-Tools availability sentinel (mirrors cpsat.py pattern)
# ---------------------------------------------------------------------------
_ORTOOLS_AVAILABLE: bool = False
_cp_model: Any = None
_ORTOOLS_IMPORT_ERROR: str | None = None

try:
    from ortools.sat.python import cp_model

    _ORTOOLS_AVAILABLE = True
except ImportError as exc:
    _ORTOOLS_IMPORT_ERROR = str(exc)

_OPTIMAL_OR_FEASIBLE = {cp_model.OPTIMAL, cp_model.FEASIBLE} if _ORTOOLS_AVAILABLE else set()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChangeoverCost:
    """Transition cost between two product types.

    Attributes:
        from_product: Source product type.
        to_product: Target product type.
        cost: Resource/monetary cost of the transition.
        time_minutes: Time required for the transition in minutes.
    """

    from_product: str
    to_product: str
    cost: int
    time_minutes: int


@dataclass(frozen=True, slots=True)
class ChangeoverTransition:
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


@dataclass(frozen=True, slots=True)
class ChangeoverResult:
    """Result of sequence optimization.

    Attributes:
        sequence: Optimal product ordering (list of product types).
        total_cost: Sum of all transition costs.
        total_time_minutes: Sum of all transition times.
        transitions: Detailed list of transitions in the optimized sequence.
    """

    sequence: list[str]
    total_cost: int
    total_time_minutes: int
    transitions: list[ChangeoverTransition]


# ---------------------------------------------------------------------------
# ChangeoverOptimizer
# ---------------------------------------------------------------------------


class ChangeoverOptimizer:
    """Optimizer for product changeover sequencing in flexible production.

    Stores an asymmetric cost matrix between product types and finds the
    optimal production sequence minimizing total changeover cost using
    CP-SAT constraint programming.

    The cost matrix is asymmetric: transitioning A→B may have different
    cost/time than B→A (e.g., disassembly is faster than assembly).

    The optimizer treats this as a TSP-like problem: given a set of product
    types to produce, find the ordering that minimizes the sum of transition
    costs between consecutive products.
    """

    def __init__(self) -> None:
        """Initialize an empty changeover optimizer."""
        # Matrix: {(from_product, to_product): ChangeoverCost}
        self._matrix: dict[tuple[str, str], ChangeoverCost] = {}
        # Track all known product types
        self._products: set[str] = set()

    # ------------------------------------------------------------------
    # Public API -- cost matrix management
    # ------------------------------------------------------------------

    def add_changeover_cost(
        self,
        product_a: str,
        product_b: str,
        cost: int,
        time_minutes: int = 0,
    ) -> None:
        """Register the transition cost from product_a to product_b.

        Args:
            product_a: Source product type.
            product_b: Target product type.
            cost: Resource/monetary cost of transitioning A→B (non-negative).
            time_minutes: Time required for the transition in minutes.

        Raises:
            ValueError: If product_a == product_b, or cost is negative,
                or time_minutes is negative.
        """
        if product_a == product_b:
            raise ValueError(
                f"Cannot add changeover cost between identical products: '{product_a}'"
            )
        if cost < 0:
            raise ValueError(f"Cost must be non-negative, got {cost}")
        if time_minutes < 0:
            raise ValueError(f"time_minutes must be non-negative, got {time_minutes}")

        self._matrix[(product_a, product_b)] = ChangeoverCost(
            from_product=product_a,
            to_product=product_b,
            cost=cost,
            time_minutes=time_minutes,
        )
        self._products.add(product_a)
        self._products.add(product_b)
        logger.debug(
            "Registered changeover %s→%s: cost=%d, time=%dm",
            product_a,
            product_b,
            cost,
            time_minutes,
        )

    def get_changeover_cost(self, product_a: str, product_b: str) -> ChangeoverCost | None:
        """Get the transition cost from product_a to product_b.

        Args:
            product_a: Source product type.
            product_b: Target product type.

        Returns:
            ChangeoverCost if registered, None otherwise.
        """
        return self._matrix.get((product_a, product_b))

    def get_changeover_matrix(self) -> dict[str, dict[str, ChangeoverCost | None]]:
        """Return the full changeover cost matrix as a nested dict.

        Returns:
            Nested dict: matrix[from][to] = ChangeoverCost | None.
            Products with no registered transition have None.
        """
        products = sorted(self._products)
        matrix: dict[str, dict[str, ChangeoverCost | None]] = {}
        for from_p in products:
            row: dict[str, ChangeoverCost | None] = {}
            for to_p in products:
                if from_p == to_p:
                    row[to_p] = None
                else:
                    row[to_p] = self._matrix.get((from_p, to_p))
            matrix[from_p] = row
        return matrix

    def get_products(self) -> list[str]:
        """Return all known product types.

        Returns:
            Sorted list of product type identifiers.
        """
        return sorted(self._products)

    def remove_changeover_cost(self, product_a: str, product_b: str) -> bool:
        """Remove a registered transition cost.

        Args:
            product_a: Source product type.
            product_b: Target product type.

        Returns:
            True if the cost was removed, False if it was not registered.
        """
        return self._matrix.pop((product_a, product_b), None) is not None

    # ------------------------------------------------------------------
    # Public API -- sequence optimization
    # ------------------------------------------------------------------

    def optimize_sequence(
        self,
        products: list[str],
        start_product: str | None = None,
        time_limit: float = 5.0,
    ) -> ChangeoverResult | None:
        """Find the optimal production sequence minimizing total changeover cost.

        Uses CP-SAT to solve a TSP-like formulation: find the ordering of
        the given products that minimizes the sum of transition costs
        between consecutive products.

        Args:
            products: List of product types to sequence. Duplicates are
                allowed (e.g., producing the same product in multiple batches).
            start_product: If specified, the sequence must start with this
                product. If None, the optimizer chooses the best starting point.
            time_limit: Solver time limit in seconds.

        Returns:
            ChangeoverResult with optimal sequence and costs, or None if
            OR-Tools is unavailable, the solver times out, or the input
            is invalid (e.g., missing transition costs).

        Raises:
            ValueError: If products list is empty, or start_product is not
                in the products list, or any required transition cost is
                missing from the matrix.
        """
        if not products:
            raise ValueError("Cannot optimize an empty product list")
        if start_product is not None and start_product not in products:
            raise ValueError(
                f"start_product '{start_product}' not in products list: {products}"
            )

        # Single product — no transitions
        if len(products) == 1:
            return ChangeoverResult(
                sequence=list(products),
                total_cost=0,
                total_time_minutes=0,
                transitions=[],
            )

        # Validate all required transition costs exist
        unique_products = list(dict.fromkeys(products))  # preserve order, dedup
        missing_transitions: list[tuple[str, str]] = []
        for from_p in unique_products:
            for to_p in unique_products:
                if from_p != to_p and (from_p, to_p) not in self._matrix:
                    missing_transitions.append((from_p, to_p))

        if missing_transitions:
            raise ValueError(
                f"Missing changeover costs for transitions: {missing_transitions}"
            )

        if not _ORTOOLS_AVAILABLE:
            logger.warning(
                "OR-Tools not available: %s — ChangeoverOptimizer cannot solve",
                _ORTOOLS_IMPORT_ERROR,
            )
            return None

        result = self._solve_cpsat(products, start_product, time_limit)
        return result

    # ------------------------------------------------------------------
    # CP-SAT solver
    # ------------------------------------------------------------------

    def _solve_cpsat(
        self,
        products: list[str],
        start_product: str | None,
        time_limit: float,
    ) -> ChangeoverResult | None:
        """Solve the TSP-like changeover minimization via CP-SAT.

        Formulation:
        - For each position i (0..n-1), create an IntVar for the product
          index at that position.
        - Add AllDifferent constraint.
        - If start_product is specified, fix position 0.
        - Transition cost: for each pair of consecutive positions, add
          the changeover cost based on the product pair.
        - Objective: minimize sum of transition costs.
        """
        n = len(products)
        model = cp_model.CpModel()

        # Product index mapping
        product_list = list(products)
        product_to_idx = {p: i for i, p in enumerate(product_list)}
        idx_to_product = dict(enumerate(product_list))

        # position[i] = index of the product at position i in the sequence
        position: list[Any] = []
        for i in range(n):
            pos = model.NewIntVar(0, n - 1, f"pos_{i}")
            position.append(pos)

        # AllDifferent: each product appears exactly once
        model.AddAllDifferent(position)

        # Fix start product if specified
        if start_product is not None:
            model.Add(position[0] == product_to_idx[start_product])

        # Transition costs between consecutive positions
        # For each pair of consecutive positions (i, i+1), we need:
        #   transition_cost[i] = cost(position[i], position[i+1])
        # This is a routing cost — use element constraints via BoolVars.

        transition_cost_vars: list[Any] = []
        transition_time_vars: list[Any] = []

        for i in range(n - 1):
            # For each possible (from, to) pair, create a BoolVar
            # and use it to conditionally add the transition cost
            pair_vars: list[Any] = []
            cost_terms: list[tuple[Any, int]] = []
            time_terms: list[tuple[Any, int]] = []

            for from_idx in range(n):
                for to_idx in range(n):
                    if from_idx == to_idx:
                        continue
                    from_p = idx_to_product[from_idx]
                    to_p = idx_to_product[to_idx]
                    cost_entry = self._matrix.get((from_p, to_p))
                    if cost_entry is None:
                        continue

                    # b = 1 iff position[i] == from_idx AND position[i+1] == to_idx
                    b = model.NewBoolVar(f"b_{i}_{from_idx}_{to_idx}")

                    # position[i] == from_idx AND position[i+1] == to_idx => b = 1
                    # Use channeling constraints
                    is_from = model.NewBoolVar(f"from_{i}_{from_idx}")
                    is_to = model.NewBoolVar(f"to_{i}_{to_idx}")

                    model.Add(position[i] == from_idx).OnlyEnforceIf(is_from)
                    model.Add(position[i] != from_idx).OnlyEnforceIf(is_from.Not())

                    model.Add(position[i + 1] == to_idx).OnlyEnforceIf(is_to)
                    model.Add(position[i + 1] != to_idx).OnlyEnforceIf(is_to.Not())

                    # b = is_from AND is_to
                    model.AddBoolAnd([is_from, is_to]).OnlyEnforceIf(b)
                    model.AddBoolOr([is_from.Not(), is_to.Not()]).OnlyEnforceIf(b.Not())

                    pair_vars.append(b)
                    cost_terms.append((b, cost_entry.cost))
                    time_terms.append((b, cost_entry.time_minutes))

            # Exactly one transition must be active for this position pair
            if pair_vars:
                model.AddExactlyOne(pair_vars)

                # Transition cost for position i
                tc = model.NewIntVar(0, sum(c for _, c in cost_terms), f"tc_{i}")
                model.Add(tc == sum(b * c for b, c in cost_terms))
                transition_cost_vars.append(tc)

                # Transition time for position i
                tt = model.NewIntVar(
                    0, sum(t for _, t in time_terms), f"tt_{i}"
                )
                model.Add(tt == sum(b * t for b, t in time_terms))
                transition_time_vars.append(tt)
            else:
                # No transitions possible (shouldn't happen for valid input)
                tc = model.NewIntVar(0, 0, f"tc_{i}")
                tt = model.NewIntVar(0, 0, f"tt_{i}")
                transition_cost_vars.append(tc)
                transition_time_vars.append(tt)

        # Total cost
        max_total_cost = sum(c.cost for c in self._matrix.values())
        total_cost = model.NewIntVar(0, max_total_cost, "total_cost")
        model.Add(total_cost == sum(transition_cost_vars))

        # Total time
        max_total_time = sum(c.time_minutes for c in self._matrix.values())
        total_time = model.NewIntVar(0, max_total_time, "total_time")
        model.Add(total_time == sum(transition_time_vars))

        # Objective: minimize total changeover cost
        model.Minimize(total_cost)

        # Solve
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = time_limit
        solver.parameters.log_search_progress = False

        status = solver.Solve(model)

        if status not in _OPTIMAL_OR_FEASIBLE:
            logger.debug("CP-SAT solver status %s — returning None", status)
            return None

        # Extract solution
        sequence: list[str] = []
        for i in range(n):
            idx = solver.Value(position[i])
            sequence.append(idx_to_product[idx])

        # Build transitions
        transitions: list[ChangeoverTransition] = []
        for i in range(n - 1):
            from_p = sequence[i]
            to_p = sequence[i + 1]
            cost_entry = self._matrix[(from_p, to_p)]
            transitions.append(
                ChangeoverTransition(
                    from_product=from_p,
                    to_product=to_p,
                    cost=cost_entry.cost,
                    time_minutes=cost_entry.time_minutes,
                )
            )

        return ChangeoverResult(
            sequence=sequence,
            total_cost=solver.Value(total_cost),
            total_time_minutes=solver.Value(total_time),
            transitions=transitions,
        )

    # ------------------------------------------------------------------
    # Convenience: compute cost of a given sequence (no optimization)
    # ------------------------------------------------------------------

    def compute_sequence_cost(self, sequence: list[str]) -> ChangeoverResult:
        """Compute the total changeover cost for a given product sequence.

        This does NOT optimize — it evaluates the cost of the given ordering.
        Useful for comparison with the optimal solution.

        Args:
            sequence: Ordered list of product types.

        Returns:
            ChangeoverResult with the given sequence and its costs.

        Raises:
            ValueError: If sequence has < 1 product, or any transition cost
                is missing from the matrix.
        """
        if not sequence:
            raise ValueError("Cannot compute cost of empty sequence")

        transitions: list[ChangeoverTransition] = []
        total_cost = 0
        total_time = 0

        for i in range(len(sequence) - 1):
            from_p = sequence[i]
            to_p = sequence[i + 1]
            cost_entry = self._matrix.get((from_p, to_p))
            if cost_entry is None:
                raise ValueError(
                    f"Missing changeover cost for transition: {from_p}→{to_p}"
                )
            transitions.append(
                ChangeoverTransition(
                    from_product=from_p,
                    to_product=to_p,
                    cost=cost_entry.cost,
                    time_minutes=cost_entry.time_minutes,
                )
            )
            total_cost += cost_entry.cost
            total_time += cost_entry.time_minutes

        return ChangeoverResult(
            sequence=list(sequence),
            total_cost=total_cost,
            total_time_minutes=total_time,
            transitions=transitions,
        )
