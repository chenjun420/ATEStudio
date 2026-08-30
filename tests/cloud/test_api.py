"""API integration tests for scripts CRUD operations.

Uses httpx AsyncClient with ASGITransport to test the FastAPI endpoints.
All tests use SQLite in-memory database configured in conftest.py.
"""

import pytest


class TestListScripts:
    """Tests for GET /api/v1/scripts endpoint."""

    @pytest.mark.asyncio
    async def test_list_scripts_empty(self, client):
        """Test listing scripts when storage is empty."""
        response = await client.get("/api/v1/scripts")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "total" in data
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_scripts_with_data(self, client):
        """Test listing scripts when storage has items."""
        # Create a script first
        create_data = {
            "name": "Test Script",
            "script_path": "/scripts/test.py"
        }
        await client.post("/api/v1/scripts", json=create_data)

        # List scripts
        response = await client.get("/api/v1/scripts")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Test Script"


class TestCreateScript:
    """Tests for POST /api/v1/scripts endpoint."""

    @pytest.mark.asyncio
    async def test_create_script(self, client):
        """Test creating a new script."""
        script_data = {
            "name": "New Script",
            "description": "A test script",
            "script_path": "/scripts/new.py",
            "tags": ["test", "demo"]
        }
        response = await client.post("/api/v1/scripts", json=script_data)

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "New Script"
        assert data["description"] == "A test script"
        assert data["script_path"] == "/scripts/new.py"
        assert data["tags"] == ["test", "demo"]
        assert "id" in data
        assert "created_at" in data
        assert "updated_at" in data

    @pytest.mark.asyncio
    async def test_create_script_minimal(self, client):
        """Test creating a script with minimal required fields."""
        script_data = {
            "name": "Minimal Script",
            "script_path": "/scripts/minimal.py"
        }
        response = await client.post("/api/v1/scripts", json=script_data)

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Minimal Script"
        assert data["script_path"] == "/scripts/minimal.py"
        assert data["description"] is None
        assert data["tags"] == []

    @pytest.mark.asyncio
    async def test_create_script_invalid(self, client):
        """Test creating a script with invalid data."""
        script_data = {
            "name": "",  # Empty name is invalid
            "script_path": "/scripts/test.py"
        }
        response = await client.post("/api/v1/scripts", json=script_data)
        assert response.status_code == 422  # Validation error

    @pytest.mark.asyncio
    async def test_create_script_duplicate_name(self, client):
        """Test creating a script with a duplicate name."""
        script_data = {
            "name": "Duplicate Script",
            "script_path": "/scripts/duplicate.py"
        }
        response = await client.post("/api/v1/scripts", json=script_data)
        assert response.status_code == 201

        # Try to create another script with the same name
        response = await client.post("/api/v1/scripts", json=script_data)
        assert response.status_code == 409  # Conflict


class TestGetScript:
    """Tests for GET /api/v1/scripts/{id} endpoint."""

    @pytest.mark.asyncio
    async def test_get_script(self, client):
        """Test getting a script by ID."""
        # Create a script first
        create_data = {
            "name": "Get Test Script",
            "script_path": "/scripts/get_test.py"
        }
        create_response = await client.post("/api/v1/scripts", json=create_data)
        script_id = create_response.json()["id"]

        # Get the script
        response = await client.get(f"/api/v1/scripts/{script_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == script_id
        assert data["name"] == "Get Test Script"

    @pytest.mark.asyncio
    async def test_get_nonexistent_script(self, client):
        """Test getting a script that doesn't exist."""
        response = await client.get("/api/v1/scripts/nonexistent-id")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


class TestUpdateScript:
    """Tests for PUT /api/v1/scripts/{id} endpoint."""

    @pytest.mark.asyncio
    async def test_update_script(self, client):
        """Test updating an existing script."""
        # Create a script first
        create_data = {
            "name": "Original Name",
            "description": "Original description",
            "script_path": "/scripts/original.py"
        }
        create_response = await client.post("/api/v1/scripts", json=create_data)
        script_id = create_response.json()["id"]

        # Update the script
        update_data = {
            "name": "Updated Name",
            "description": "Updated description"
        }
        response = await client.put(f"/api/v1/scripts/{script_id}", json=update_data)

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"
        assert data["description"] == "Updated description"
        assert data["script_path"] == "/scripts/original.py"  # Unchanged

    @pytest.mark.asyncio
    async def test_update_nonexistent_script(self, client):
        """Test updating a script that doesn't exist."""
        update_data = {"name": "New Name"}
        response = await client.put("/api/v1/scripts/nonexistent-id", json=update_data)
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_script_duplicate_name(self, client):
        """Test updating a script name to an already existing name."""
        # Create two scripts
        await client.post("/api/v1/scripts", json={
            "name": "Script One",
            "script_path": "/scripts/one.py"
        })
        create_response = await client.post("/api/v1/scripts", json={
            "name": "Script Two",
            "script_path": "/scripts/two.py"
        })
        script_id = create_response.json()["id"]

        # Try to update Script Two's name to Script One
        update_data = {"name": "Script One"}
        response = await client.put(f"/api/v1/scripts/{script_id}", json=update_data)
        assert response.status_code == 409  # Conflict


class TestDeleteScript:
    """Tests for DELETE /api/v1/scripts/{id} endpoint."""

    @pytest.mark.asyncio
    async def test_delete_script(self, client):
        """Test deleting an existing script."""
        # Create a script first
        create_data = {
            "name": "To Delete",
            "script_path": "/scripts/delete.py"
        }
        create_response = await client.post("/api/v1/scripts", json=create_data)
        script_id = create_response.json()["id"]

        # Delete the script
        response = await client.delete(f"/api/v1/scripts/{script_id}")
        assert response.status_code == 204

        # Verify it's deleted
        get_response = await client.get(f"/api/v1/scripts/{script_id}")
        assert get_response.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_nonexistent_script(self, client):
        """Test deleting a script that doesn't exist."""
        response = await client.delete("/api/v1/scripts/nonexistent-id")
        assert response.status_code == 404
