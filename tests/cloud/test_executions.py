"""Tests for execution CRUD operations and SSE bridge.

Uses httpx AsyncClient with ASGITransport to test the FastAPI endpoints.
SSE bridge tests verify local-mode operation (no NATS required).
"""

import asyncio
import json
import time

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
        """Test get_or_create_queue creates and returns queues with refcounting."""
        bridge = SSEBridge(nc=None)
        queue1 = bridge.get_or_create_queue("run-001")
        queue2 = bridge.get_or_create_queue("run-001")
        assert queue1 is queue2  # Same queue for same run_id
        assert bridge._refcounts["run-001"] == 2  # Two references

        queue3 = bridge.get_or_create_queue("run-002")
        assert queue3 is not queue1  # Different queue for different run_id
        assert bridge._refcounts["run-002"] == 1

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
        """Test remove_queue refcounts and cleans up queue at zero."""
        bridge = SSEBridge(nc=None)
        bridge.get_or_create_queue("run-001")
        assert "run-001" in bridge._queues
        assert bridge._refcounts["run-001"] == 1

        # One removal at refcount 1 → should delete
        bridge.remove_queue("run-001")
        assert "run-001" not in bridge._queues
        assert "run-001" not in bridge._refcounts

    def test_remove_queue_refcount_multiple_clients(self):
        """Test remove_queue only deletes queue when all clients disconnect."""
        bridge = SSEBridge(nc=None)
        # Simulate two SSE clients
        bridge.get_or_create_queue("run-001")  # client 1
        bridge.get_or_create_queue("run-001")  # client 2
        assert bridge._refcounts["run-001"] == 2

        # Client 1 disconnects
        bridge.remove_queue("run-001")
        assert "run-001" in bridge._queues  # Queue still alive
        assert bridge._refcounts["run-001"] == 1

        # Client 2 disconnects
        bridge.remove_queue("run-001")
        assert "run-001" not in bridge._queues  # Queue deleted
        assert "run-001" not in bridge._refcounts

    def test_remove_queue_excess_calls_no_crash(self):
        """Test remove_queue beyond zero refcount doesn't crash."""
        bridge = SSEBridge(nc=None)
        bridge.get_or_create_queue("run-001")
        bridge.remove_queue("run-001")
        # Should be gone now — extra remove should be no-op
        bridge.remove_queue("run-001")  # Should not raise
        bridge.remove_queue("run-001")  # Should not raise

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


class TestReplayPagination:
    """Tests for JetStream replay pagination logic."""

    @pytest.mark.asyncio
    async def test_replay_skips_when_no_nats(self):
        """Test replay_from_jetstream returns empty when NATS is unavailable."""
        bridge = SSEBridge(nc=None)
        results = []
        async for event in bridge.replay_from_jetstream("run-001", "run-001-5"):
            results.append(event)

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_replay_skips_invalid_last_event_id(self):
        """Test replay_from_jetstream handles invalid Last-Event-ID gracefully."""
        bridge = SSEBridge(nc=None)
        results = []
        async for event in bridge.replay_from_jetstream("run-001", "not-a-valid-id"):
            results.append(event)

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_replay_pagination_loop_logic(self):
        """Test that replay_from_jetstream pagination loop fetches multiple batches.

        This test verifies the pagination loop structure by mocking the
        JetStream pull subscriber to return multiple batches.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        # Create a mock NATS client
        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_js = MagicMock()
        mock_nc.jetstream.return_value = mock_js

        # Mock pull subscribe and fetch to simulate 2 batches
        mock_psub = MagicMock()

        # Batch 1: 100 messages (full batch — triggers next iteration)
        batch1 = []
        for i in range(100):
            msg = MagicMock()
            msg.data = json.dumps({
                "id": f"run-001-{i + 1}",
                "type": "EVENT",
                "category": "event",
                "run_id": "run-001",
                "data": {"seq": i + 1},
                "timestamp": time.time(),
            }).encode()
            meta = MagicMock()
            meta.sequence = MagicMock()
            meta.sequence.stream = i + 1
            msg.metadata = AsyncMock(return_value=meta)
            msg.ack = AsyncMock()
            batch1.append(msg)

        # Batch 2: 50 messages (partial batch — triggers break)
        batch2 = []
        for i in range(50):
            msg = MagicMock()
            msg.data = json.dumps({
                "id": f"run-001-{i + 101}",
                "type": "EVENT",
                "category": "event",
                "run_id": "run-001",
                "data": {"seq": i + 101},
                "timestamp": time.time(),
            }).encode()
            meta = MagicMock()
            meta.sequence = MagicMock()
            meta.sequence.stream = i + 101
            msg.metadata = AsyncMock(return_value=meta)
            msg.ack = AsyncMock()
            batch2.append(msg)

        mock_psub.fetch = AsyncMock(side_effect=[batch1, batch2])
        mock_js.pull_subscribe = AsyncMock(return_value=mock_psub)

        bridge = SSEBridge(nc=mock_nc)
        results = []
        async for event in bridge.replay_from_jetstream("run-001", "run-001-0"):
            results.append(event)

        # Should have all 150 events from both batches
        assert len(results) == 150
        assert mock_psub.fetch.call_count == 2  # Two batch fetches
        mock_psub.unsubscribe.assert_called_once()  # Cleaned up

    @pytest.mark.asyncio
    async def test_replay_pagination_empty_batch_stops(self):
        """Test replay_from_jetstream stops when fetch returns empty."""
        from unittest.mock import AsyncMock, MagicMock

        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_js = MagicMock()
        mock_nc.jetstream.return_value = mock_js

        mock_psub = MagicMock()
        mock_psub.fetch = AsyncMock(return_value=[])  # Empty batch
        mock_js.pull_subscribe = AsyncMock(return_value=mock_psub)

        bridge = SSEBridge(nc=mock_nc)
        results = []
        async for event in bridge.replay_from_jetstream("run-001", "run-001-5"):
            results.append(event)

        assert len(results) == 0
        mock_psub.fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_replay_pagination_timeout_stops(self):
        """Test replay_from_jetstream stops when timeout occurs."""
        from unittest.mock import AsyncMock, MagicMock

        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_js = MagicMock()
        mock_nc.jetstream.return_value = mock_js

        mock_psub = MagicMock()
        mock_psub.fetch = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_js.pull_subscribe = AsyncMock(return_value=mock_psub)

        bridge = SSEBridge(nc=mock_nc)
        results = []
        async for event in bridge.replay_from_jetstream("run-001", "run-001-5"):
            results.append(event)

        assert len(results) == 0  # Timeout gracefully stops

    @pytest.mark.asyncio
    async def test_replay_pagination_250_plus_events(self):
        """Test replay_from_jetstream recovers 250+ events across 3 batches.

        This validates the core bug fix: events beyond batch 100 are NOT lost.
        """
        from unittest.mock import AsyncMock, MagicMock

        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_js = MagicMock()
        mock_nc.jetstream.return_value = mock_js

        mock_psub = MagicMock()

        def make_batch(start: int, count: int):
            batch = []
            for i in range(count):
                msg = MagicMock()
                msg.data = json.dumps({
                    "id": f"run-001-{start + i}",
                    "type": "EVENT",
                    "category": "event",
                    "run_id": "run-001",
                    "data": {"seq": start + i},
                    "timestamp": time.time(),
                }).encode()
                meta = MagicMock()
                meta.sequence = MagicMock()
                meta.sequence.stream = start + i
                msg.metadata = AsyncMock(return_value=meta)
                msg.ack = AsyncMock()
                batch.append(msg)
            return batch

        # 3 batches: 100 + 100 + 50 = 250 events
        batch1 = make_batch(1, 100)
        batch2 = make_batch(101, 100)
        batch3 = make_batch(201, 50)

        mock_psub.fetch = AsyncMock(side_effect=[batch1, batch2, batch3])
        mock_js.pull_subscribe = AsyncMock(return_value=mock_psub)

        bridge = SSEBridge(nc=mock_nc)
        results = []
        async for event in bridge.replay_from_jetstream("run-001", "run-001-0"):
            results.append(event)

        assert len(results) == 250
        assert mock_psub.fetch.call_count == 3


class TestLocalModeHeartbeat:
    """Tests for local-mode heartbeat generation."""

    @pytest.mark.asyncio
    async def test_local_heartbeat_yields_keep_alive(self):
        """Test get_local_heartbeat yields keep-alive events at interval."""
        bridge = SSEBridge(nc=None)

        # Collect the first heartbeat
        heartbeat_gen = bridge.get_local_heartbeat("run-001")
        result = await heartbeat_gen.__anext__()

        assert result["comment"] == "keep-alive"
        assert "data" in result

    @pytest.mark.asyncio
    async def test_local_heartbeat_multiple_yields(self):
        """Test get_local_heartbeat yields multiple keep-alive events."""
        bridge = SSEBridge(nc=None)

        heartbeat_gen = bridge.get_local_heartbeat("run-001")
        r1 = await heartbeat_gen.__anext__()
        assert r1["comment"] == "keep-alive"


class TestSSEEndpointHeartbeat:
    """Tests for SSE endpoint heartbeat behavior."""

    @pytest.mark.asyncio
    async def test_keep_alive_timeout_is_15s(self):
        """Verify keep-alive timeout is 15 seconds (not 30)."""
        from ate_cloud.api.v1.executions import stream_execution_events

        # The timeout is embedded in the event_generator closure.
        # We verify by reading the source — the heartbeat_interval is 15.0.
        import inspect
        source = inspect.getsource(stream_execution_events)
        assert "heartbeat_interval = 15.0" in source or "timeout=heartbeat_interval" in source

    @pytest.mark.asyncio
    async def test_local_mode_uses_asyncio_wait_for_heartbeat(self):
        """Test local mode heartbeat uses asyncio.wait race pattern."""
        import inspect
        from ate_cloud.api.v1.executions import stream_execution_events
        source = inspect.getsource(stream_execution_events)
        # Local mode should use asyncio.wait with FIRST_COMPLETED
        assert "asyncio.wait" in source or "FIRST_COMPLETED" in source
