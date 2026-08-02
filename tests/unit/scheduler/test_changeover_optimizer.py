"""Unit tests for ChangeoverOptimizer.

Tests cover:
- add_changeover_cost: registration, validation (identical products, negative cost)
- get_changeover_matrix: full matrix retrieval, empty matrix
- get_changeover_cost: single lookup, missing lookup
- get_products: product tracking
- remove_changeover_cost: removal, non-existent removal
- optimize_sequence: single product, two products, three products, start_product constraint
- optimize_sequence: validation errors (empty list, invalid start_product, missing transitions)
- compute_sequence_cost: cost evaluation for a given order
- CP-SAT integration: 2 product alternating scheduling minimizes changeover cost
"""

from __future__ import annotations

import pytest

from ate_platform.scheduler.changeover_optimizer import (
    ChangeoverOptimizer,
)

# ---------------------------------------------------------------------------
# Skip if OR-Tools is not installed
# ---------------------------------------------------------------------------
ortools_available = False
try:
    from ortools.sat.python import cp_model  # noqa: F401

    ortools_available = True
except ImportError:
    pass

pytestmark = pytest.mark.skipif(
    not ortools_available,
    reason="OR-Tools not installed",
)


class TestAddChangeoverCost:
    """Tests for add_changeover_cost()."""

    def test_add_cost_basic(self) -> None:
        """Given a valid transition, the cost is stored and retrievable."""
        opt = ChangeoverOptimizer()
        opt.add_changeover_cost("product_a", "product_b", cost=100, time_minutes=30)

        cost = opt.get_changeover_cost("product_a", "product_b")
        assert cost is not None
        assert cost.cost == 100
        assert cost.time_minutes == 30

    def test_add_cost_default_time(self) -> None:
        """Given no time_minutes, default is 0."""
        opt = ChangeoverOptimizer()
        opt.add_changeover_cost("a", "b", cost=50)

        cost = opt.get_changeover_cost("a", "b")
        assert cost is not None
        assert cost.time_minutes == 0

    def test_add_cost_identical_products_raises(self) -> None:
        """Given identical products, ValueError is raised."""
        opt = ChangeoverOptimizer()
        with pytest.raises(ValueError, match="identical products"):
            opt.add_changeover_cost("a", "a", cost=10)

    def test_add_cost_negative_cost_raises(self) -> None:
        """Given negative cost, ValueError is raised."""
        opt = ChangeoverOptimizer()
        with pytest.raises(ValueError, match="non-negative"):
            opt.add_changeover_cost("a", "b", cost=-1)

    def test_add_cost_negative_time_raises(self) -> None:
        """Given negative time, ValueError is raised."""
        opt = ChangeoverOptimizer()
        with pytest.raises(ValueError, match="non-negative"):
            opt.add_changeover_cost("a", "b", cost=10, time_minutes=-5)

    def test_add_cost_overwrites(self) -> None:
        """Given a second call for the same pair, the cost is overwritten."""
        opt = ChangeoverOptimizer()
        opt.add_changeover_cost("a", "b", cost=100, time_minutes=30)
        opt.add_changeover_cost("a", "b", cost=200, time_minutes=45)

        cost = opt.get_changeover_cost("a", "b")
        assert cost is not None
        assert cost.cost == 200
        assert cost.time_minutes == 45

    def test_add_cost_asymmetric(self) -> None:
        """Given A→B and B→A with different costs, both are stored."""
        opt = ChangeoverOptimizer()
        opt.add_changeover_cost("a", "b", cost=100)
        opt.add_changeover_cost("b", "a", cost=50)

        assert opt.get_changeover_cost("a", "b").cost == 100  # type: ignore[union-attr]
        assert opt.get_changeover_cost("b", "a").cost == 50  # type: ignore[union-attr]


class TestGetChangeoverMatrix:
    """Tests for get_changeover_matrix()."""

    def test_empty_matrix(self) -> None:
        """Given no costs, the matrix is empty."""
        opt = ChangeoverOptimizer()
        matrix = opt.get_changeover_matrix()
        assert matrix == {}

    def test_matrix_with_two_products(self) -> None:
        """Given two products with both directions, the matrix has 2x2 with None on diagonal."""
        opt = ChangeoverOptimizer()
        opt.add_changeover_cost("a", "b", cost=100, time_minutes=10)
        opt.add_changeover_cost("b", "a", cost=80, time_minutes=8)

        matrix = opt.get_changeover_matrix()
        assert sorted(matrix.keys()) == ["a", "b"]
        assert matrix["a"]["b"].cost == 100
        assert matrix["a"]["a"] is None
        assert matrix["b"]["a"].cost == 80
        assert matrix["b"]["b"] is None

    def test_matrix_with_missing_direction(self) -> None:
        """Given only A→B, the B→A cell is None."""
        opt = ChangeoverOptimizer()
        opt.add_changeover_cost("a", "b", cost=100)

        matrix = opt.get_changeover_matrix()
        assert matrix["a"]["b"] is not None
        assert matrix["b"]["a"] is None


class TestGetProducts:
    """Tests for get_products()."""

    def test_empty(self) -> None:
        """Given no costs, products list is empty."""
        opt = ChangeoverOptimizer()
        assert opt.get_products() == []

    def test_tracks_products(self) -> None:
        """Given multiple transitions, all products are tracked."""
        opt = ChangeoverOptimizer()
        opt.add_changeover_cost("c", "a", cost=10)
        opt.add_changeover_cost("a", "b", cost=20)

        assert opt.get_products() == ["a", "b", "c"]


class TestRemoveChangeoverCost:
    """Tests for remove_changeover_cost()."""

    def test_remove_existing(self) -> None:
        """Given an existing transition, removal returns True."""
        opt = ChangeoverOptimizer()
        opt.add_changeover_cost("a", "b", cost=100)
        assert opt.remove_changeover_cost("a", "b") is True
        assert opt.get_changeover_cost("a", "b") is None

    def test_remove_nonexistent(self) -> None:
        """Given a non-existent transition, removal returns False."""
        opt = ChangeoverOptimizer()
        assert opt.remove_changeover_cost("a", "b") is False


class TestOptimizeSequence:
    """Tests for optimize_sequence()."""

    def test_single_product(self) -> None:
        """Given a single product, the result has no transitions and zero cost."""
        opt = ChangeoverOptimizer()
        result = opt.optimize_sequence(["a"])
        assert result is not None
        assert result.sequence == ["a"]
        assert result.total_cost == 0
        assert result.total_time_minutes == 0
        assert result.transitions == []

    def test_two_products_minimizes_cost(self) -> None:
        """Given 2 products with asymmetric costs, the optimizer picks the cheaper direction."""
        opt = ChangeoverOptimizer()
        opt.add_changeover_cost("a", "b", cost=100, time_minutes=30)
        opt.add_changeover_cost("b", "a", cost=50, time_minutes=15)

        result = opt.optimize_sequence(["a", "b"])
        assert result is not None
        # The cheaper transition is b→a (50 < 100), so the optimal order is [b, a]
        assert result.sequence == ["b", "a"]
        assert result.total_cost == 50
        assert result.total_time_minutes == 15

    def test_three_products_finds_optimal(self) -> None:
        """Given 3 products, the optimizer finds the TSP-optimal ordering."""
        opt = ChangeoverOptimizer()
        # Create a clear cheapest path: a→b→c (cost 10+10=20)
        opt.add_changeover_cost("a", "b", cost=10, time_minutes=5)
        opt.add_changeover_cost("b", "c", cost=10, time_minutes=5)
        # All other directions are expensive
        opt.add_changeover_cost("a", "c", cost=100, time_minutes=50)
        opt.add_changeover_cost("b", "a", cost=100, time_minutes=50)
        opt.add_changeover_cost("c", "a", cost=100, time_minutes=50)
        opt.add_changeover_cost("c", "b", cost=100, time_minutes=50)

        result = opt.optimize_sequence(["a", "b", "c"])
        assert result is not None
        assert result.sequence == ["a", "b", "c"]
        assert result.total_cost == 20
        assert len(result.transitions) == 2

    def test_start_product_constraint(self) -> None:
        """Given a start_product constraint, the sequence starts with it."""
        opt = ChangeoverOptimizer()
        opt.add_changeover_cost("a", "b", cost=100, time_minutes=30)
        opt.add_changeover_cost("b", "a", cost=50, time_minutes=15)

        # Force start with 'a' even though b→a is cheaper
        result = opt.optimize_sequence(["a", "b"], start_product="a")
        assert result is not None
        assert result.sequence[0] == "a"
        assert result.total_cost == 100  # a→b

    def test_empty_list_raises(self) -> None:
        """Given an empty product list, ValueError is raised."""
        opt = ChangeoverOptimizer()
        with pytest.raises(ValueError, match="empty product list"):
            opt.optimize_sequence([])

    def test_invalid_start_product_raises(self) -> None:
        """Given a start_product not in the list, ValueError is raised."""
        opt = ChangeoverOptimizer()
        with pytest.raises(ValueError, match="start_product"):
            opt.optimize_sequence(["a", "b"], start_product="c")

    def test_missing_transition_raises(self) -> None:
        """Given missing transition costs, ValueError is raised."""
        opt = ChangeoverOptimizer()
        opt.add_changeover_cost("a", "b", cost=10)
        # Missing b→a transition

        with pytest.raises(ValueError, match="Missing changeover costs"):
            opt.optimize_sequence(["a", "b"])

    def test_result_transitions_are_correct(self) -> None:
        """Given an optimization result, transitions match the sequence."""
        opt = ChangeoverOptimizer()
        opt.add_changeover_cost("a", "b", cost=10, time_minutes=5)
        opt.add_changeover_cost("b", "a", cost=20, time_minutes=10)

        result = opt.optimize_sequence(["a", "b"])
        assert result is not None
        assert len(result.transitions) == 1
        t = result.transitions[0]
        assert t.from_product == result.sequence[0]
        assert t.to_product == result.sequence[1]
        assert t.cost == result.total_cost
        assert t.time_minutes == result.total_time_minutes


class TestComputeSequenceCost:
    """Tests for compute_sequence_cost() — non-optimizing evaluation."""

    def test_single_product(self) -> None:
        """Given a single product, cost is zero."""
        opt = ChangeoverOptimizer()
        result = opt.compute_sequence_cost(["a"])
        assert result.total_cost == 0
        assert result.transitions == []

    def test_two_products(self) -> None:
        """Given two products, the cost matches the registered transition."""
        opt = ChangeoverOptimizer()
        opt.add_changeover_cost("a", "b", cost=100, time_minutes=30)

        result = opt.compute_sequence_cost(["a", "b"])
        assert result.total_cost == 100
        assert result.total_time_minutes == 30

    def test_missing_transition_raises(self) -> None:
        """Given a missing transition, ValueError is raised."""
        opt = ChangeoverOptimizer()
        with pytest.raises(ValueError, match="Missing changeover cost"):
            opt.compute_sequence_cost(["a", "b"])

    def test_empty_sequence_raises(self) -> None:
        """Given an empty sequence, ValueError is raised."""
        opt = ChangeoverOptimizer()
        with pytest.raises(ValueError, match="empty sequence"):
            opt.compute_sequence_cost([])


class TestChangeoverOptimizationVerification:
    """Verification: 2-product alternating scheduling minimizes changeover cost.

    This validates the core requirement: given two products with asymmetric
    changeover costs, the optimizer selects the sequence with minimum total cost.
    """

    def test_two_product_alternating_scheduling(self) -> None:
        """Given 2 products alternating in production, the optimizer minimizes changeover cost.

        Scenario: Product A and Product B need to be produced. Transitioning
        A→B costs 120 (30 min), B→A costs 80 (20 min). The optimizer should
        choose B→A (cost 80) over A→B (cost 120), saving 40 cost units.
        """
        opt = ChangeoverOptimizer()
        opt.add_changeover_cost("product_a", "product_b", cost=120, time_minutes=30)
        opt.add_changeover_cost("product_b", "product_a", cost=80, time_minutes=20)

        # Optimize without start constraint
        result = opt.optimize_sequence(["product_a", "product_b"])
        assert result is not None

        # Verify the optimizer chose the cheaper direction
        assert result.sequence == ["product_b", "product_a"]
        assert result.total_cost == 80
        assert result.total_time_minutes == 20

        # Verify the alternative would have been more expensive
        alternative = opt.compute_sequence_cost(["product_a", "product_b"])
        assert alternative.total_cost == 120
        assert result.total_cost < alternative.total_cost

    def test_optimal_is_better_than_worst_permutation(self) -> None:
        """Given 3 products, the optimal is at least as good as any permutation."""
        opt = ChangeoverOptimizer()
        # Set up a triangle: a→b→c is cheapest
        opt.add_changeover_cost("a", "b", cost=5, time_minutes=2)
        opt.add_changeover_cost("b", "c", cost=5, time_minutes=2)
        opt.add_changeover_cost("a", "c", cost=50, time_minutes=20)
        opt.add_changeover_cost("b", "a", cost=50, time_minutes=20)
        opt.add_changeover_cost("c", "a", cost=50, time_minutes=20)
        opt.add_changeover_cost("c", "b", cost=50, time_minutes=20)

        result = opt.optimize_sequence(["a", "b", "c"])
        assert result is not None

        # Check that optimal is ≤ all other permutations
        permutations = [
            ["a", "c", "b"],
            ["b", "a", "c"],
            ["b", "c", "a"],
            ["c", "a", "b"],
            ["c", "b", "a"],
        ]
        for perm in permutations:
            perm_cost = opt.compute_sequence_cost(perm)
            assert result.total_cost <= perm_cost.total_cost, (
                f"Optimal cost {result.total_cost} should be <= {perm_cost.total_cost} for {perm}"
            )
