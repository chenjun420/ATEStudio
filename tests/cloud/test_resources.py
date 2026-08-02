"""API integration tests for resource management endpoints.

Tests cover:
- POST /api/v1/resources/humans — create operator (201, 409 duplicate)
- GET /api/v1/resources/humans — list operators (200, empty, with items)
- GET /api/v1/resources/humans/{id} — get operator (200, 404)
- DELETE /api/v1/resources/humans/{id} — delete operator (204, 404)
- POST /api/v1/resources/robots — create robot (201, 409 duplicate)
- GET /api/v1/resources/robots — list robots (200, empty, with items)
- GET /api/v1/resources/robots/{id} — get robot (200, 404)
- DELETE /api/v1/resources/robots/{id} — delete robot (204, 404)
- Schema validation — invalid data returns 422
- Cross-resource isolation — operators and robots are independent stores
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Fixtures: clean in-memory stores before each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_resource_stores() -> None:
    """Clear in-memory operator and robot stores before each test."""
    from ate_cloud.api.v1.resources import _get_operator_store, _get_robot_store

    _get_operator_store().clear()
    _get_robot_store().clear()
    yield
    _get_operator_store().clear()
    _get_robot_store().clear()


# ---------------------------------------------------------------------------
# Helper data
# ---------------------------------------------------------------------------


def _operator_data(name: str = "Alice") -> dict[str, object]:
    """Return sample operator data for testing."""
    return {
        "name": name,
        "skills": ["soldering", "rf_calibration"],
        "max_concurrent_tasks": 2,
        "available_from": 0,
        "available_to": 480,
    }


def _robot_data(name: str = "Handler-01") -> dict[str, object]:
    """Return sample robot data for testing."""
    return {
        "name": name,
        "robot_type": "handler",
        "capabilities": ["pick", "place", "scan"],
        "speed": 1.5,
        "max_concurrent_tasks": 1,
        "available_from": 0,
        "available_to": 480,
    }


# ---------------------------------------------------------------------------
# Tests: Operator CRUD
# ---------------------------------------------------------------------------


class TestCreateOperator:
    """Tests for POST /api/v1/resources/humans."""

    @pytest.mark.asyncio
    async def test_create_operator_returns_201(self, client) -> None:
        """Given: valid operator data. When: POST /resources/humans.
        Then: returns 201 with created operator."""
        response = await client.post(
            "/api/v1/resources/humans",
            json=_operator_data(),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Alice"
        assert data["skills"] == ["soldering", "rf_calibration"]
        assert data["max_concurrent_tasks"] == 2
        assert data["available_from"] == 0
        assert data["available_to"] == 480
        assert data["status"] == "available"
        assert data["assigned_task_ids"] == []
        assert "id" in data
        assert "created_at" in data

    @pytest.mark.asyncio
    async def test_create_duplicate_name_returns_409(self, client) -> None:
        """Given: existing operator named 'Alice'. When: POST same name.
        Then: returns 409 Conflict."""
        await client.post(
            "/api/v1/resources/humans",
            json=_operator_data("Alice"),
        )
        response = await client.post(
            "/api/v1/resources/humans",
            json=_operator_data("Alice"),
        )
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_operator_minimal(self, client) -> None:
        """Given: only required field (name). When: POST.
        Then: returns 201 with defaults."""
        response = await client.post(
            "/api/v1/resources/humans",
            json={"name": "Bob"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Bob"
        assert data["skills"] == []
        assert data["max_concurrent_tasks"] == 1

    @pytest.mark.asyncio
    async def test_create_operator_invalid_data_422(self, client) -> None:
        """Given: empty name. When: POST. Then: returns 422."""
        response = await client.post(
            "/api/v1/resources/humans",
            json={"name": ""},
        )
        assert response.status_code == 422


class TestListOperators:
    """Tests for GET /api/v1/resources/humans."""

    @pytest.mark.asyncio
    async def test_list_empty_returns_200(self, client) -> None:
        """Given: no operators. When: GET /resources/humans.
        Then: returns 200 with empty list."""
        response = await client.get("/api/v1/resources/humans")
        assert response.status_code == 200
        data = response.json()
        assert data["operators"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_with_operators(self, client) -> None:
        """Given: 2 operators. When: GET /resources/humans.
        Then: returns 200 with both operators."""
        await client.post(
            "/api/v1/resources/humans",
            json=_operator_data("Alice"),
        )
        await client.post(
            "/api/v1/resources/humans",
            json=_operator_data("Bob"),
        )
        response = await client.get("/api/v1/resources/humans")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        names = {op["name"] for op in data["operators"]}
        assert names == {"Alice", "Bob"}


class TestGetOperator:
    """Tests for GET /api/v1/resources/humans/{operator_id}."""

    @pytest.mark.asyncio
    async def test_get_operator_returns_200(self, client) -> None:
        """Given: existing operator. When: GET by id.
        Then: returns 200 with operator data."""
        create_resp = await client.post(
            "/api/v1/resources/humans",
            json=_operator_data(),
        )
        op_id = create_resp.json()["id"]
        response = await client.get(f"/api/v1/resources/humans/{op_id}")
        assert response.status_code == 200
        assert response.json()["name"] == "Alice"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_404(self, client) -> None:
        """Given: no operator with given id. When: GET.
        Then: returns 404."""
        response = await client.get(
            "/api/v1/resources/humans/nonexistent-id"
        )
        assert response.status_code == 404


class TestDeleteOperator:
    """Tests for DELETE /api/v1/resources/humans/{operator_id}."""

    @pytest.mark.asyncio
    async def test_delete_operator_returns_204(self, client) -> None:
        """Given: existing operator. When: DELETE.
        Then: returns 204, operator removed."""
        create_resp = await client.post(
            "/api/v1/resources/humans",
            json=_operator_data(),
        )
        op_id = create_resp.json()["id"]
        response = await client.delete(f"/api/v1/resources/humans/{op_id}")
        assert response.status_code == 204
        # Verify it's gone
        get_resp = await client.get(f"/api/v1/resources/humans/{op_id}")
        assert get_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, client) -> None:
        """Given: no operator with given id. When: DELETE.
        Then: returns 404."""
        response = await client.delete(
            "/api/v1/resources/humans/nonexistent-id"
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Robot CRUD
# ---------------------------------------------------------------------------


class TestCreateRobot:
    """Tests for POST /api/v1/resources/robots."""

    @pytest.mark.asyncio
    async def test_create_robot_returns_201(self, client) -> None:
        """Given: valid robot data. When: POST /resources/robots.
        Then: returns 201 with created robot."""
        response = await client.post(
            "/api/v1/resources/robots",
            json=_robot_data(),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Handler-01"
        assert data["robot_type"] == "handler"
        assert data["capabilities"] == ["pick", "place", "scan"]
        assert data["speed"] == 1.5
        assert data["max_concurrent_tasks"] == 1
        assert data["status"] == "available"
        assert "id" in data
        assert "created_at" in data

    @pytest.mark.asyncio
    async def test_create_duplicate_robot_name_409(self, client) -> None:
        """Given: existing robot named 'Handler-01'. When: POST same name.
        Then: returns 409."""
        await client.post(
            "/api/v1/resources/robots",
            json=_robot_data("Handler-01"),
        )
        response = await client.post(
            "/api/v1/resources/robots",
            json=_robot_data("Handler-01"),
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_create_robot_invalid_speed_422(self, client) -> None:
        """Given: speed=0 (invalid). When: POST. Then: returns 422."""
        response = await client.post(
            "/api/v1/resources/robots",
            json={"name": "Bad-Robot", "robot_type": "handler", "speed": 0},
        )
        assert response.status_code == 422


class TestListRobots:
    """Tests for GET /api/v1/resources/robots."""

    @pytest.mark.asyncio
    async def test_list_robots_empty(self, client) -> None:
        """Given: no robots. When: GET /resources/robots.
        Then: returns 200 with empty list."""
        response = await client.get("/api/v1/resources/robots")
        assert response.status_code == 200
        data = response.json()
        assert data["robots"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_robots_with_items(self, client) -> None:
        """Given: 2 robots. When: GET. Then: returns both."""
        await client.post(
            "/api/v1/resources/robots",
            json=_robot_data("Robot-A"),
        )
        await client.post(
            "/api/v1/resources/robots",
            json=_robot_data("Robot-B"),
        )
        response = await client.get("/api/v1/resources/robots")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2


class TestGetRobot:
    """Tests for GET /api/v1/resources/robots/{robot_id}."""

    @pytest.mark.asyncio
    async def test_get_robot_returns_200(self, client) -> None:
        """Given: existing robot. When: GET by id.
        Then: returns 200 with robot data."""
        create_resp = await client.post(
            "/api/v1/resources/robots",
            json=_robot_data(),
        )
        rb_id = create_resp.json()["id"]
        response = await client.get(f"/api/v1/resources/robots/{rb_id}")
        assert response.status_code == 200
        assert response.json()["name"] == "Handler-01"

    @pytest.mark.asyncio
    async def test_get_robot_nonexistent_404(self, client) -> None:
        """Given: no robot. When: GET. Then: returns 404."""
        response = await client.get("/api/v1/resources/robots/no-such-id")
        assert response.status_code == 404


class TestDeleteRobot:
    """Tests for DELETE /api/v1/resources/robots/{robot_id}."""

    @pytest.mark.asyncio
    async def test_delete_robot_returns_204(self, client) -> None:
        """Given: existing robot. When: DELETE. Then: returns 204."""
        create_resp = await client.post(
            "/api/v1/resources/robots",
            json=_robot_data(),
        )
        rb_id = create_resp.json()["id"]
        response = await client.delete(f"/api/v1/resources/robots/{rb_id}")
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_robot_nonexistent_404(self, client) -> None:
        """Given: no robot. When: DELETE. Then: returns 404."""
        response = await client.delete("/api/v1/resources/robots/no-id")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Tests: cross-resource isolation
# ---------------------------------------------------------------------------


class TestCrossResourceIsolation:
    """Operators and robots are independent stores."""

    @pytest.mark.asyncio
    async def test_operator_does_not_appear_in_robots(self, client) -> None:
        """Given: 1 operator, 0 robots. When: GET both lists.
        Then: operators list has 1, robots list has 0."""
        await client.post(
            "/api/v1/resources/humans",
            json=_operator_data(),
        )
        ops = (await client.get("/api/v1/resources/humans")).json()
        rbs = (await client.get("/api/v1/resources/robots")).json()
        assert ops["total"] == 1
        assert rbs["total"] == 0

    @pytest.mark.asyncio
    async def test_robot_does_not_appear_in_operators(self, client) -> None:
        """Given: 0 operators, 1 robot. When: GET both lists.
        Then: operators list has 0, robots list has 1."""
        await client.post(
            "/api/v1/resources/robots",
            json=_robot_data(),
        )
        ops = (await client.get("/api/v1/resources/humans")).json()
        rbs = (await client.get("/api/v1/resources/robots")).json()
        assert ops["total"] == 0
        assert rbs["total"] == 1
