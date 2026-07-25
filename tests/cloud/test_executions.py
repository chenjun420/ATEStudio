"""Tests for execution CRUD operations and SSE bridge.

Uses httpx AsyncClient with ASGITransport to test the FastAPI endpoints.
SSE bridge tests verify local-mode operation (no NATS required).
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from ate_cloud.nats.sse_bridge import SSEBridge


class TestCreateExecution:
    """Tests for POST /api/v1/executions endpoint."""

    @pytest.mark.asyncio
    async def test_create_execution(self, client):
        """Test creating a new execution."""
        execution_data = {
            "sequence_id": "seq-001",
            "config": {"max_concurrency": 4},
        }
        response = await client.post("/api/v1/executions", json=execution_data)

        assert response.status_code == 201
        data = response.json()
        assert data["sequence_id"] == "seq-001"
        assert data["status"] == "PENDING"
        assert data["config"] == {"max_concurrency": 4}
        assert "id" in data
        assert "created_at" in data
        assert "updated_at" in data

    @pytest.mark.asyncio
    async def test_create_execution_minimal(self, client):
        """Test creating an execution with minimal required fields."""
        execution_data = {"sequence_id": "seq-002"}
        response = await client.post("/api/v1/executions", json=execution_data)

        assert response.status_code == 201
        data = response.json()
        assert data["sequence_id"] == "seq-002"
        assert data["status"] == "PENDING"
        assert data["config"] is None

    @pytest.mark.asyncio
    async def test_create_execution_invalid(self, client):
        """Test creating an execution with invalid data (missing sequence_id)."""
        execution_data = {}
        response = await client.post("/api/v1/executions", json=execution_data)
        assert response.status_code == 422  # Validation error


class TestGetExecution:
    """Tests for GET /api/v1/executions/{run_id} endpoint."""

    @pytest.mark.asyncio
    async def test_get_execution(self, client):
        """Test getting an execution by run_id."""
        # Create an execution first
        create_data = {"sequence_id": "seq-003"}
        create_response = await client.post("/api/v1/executions", json=create_data)
        run_id = create_response.json()["id"]

        # Get the execution
        response = await client.get(f"/api/v1/executions/{run_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == run_id
        assert data["sequence_id"] == "seq-003"
        assert data["status"] == "PENDING"

    @pytest.mark.asyncio
    async def test_get_nonexistent_execution(self, client):
        """Test getting an execution that doesn't exist."""
        response = await client.get("/api/v1/executions/nonexistent-id")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


class TestAbortExecution:
    """Tests for POST /api/v1/executions/{run_id}/abort endpoint."""

    @pytest.mark.asyncio
    async def test_abort_execution(self, client):
        """Test aborting a running execution."""
        # Create an execution first
        create_data = {"sequence_id": "seq-004"}
        create_response = await client.post("/api/v1/executions", json=create_data)
        run_id = create_response.json()["id"]

        # Abort the execution
        response = await client.post(f"/api/v1/executions/{run_id}/abort")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == run_id
        assert data["status"] == "ABORTING"

    @pytest.mark.asyncio
    async def test_abort_nonexistent_execution(self, client):
        """Test aborting an execution that doesn't exist."""
        response = await client.post("/api/v1/executions/nonexistent-id/abort")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_abort_already_completed_execution(self, client):
        """Test aborting an execution already in terminal state."""
        # Create and abort an execution
        create_data = {"sequence_id": "seq-005"}
        create_response = await client.post("/api/v1/executions", json=create_data)
        run_id = create_response.json()["id"]

        await client.post(f"/api/v1/executions/{run_id}/abort")

        # Try to abort again
        response = await client.post(f"/api/v1/executions/{run_id}/abort")
        assert response.status_code == 409


class TestSSEBridgeLocalMode:
    """Tests for SSEBridge in local mode (no NATS)."""

    def test_bridge_initialization(self):
        """Test SSEBridge initializes correctly without NATS."""
        bridge = SSEBridge(nc=None)
        assert not bridge.nats_available
        assert len(bridge._queues) == 0

    def test_get_or_create_queue(self):
        """Test get_or_create_queue creates and returns queues."""
        bridge = SSEBridge(nc=None)
        queue1 = bridge.get_or_create_queue("run-001")
        queue2 = bridge.get_or_create_queue("run-001")
        assert queue1 is queue2  # Same queue for same run_id

        queue3 = bridge.get_or_create_queue("run-002")
        assert queue3 is not queue1  # Different queue for different run_id

    @pytest.mark.asyncio
    async def test_publish_event_to_queue(self):
        """Test publish_event pushes events to local queue in local mode."""
        bridge = SSEBridge(nc=None)
        await bridge.publish_event(
            run_id="run-001",
            event_type="EXECUTION_STARTED",
            data={"status": "PENDING"},
        )

        queue = bridge.get_or_create_queue("run-001")
        event = queue.get_nowait()
        assert event["type"] == "EXECUTION_STARTED"
        assert event["run_id"] == "run-001"
        assert event["data"] == {"status": "PENDING"}
        assert "id" in event
        assert "timestamp" in event

    @pytest.mark.asyncio
    async def test_publish_multiple_events(self):
        """Test publishing multiple events maintains order."""
        bridge = SSEBridge(nc=None)
        await bridge.publish_event("run-001", "EXECUTION_STARTED", {"status": "PENDING"})
        await bridge.publish_event("run-001", "STEP_COMPLETED", {"step_id": "step-1"})

        queue = bridge.get_or_create_queue("run-001")
        event1 = queue.get_nowait()
        event2 = queue.get_nowait()
        assert event1["type"] == "EXECUTION_STARTED"
        assert event2["type"] == "STEP_COMPLETED"

    def test_remove_queue(self):
        """Test remove_queue cleans up queue for a run_id."""
        bridge = SSEBridge(nc=None)
        bridge.get_or_create_queue("run-001")
        assert "run-001" in bridge._queues

        bridge.remove_queue("run-001")
        assert "run-001" not in bridge._queues

    def test_remove_nonexistent_queue(self):
        """Test remove_queue is safe for nonexistent run_id."""
        bridge = SSEBridge(nc=None)
        bridge.remove_queue("nonexistent")  # Should not raise

    @pytest.mark.asyncio
    async def test_event_ids_are_sequential(self):
        """Test that event IDs are sequential within a bridge instance."""
        bridge = SSEBridge(nc=None)
        await bridge.publish_event("run-001", "EVENT_1", {})
        await bridge.publish_event("run-001", "EVENT_2", {})

        queue = bridge.get_or_create_queue("run-001")
        event1 = queue.get_nowait()
        event2 = queue.get_nowait()

        # IDs should be run_id-N format with sequential N
        id1_parts = event1["id"].split("-")
        id2_parts = event2["id"].split("-")
        # The counter part is the last element
        assert int(id2_parts[-1]) == int(id1_parts[-1]) + 1


class TestSSEEndpoint:
    """Tests for GET /api/v1/executions/{run_id}/events SSE endpoint."""

    @pytest.mark.asyncio
    async def test_sse_endpoint_returns_event_source(self, app):
        """Test SSE endpoint returns EventSourceResponse with correct content type."""
        from ate_cloud.api.v1.executions import stream_execution_events
        from unittest.mock import MagicMock

        # Create an execution first to get a valid run_id
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            create_data = {"sequence_id": "seq-sse-001"}
            create_response = await ac.post("/api/v1/executions", json=create_data)
            run_id = create_response.json()["id"]

        # Test the endpoint directly — verify it returns EventSourceResponse
        from sse_starlette.sse import EventSourceResponse

        bridge = app.state.sse_bridge
        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.app = app

        response = await stream_execution_events(run_id=run_id, request=mock_request, bridge=bridge)
        assert isinstance(response, EventSourceResponse)
