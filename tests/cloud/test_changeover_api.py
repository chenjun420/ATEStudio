"""API integration tests for product changeover endpoints.

Tests cover:
- PUT /api/v1/changeover/{product_a}/{product_b} — register/update costs
- GET /api/v1/changeover/matrix — retrieve full matrix
- GET /api/v1/changeover/products — list known products
- DELETE /api/v1/changeover/{product_a}/{product_b} — remove costs
- POST /api/v1/changeover/optimize — optimize a product sequence

The changeover optimizer is a module-level singleton. Tests reset its
state in a fixture to ensure isolation.
"""

from __future__ import annotations

import pytest

# Skip if OR-Tools is not installed
ortools_available = False
try:
    from ortools.sat.python import cp_model  # noqa: F401

    ortools_available = True
except ImportError:
    pass


@pytest.fixture(autouse=True)
def _reset_changeover_optimizer() -> None:
    """Reset the shared optimizer singleton before each test."""
    from ate_cloud.api.v1.changeover import _get_optimizer

    opt = _get_optimizer()
    opt._matrix.clear()  # type: ignore[attr-defined]
    opt._products.clear()  # type: ignore[attr-defined]
    yield
    # Clean up after test too
    opt._matrix.clear()  # type: ignore[attr-defined]
    opt._products.clear()  # type: ignore[attr-defined]


class TestSetChangeoverCost:
    """Tests for PUT /api/v1/changeover/{product_a}/{product_b}."""

    @pytest.mark.asyncio
    async def test_set_cost_creates_entry(self, client) -> None:
        """Test registering a new transition cost returns 201."""
        response = await client.put(
            "/api/v1/changeover/product_a/product_b",
            json={"cost": 100, "time_minutes": 30},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["from_product"] == "product_a"
        assert data["to_product"] == "product_b"
        assert data["cost"] == 100
        assert data["time_minutes"] == 30

    @pytest.mark.asyncio
    async def test_set_cost_default_time(self, client) -> None:
        """Test registering without time_minutes defaults to 0."""
        response = await client.put(
            "/api/v1/changeover/a/b",
            json={"cost": 50},
        )

        assert response.status_code == 201
        assert response.json()["time_minutes"] == 0

    @pytest.mark.asyncio
    async def test_set_cost_identical_products_returns_400(self, client) -> None:
        """Test that identical products returns 400."""
        response = await client.put(
            "/api/v1/changeover/a/a",
            json={"cost": 10, "time_minutes": 5},
        )

        assert response.status_code == 400
        assert "identical" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_set_cost_negative_cost_returns_422(self, client) -> None:
        """Test that negative cost returns 422 (Pydantic validation)."""
        response = await client.put(
            "/api/v1/changeover/a/b",
            json={"cost": -1, "time_minutes": 5},
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_set_cost_overwrites(self, client) -> None:
        """Test that re-registering overwrites the previous cost."""
        await client.put("/api/v1/changeover/a/b", json={"cost": 100, "time_minutes": 30})
        response = await client.put(
            "/api/v1/changeover/a/b",
            json={"cost": 200, "time_minutes": 45},
        )

        assert response.status_code == 201
        assert response.json()["cost"] == 200
        assert response.json()["time_minutes"] == 45


class TestGetChangeoverMatrix:
    """Tests for GET /api/v1/changeover/matrix."""

    @pytest.mark.asyncio
    async def test_empty_matrix(self, client) -> None:
        """Test that an empty matrix returns empty products and entries."""
        response = await client.get("/api/v1/changeover/matrix")

        assert response.status_code == 200
        data = response.json()
        assert data["products"] == []
        assert data["entries"] == []

    @pytest.mark.asyncio
    async def test_matrix_with_entries(self, client) -> None:
        """Test that a populated matrix returns all entries."""
        await client.put("/api/v1/changeover/a/b", json={"cost": 100, "time_minutes": 30})
        await client.put("/api/v1/changeover/b/a", json={"cost": 80, "time_minutes": 20})

        response = await client.get("/api/v1/changeover/matrix")

        assert response.status_code == 200
        data = response.json()
        assert sorted(data["products"]) == ["a", "b"]
        # Entries include diagonal (None) and registered transitions
        entries = {(e["from_product"], e["to_product"]): e for e in data["entries"]}
        assert entries[("a", "b")]["cost"] == 100
        assert entries[("a", "b")]["time_minutes"] == 30
        assert entries[("b", "a")]["cost"] == 80
        assert entries[("a", "a")]["cost"] is None
        assert entries[("b", "b")]["cost"] is None


class TestGetProducts:
    """Tests for GET /api/v1/changeover/products."""

    @pytest.mark.asyncio
    async def test_empty_products(self, client) -> None:
        """Test that no costs returns empty product list."""
        response = await client.get("/api/v1/changeover/products")

        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_products_after_registration(self, client) -> None:
        """Test that products are listed after cost registration."""
        await client.put("/api/v1/changeover/c/a", json={"cost": 10})
        await client.put("/api/v1/changeover/a/b", json={"cost": 20})

        response = await client.get("/api/v1/changeover/products")

        assert response.status_code == 200
        assert response.json() == ["a", "b", "c"]


class TestDeleteChangeoverCost:
    """Tests for DELETE /api/v1/changeover/{product_a}/{product_b}."""

    @pytest.mark.asyncio
    async def test_delete_existing(self, client) -> None:
        """Test deleting an existing transition returns 204."""
        await client.put("/api/v1/changeover/a/b", json={"cost": 100})

        response = await client.delete("/api/v1/changeover/a/b")

        assert response.status_code == 204

        # Verify it's gone
        matrix = (await client.get("/api/v1/changeover/matrix")).json()
        entries = {(e["from_product"], e["to_product"]): e for e in matrix["entries"]}
        assert entries.get(("a", "b"), {}).get("cost") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, client) -> None:
        """Test deleting a non-existent transition returns 404."""
        response = await client.delete("/api/v1/changeover/x/y")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


@pytest.mark.skipif(
    not ortools_available,
    reason="OR-Tools not installed",
)
class TestOptimizeSequence:
    """Tests for POST /api/v1/changeover/optimize."""

    @pytest.mark.asyncio
    async def test_optimize_single_product(self, client) -> None:
        """Test optimizing a single product returns zero cost."""
        response = await client.post(
            "/api/v1/changeover/optimize",
            json={"products": ["a"]},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["sequence"] == ["a"]
        assert data["total_cost"] == 0
        assert data["transitions"] == []

    @pytest.mark.asyncio
    async def test_optimize_two_products(self, client) -> None:
        """Test optimizing 2 products picks the cheaper direction."""
        await client.put("/api/v1/changeover/a/b", json={"cost": 100, "time_minutes": 30})
        await client.put("/api/v1/changeover/b/a", json={"cost": 50, "time_minutes": 15})

        response = await client.post(
            "/api/v1/changeover/optimize",
            json={"products": ["a", "b"]},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["sequence"] == ["b", "a"]
        assert data["total_cost"] == 50
        assert data["total_time_minutes"] == 15

    @pytest.mark.asyncio
    async def test_optimize_with_start_product(self, client) -> None:
        """Test that start_product constraint is respected."""
        await client.put("/api/v1/changeover/a/b", json={"cost": 100, "time_minutes": 30})
        await client.put("/api/v1/changeover/b/a", json={"cost": 50, "time_minutes": 15})

        response = await client.post(
            "/api/v1/changeover/optimize",
            json={"products": ["a", "b"], "start_product": "a"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["sequence"][0] == "a"

    @pytest.mark.asyncio
    async def test_optimize_missing_transitions_returns_400(self, client) -> None:
        """Test that missing transitions return 400."""
        await client.put("/api/v1/changeover/a/b", json={"cost": 100})
        # Missing b→a

        response = await client.post(
            "/api/v1/changeover/optimize",
            json={"products": ["a", "b"]},
        )

        assert response.status_code == 400
        assert "missing" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_optimize_empty_list_returns_400(self, client) -> None:
        """Test that an empty product list returns 400."""
        response = await client.post(
            "/api/v1/changeover/optimize",
            json={"products": []},
        )

        # Pydantic validation error (min_length=1)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_optimize_invalid_start_product_returns_400(self, client) -> None:
        """Test that an invalid start_product returns 400."""
        await client.put("/api/v1/changeover/a/b", json={"cost": 100})
        await client.put("/api/v1/changeover/b/a", json={"cost": 50})

        response = await client.post(
            "/api/v1/changeover/optimize",
            json={"products": ["a", "b"], "start_product": "c"},
        )

        assert response.status_code == 400
        assert "start_product" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_optimize_returns_transitions(self, client) -> None:
        """Test that the optimize response includes detailed transitions."""
        await client.put("/api/v1/changeover/a/b", json={"cost": 10, "time_minutes": 5})
        await client.put("/api/v1/changeover/b/a", json={"cost": 20, "time_minutes": 10})

        response = await client.post(
            "/api/v1/changeover/optimize",
            json={"products": ["a", "b"]},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["transitions"]) == 1
        t = data["transitions"][0]
        assert t["from_product"] == data["sequence"][0]
        assert t["to_product"] == data["sequence"][1]
        assert t["cost"] == data["total_cost"]
