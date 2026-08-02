"""Tests verifying SSE integration: ExecutionStatusRelay -> SSEBridge -> SSE endpoint.

Verifies that events pushed via SSEBridge.push_to_queue_only() (the method
called by ExecutionStatusRelay at execution_status_relay.py:174) flow through
events_for_run() (the generator used by the GET /api/v1/executions/{run_id}/events
SSE endpoint at executions.py:75).

This is a verification task — the SSE endpoint, SSEBridge, and
ExecutionStatusRelay are NOT modified. These tests pin the integration
contract so future changes can't silently break the relay -> bridge -> client
flow.
"""

import asyncio

import pytest

from ate_cloud.nats.sse_bridge import SSEBridge

# Short timeout for collecting events from events_for_run(). Phase 2 of
# events_for_run() drains already-queued events synchronously via get_nowait(),
# so pushed events arrive immediately. Phase 3 blocks on queue.get() waiting
# for live events — this timeout catches that block so the test doesn't hang.
_DRAIN_TIMEOUT: float = 1.0


async def _drain(
    gen: "asyncio.AsyncIterator[dict[str, object]]",
    count: int,
    timeout: float = _DRAIN_TIMEOUT,
) -> list[dict[str, object]]:
    """Collect up to ``count`` events from an async generator.

    Uses ``wait_for`` with a short timeout so the generator doesn't block
    forever in events_for_run()'s Phase 3 (live event wait). Returns when
    ``count`` events are collected or the timeout fires on the next iteration.

    Args:
        gen: The async generator (from ``SSEBridge.events_for_run``).
        count: Maximum number of events to collect.
        timeout: Per-event timeout in seconds.

    Returns:
        List of collected event dicts (may be shorter than ``count`` if the
        timeout fired before all were available).
    """
    events: list[dict[str, object]] = []
    try:
        for _ in range(count):
            event = await asyncio.wait_for(gen.__anext__(), timeout=timeout)
            events.append(event)
    except (TimeoutError, StopAsyncIteration):
        pass
    return events


class TestSSEExecutionRelay:
    """Verify ExecutionStatusRelay -> SSEBridge -> SSE endpoint integration."""

    @pytest.mark.asyncio
    async def test_sse_receives_execution_events_from_relay(self):
        """Events pushed via push_to_queue_only() are delivered by events_for_run().

        ExecutionStatusRelay calls push_to_queue_only() for each status event
        it pulls from ATE_STATUS (execution_status_relay.py:174). The SSE
        endpoint calls events_for_run() to stream events to connected clients
        (executions.py:75). This test verifies the bridge between them: events
        pushed via push_to_queue_only() appear in the events_for_run()
        generator output.
        """
        bridge = SSEBridge(nc=None)
        run_id = "relay-integration-001"

        # Simulate ExecutionStatusRelay pushing status events (the exact
        # call sequence from _process_message in execution_status_relay.py).
        await bridge.push_to_queue_only(run_id, {
            "type": "execution_started",
            "run_id": run_id,
        })
        await bridge.push_to_queue_only(run_id, {
            "type": "step_started",
            "step_id": "step-1",
            "run_id": run_id,
        })

        # SSE endpoint uses events_for_run() to deliver events to clients.
        gen = bridge.events_for_run(run_id)
        events = await _drain(gen, count=2)

        assert len(events) == 2, (
            f"Expected 2 events from relay, got {len(events)}"
        )
        assert events[0]["type"] == "execution_started"
        assert events[0]["run_id"] == run_id
        assert events[1]["type"] == "step_started"
        assert events[1]["step_id"] == "step-1"

        await bridge.close()

    @pytest.mark.asyncio
    async def test_sse_step_events_ordered(self):
        """Step events arrive in the same order they were pushed.

        asyncio.Queue is FIFO by construction, but this test pins the ordering
        contract end-to-end: a client streaming via events_for_run() sees
        step_started -> step_completed -> step_started -> step_failed in push
        order. If push_to_queue_only() or events_for_run()'s Phase 2 drain
        ever reorders, this test catches it.
        """
        bridge = SSEBridge(nc=None)
        run_id = "relay-ordering-002"

        step_events = [
            {"type": "step_started", "step_id": "step-1", "run_id": run_id},
            {"type": "step_completed", "step_id": "step-1", "status": "PASSED", "run_id": run_id},
            {"type": "step_started", "step_id": "step-2", "run_id": run_id},
            {"type": "step_failed", "step_id": "step-2", "status": "FAILED", "run_id": run_id},
        ]
        for event in step_events:
            await bridge.push_to_queue_only(run_id, event)

        gen = bridge.events_for_run(run_id)
        events = await _drain(gen, count=len(step_events))

        assert len(events) == len(step_events), (
            f"Expected {len(step_events)} events, got {len(events)}"
        )
        for i, expected in enumerate(step_events):
            assert events[i]["type"] == expected["type"], (
                f"Event {i}: expected type={expected['type']}, "
                f"got {events[i]['type']}"
            )
            assert events[i]["step_id"] == expected["step_id"], (
                f"Event {i}: expected step_id={expected['step_id']}, "
                f"got {events[i]['step_id']}"
            )

        await bridge.close()

    @pytest.mark.asyncio
    async def test_existing_sse_endpoint_unchanged(self):
        """publish_event() and push_to_queue_only() coexist in the same queue.

        Backward compatibility: existing code that uses publish_event() (which
        publishes to NATS + local queue with a full event envelope) still
        flows to SSE clients via events_for_run(). The new push_to_queue_only()
        method (used by ExecutionStatusRelay, pushes raw event dicts) flows
        through the same queue. Both sources are delivered to connected SSE
        clients in push order.
        """
        bridge = SSEBridge(nc=None)
        run_id = "relay-backcompat-003"

        # Existing path: publish_event() wraps the event in an envelope
        # (id, type, category, run_id, data, timestamp) and pushes to queue.
        await bridge.publish_event(
            run_id=run_id,
            event_type="EXECUTION_STARTED",
            data={"status": "PENDING"},
        )

        # New path: push_to_queue_only() pushes the raw event dict (no
        # envelope wrapping). This is what ExecutionStatusRelay calls.
        relay_event = {
            "type": "step_completed",
            "step_id": "step-1",
            "status": "PASSED",
            "run_id": run_id,
        }
        await bridge.push_to_queue_only(run_id, relay_event)

        gen = bridge.events_for_run(run_id)
        events = await _drain(gen, count=2)

        assert len(events) == 2, (
            f"Expected 2 events (1 from publish_event, 1 from "
            f"push_to_queue_only), got {len(events)}"
        )

        # First event: from publish_event() — has the full envelope.
        pub_event = events[0]
        assert pub_event["type"] == "EXECUTION_STARTED"
        assert pub_event["run_id"] == run_id
        assert "id" in pub_event, "publish_event() must set event id"
        assert "category" in pub_event, "publish_event() must set SSE category"
        assert "timestamp" in pub_event, "publish_event() must set timestamp"
        assert pub_event["data"] == {"status": "PENDING"}

        # Second event: from push_to_queue_only() — raw event dict, no envelope.
        relay_received = events[1]
        assert relay_received["type"] == "step_completed"
        assert relay_received["step_id"] == "step-1"
        assert relay_received["status"] == "PASSED"
        assert relay_received["run_id"] == run_id

        await bridge.close()
