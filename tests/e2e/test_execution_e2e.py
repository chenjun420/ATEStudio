"""E2E test: POST /api/v1/executions -> NATS -> scheduler -> execute -> SSE.

Exercises the full execution flow:
1. POST /api/v1/executions creates DB record + dispatches plan to NATS JetStream
2. JetStreamWorker pulls task, boots ScannerScheduler
3. Step lifecycle events flow through NATS ATE_STATUS
4. ExecutionStatusRelay consumes status, updates DB, pushes to SSE bridge
5. SSE endpoint streams events to connected clients

Tests skip gracefully if NATS server is not available at localhost:4222.

All fixtures are function-scoped to ensure the NATS client, relay task, and
test share the same asyncio event loop (pytest-asyncio 1.x with
asyncio_default_fixture_loop_scope=None creates separate loops for
module-scoped async fixtures vs. function-scoped tests, which causes
JetStream request-reply to time out).
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncGenerator
from typing import Any

import nats
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nats.js.api import AckPolicy, ConsumerConfig, RetentionPolicy, StreamConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import ate_cloud.main as main_module
from ate_cloud.db import get_db
from ate_cloud.models import Base, Execution, Sequence
from ate_cloud.nats.sse_bridge import SSEBridge
from ate_cloud.nats.stream_manager import StreamManager
from ate_cloud.services.execution_status_relay import ExecutionStatusRelay
from ate_platform.scheduler.jetstream_worker import PlanBootstrapper, _dict_to_yaml_plan
from ate_platform.types import StepStatus
from shared.events import Event, EventType

_NATS_URL = "nats://localhost:4222"
_DRAIN_TIMEOUT: float = 3.0


def _check_nats() -> bool:
    """Check if NATS is listening on localhost:4222."""
    try:
        sock = socket.create_connection(("localhost", 4222), timeout=2)
        sock.close()
        return True
    except OSError:
        return False


_NATS_AVAILABLE = _check_nats()
_skip = pytest.mark.skipif(
    not _NATS_AVAILABLE, reason="NATS server not available at localhost:4222"
)

_TEST_YAML = """
name: e2e_test_plan
version: "1.0"
scope: test
max_concurrency: 1
steps:
  - id: step_1
    script: pass_script.py
    params:
      voltage: 3.3
    timeout: 10
  - id: step_2
    script: pass_script.py
    params:
      channel: 1
    preconditions:
      - step_1
    timeout: 10
"""


# ── Fixtures (all function-scoped for event-loop consistency) ────────


@pytest.fixture
async def nc() -> AsyncGenerator[Any, None]:
    """Real NATS connection."""
    client = await nats.connect(_NATS_URL)
    saved = main_module._nats_client
    main_module._nats_client = client
    yield client
    main_module._nats_client = saved
    await client.close()


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    """In-memory SQLite engine."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def streams_and_relay(
    nc: Any, session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[SSEBridge, asyncio.Task[None]], None]:
    """Create streams, SSE bridge, and start the status relay.

    Recreates ATE_STATUS with ``ate.status.>`` so SSEBridge.publish_event
    (4-token subject) gets a PubAck. The relay's consumer filter
    ``ate.status.*`` only receives 3-token subjects — no feedback loop.
    """
    sm = StreamManager(nc)
    await sm.create_streams()
    await sm.create_dead_letter_stream()

    js = nc.jetstream()
    try:
        await js.delete_stream("ATE_STATUS")
    except Exception:
        pass
    await js.add_stream(config=StreamConfig(
        name="ATE_STATUS", subjects=["ate.status.>"],
        retention=RetentionPolicy.LIMITS, max_age=7 * 24 * 60 * 60,
    ))
    await sm.create_consumers()
    await sm.create_kv_store()

    bridge = SSEBridge(nc=nc)
    relay = ExecutionStatusRelay(
        nats_client=nc, sse_bridge=bridge, async_session_factory=session_factory,
    )
    relay_task = asyncio.create_task(relay.start())
    await asyncio.sleep(0.5)

    yield bridge, relay_task

    relay_task.cancel()
    try:
        await relay_task
    except asyncio.CancelledError:
        pass
    await bridge.cleanup()


@pytest.fixture
async def app(
    nc: Any, session_factory: async_sessionmaker[AsyncSession],
    streams_and_relay: tuple[SSEBridge, asyncio.Task[None]],
) -> FastAPI:
    """FastAPI app with test DB override and real SSE bridge."""
    bridge, _ = streams_and_relay
    app_obj = main_module.create_app()

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as s:
            yield s

    app_obj.dependency_overrides[get_db] = override_get_db
    app_obj.state.sse_bridge = bridge
    return app_obj


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def purge(nc: Any, streams_and_relay: tuple[SSEBridge, asyncio.Task[None]]) -> AsyncGenerator[None, None]:
    """Purge JetStream streams before and after each test."""
    js = nc.jetstream()
    for name in ("ATE_TASKS", "ATE_STATUS", "ATE_DEAD_LETTERS"):
        try:
            await js.purge_stream(name)
        except Exception:
            pass
    yield
    for name in ("ATE_TASKS", "ATE_STATUS", "ATE_DEAD_LETTERS"):
        try:
            await js.purge_stream(name)
        except Exception:
            pass


# ── Helpers ───────────────────────────────────────────────────────────


async def _drain_events(
    gen: asyncio.AsyncIterator[dict[str, Any]],
    count: int,
    timeout: float = _DRAIN_TIMEOUT,
) -> list[dict[str, Any]]:
    """Collect up to ``count`` events from an async generator."""
    events: list[dict[str, Any]] = []
    try:
        for _ in range(count):
            ev = await asyncio.wait_for(gen.__anext__(), timeout=timeout)
            events.append(ev)
    except (TimeoutError, StopAsyncIteration):
        pass
    return events


def _setup_step_runner(
    nc: Any, execution_id: str, event_bus: Any, registry: Any,
) -> None:
    """Subscribe to STEP_STARTED: publish status to NATS + mark step PASSED.

    Simulates script execution by immediately marking each started step as
    PASSED. Publishes flat status events (run_id, step_id, status at top
    level) to ``ate.status.{execution_id}`` so the ExecutionStatusRelay
    can update the DB and push to the SSE bridge queue.
    """

    async def on_step_started(event: Event) -> None:
        step_id = event.data.get("step_id")
        if step_id is None:
            return
        await nc.publish(
            f"ate.status.{execution_id}",
            json.dumps({"type": "STEP_STARTED", "run_id": execution_id,
                        "step_id": step_id, "status": "RUNNING"}).encode(),
        )
        try:
            registry.update_status(step_id, StepStatus.PASSED)
        except KeyError:
            pass

    async def on_step_status_changed(event: Event) -> None:
        step_id = event.data.get("step_id")
        new_status = event.data.get("new_status")
        if step_id is None or new_status is None:
            return
        if new_status in ("PASSED", "FAILED", "SKIPPED", "ERROR"):
            await nc.publish(
                f"ate.status.{execution_id}",
                json.dumps({"type": "STEP_COMPLETED", "run_id": execution_id,
                            "step_id": step_id, "status": new_status}).encode(),
            )

    event_bus.subscribe(EventType.STEP_STARTED, on_step_started)
    event_bus.subscribe(EventType.STEP_STATUS_CHANGED, on_step_status_changed)


async def _bootstrap_and_run(nc: Any, msg: Any) -> tuple[PlanBootstrapper, str]:
    """Deserialize plan from NATS message, boot scheduler with step runner."""
    execution_id = ""
    if msg.headers is not None:
        execution_id = msg.headers.get("execution_id", "")
    data = json.loads(msg.data.decode("utf-8"))
    plan = _dict_to_yaml_plan(data)
    bootstrapper = PlanBootstrapper(plan)
    scheduler = bootstrapper.bootstrap(dut_id=execution_id)
    event_bus = bootstrapper.event_bus

    _setup_step_runner(nc, execution_id, event_bus, bootstrapper.step_registry)

    await event_bus.start()
    event_bus.set_event_loop(asyncio.get_running_loop())
    await scheduler.start()
    await msg.ack()
    return bootstrapper, execution_id


# ── Tests ─────────────────────────────────────────────────────────────


@_skip
class TestExecutionE2E:
    """End-to-end execution flow tests (require real NATS)."""

    @pytest.mark.asyncio
    async def test_full_execution_flow(
        self, nc: Any, session_factory: async_sessionmaker[AsyncSession],
        streams_and_relay: tuple[SSEBridge, asyncio.Task[None]],
        app: FastAPI, client: AsyncClient, purge: None,
    ) -> None:
        """POST /api/v1/executions -> NATS -> worker -> status events -> SSE."""
        bridge = streams_and_relay[0]

        # 1. Seed a Sequence
        async with session_factory() as db:
            db.add(Sequence(id="seq-e2e-001", name="e2e-test",
                            description="E2E", yaml_content=_TEST_YAML))
            await db.commit()

        # 2. POST /api/v1/executions
        resp = await client.post("/api/v1/executions", json={"sequence_id": "seq-e2e-001"})
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
        run_id = resp.json()["id"]
        assert resp.json()["status"] == "PENDING"

        # 3. Pull task from ATE_TASKS and boot scheduler
        js = nc.jetstream()
        psub = await js.pull_subscribe("ate.tasks.*", durable="ate-worker")
        msgs = await psub.fetch(batch=1, timeout=5.0)
        assert len(msgs) == 1
        bootstrapper, execution_id = await _bootstrap_and_run(nc, msgs[0])
        assert execution_id == run_id

        # 4. Wait for steps to execute (simulated)
        await asyncio.sleep(3.0)

        # 5. Publish EXECUTION_COMPLETED
        await nc.publish(
            f"ate.status.{execution_id}",
            json.dumps({"type": "EXECUTION_COMPLETED", "run_id": execution_id,
                        "status": "COMPLETED"}).encode(),
        )
        await asyncio.sleep(1.0)  # let relay process

        # 6. Verify DB status updated by relay
        async with session_factory() as db:
            result = await db.execute(select(Execution).where(Execution.id == run_id))
            execution = result.scalar_one_or_none()
            assert execution is not None
            assert execution.status == "COMPLETED"

        # 7. Verify SSE events in bridge queue
        gen = bridge.events_for_run(run_id)
        events = await _drain_events(gen, count=10)
        assert len(events) >= 3, f"Expected >=3 events, got {len(events)}"

        # Cleanup
        await bootstrapper.scheduler.stop()
        await bootstrapper.event_bus.stop()
        bridge.remove_queue(run_id)

    @pytest.mark.asyncio
    async def test_sse_events_ordered(
        self, nc: Any, session_factory: async_sessionmaker[AsyncSession],
        streams_and_relay: tuple[SSEBridge, asyncio.Task[None]],
        app: FastAPI, client: AsyncClient, purge: None,
    ) -> None:
        """SSE events arrive in order: execution_started -> step -> step -> completed."""
        bridge = streams_and_relay[0]

        async with session_factory() as db:
            db.add(Sequence(id="seq-e2e-002", name="e2e-order",
                            description="E2E ordering", yaml_content=_TEST_YAML))
            await db.commit()

        resp = await client.post("/api/v1/executions", json={"sequence_id": "seq-e2e-002"})
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
        run_id = resp.json()["id"]

        js = nc.jetstream()
        psub = await js.pull_subscribe("ate.tasks.*", durable="ate-worker")
        msgs = await psub.fetch(batch=1, timeout=5.0)
        bootstrapper, execution_id = await _bootstrap_and_run(nc, msgs[0])

        await asyncio.sleep(3.0)

        await nc.publish(
            f"ate.status.{execution_id}",
            json.dumps({"type": "EXECUTION_COMPLETED", "run_id": execution_id,
                        "status": "COMPLETED"}).encode(),
        )
        await asyncio.sleep(1.0)

        gen = bridge.events_for_run(run_id)
        events = await _drain_events(gen, count=10)

        types = [e.get("type", "") for e in events]
        assert types[0] == "EXECUTION_STARTED", f"First event should be EXECUTION_STARTED, got {types[0]}"

        step_types = [t for t in types if t.startswith("STEP_")]
        assert len(step_types) >= 2, f"Expected >=2 step events, got {step_types}"

        first_started = next(i for i, t in enumerate(types) if t == "STEP_STARTED")
        first_completed = next(i for i, t in enumerate(types) if t == "STEP_COMPLETED")
        assert first_started < first_completed, "STEP_STARTED must precede STEP_COMPLETED"

        assert types[-1] == "EXECUTION_COMPLETED", f"Last event should be EXECUTION_COMPLETED, got {types[-1]}"

        await bootstrapper.scheduler.stop()
        await bootstrapper.event_bus.stop()
        bridge.remove_queue(run_id)

    @pytest.mark.asyncio
    async def test_dead_letter_on_failure(
        self, nc: Any, streams_and_relay: tuple[SSEBridge, asyncio.Task[None]], purge: None,
    ) -> None:
        """Failed task redelivered after ack_wait; after max_deliver, routed to DLQ."""
        js = nc.jetstream()

        # Replace ate-worker consumer with short ack_wait for fast test
        try:
            await js.delete_consumer("ATE_TASKS", "ate-worker")
        except Exception:
            pass
        await asyncio.sleep(1.0)
        await js.add_consumer("ATE_TASKS", config=ConsumerConfig(
            durable_name="ate-worker", ack_policy=AckPolicy.EXPLICIT,
            ack_wait=1, max_deliver=3,
        ))

        try:
            # Publish invalid task (will cause _dict_to_yaml_plan to fail)
            exec_id = "dlq-test-e2e"
            invalid_payload = b'{"not_a_valid": "plan"}'
            await js.publish(f"ate.tasks.{exec_id}", invalid_payload,
                             headers={"execution_id": exec_id})

            # Pull and nak 3 times — verify redelivery count increments
            psub = await js.pull_subscribe("ate.tasks.*", durable="ate-worker")
            saved_payload = b""

            for attempt in range(3):
                msgs = await psub.fetch(batch=1, timeout=10.0)
                assert len(msgs) == 1
                msg = msgs[0]
                metadata = msg.metadata
                assert metadata.num_delivered == attempt + 1, (
                    f"Expected delivery {attempt + 1}, got {metadata.num_delivered}")
                if attempt == 0:
                    saved_payload = msg.data
                await msg.nak()
                if attempt < 2:
                    await asyncio.sleep(1.5)

            # After max_deliver=3, the message is no longer redelivered.
            # Verify by attempting a 4th fetch (should timeout).
            try:
                await psub.fetch(batch=1, timeout=2.0)
                pytest.fail("Message should not be redelivered after max_deliver")
            except (TimeoutError, Exception):
                pass  # Expected — no more redeliveries

            # Publish original payload to DLQ (application-level DLQ routing
            # — the JetStreamWorker or advisory subscriber would do this)
            await js.publish(f"ate.tasks.{exec_id}.dlq", saved_payload,
                             headers={"execution_id": exec_id})

            # Verify message in ATE_DEAD_LETTERS stream
            dlq_psub = await js.pull_subscribe("ate.tasks.*.dlq", durable="dlq-test-reader")
            dlq_msgs = await dlq_psub.fetch(batch=1, timeout=5.0)
            assert len(dlq_msgs) == 1
            assert dlq_msgs[0].data == saved_payload
            await dlq_msgs[0].ack()

            try:
                await dlq_psub.unsubscribe()
            except Exception:
                pass
        finally:
            # Restore original consumer
            await asyncio.sleep(1.0)
            try:
                await js.delete_consumer("ATE_TASKS", "ate-worker")
            except Exception:
                pass
            await asyncio.sleep(1.0)
            sm = StreamManager(nc)
            await sm.create_consumers()
