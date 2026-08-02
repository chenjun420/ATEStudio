"""Unit tests for JetStreamWorker and PlanBootstrapper.

Tests cover:
- Worker pulls task from NATS and boots ScannerScheduler
- Step lifecycle events forwarded to NATS status subject
- KV heartbeat registration
- Multiprocessing isolation (ProcessStepExecutor with use_multiprocessing=True)
- Worker ID persistence to file
"""

import asyncio
import json
import uuid
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ate_platform.scheduler.jetstream_worker import (
    JetStreamWorker,
    PlanBootstrapper,
    _dict_to_yaml_plan,
)
from shared.dsl import LoopType, YamlLoop, YamlPlan, YamlStep
from shared.events import EventType


class FakeMsg:
    """Fake NATS JetStream message for testing."""

    def __init__(
        self,
        data: bytes,
        headers: dict[str, str] | None = None,
        subject: str = "ate.tasks.exec-123",
    ) -> None:
        self.data = data
        self.headers = headers
        self.subject = subject
        self.acked = False
        self.naked = False

    async def ack(self) -> None:
        self.acked = True

    async def nak(self) -> None:
        self.naked = True


def _enum_to_value(o: Any) -> Any:
    if isinstance(o, Enum):
        return o.value
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _serialize_plan(plan: YamlPlan) -> bytes:
    """Serialize YamlPlan to JSON bytes (mirrors ExecutionDispatchService)."""
    return json.dumps(asdict(plan), default=_enum_to_value).encode("utf-8")


def _make_mock_nc() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Create a mock NATS client with JetStream and KV support."""
    mock_nc = MagicMock()
    mock_nc.is_connected = True
    mock_nc.publish = AsyncMock()
    mock_nc.close = AsyncMock()
    mock_sub = MagicMock()
    mock_sub.unsubscribe = AsyncMock()
    mock_nc.subscribe = AsyncMock(return_value=mock_sub)

    mock_js = MagicMock()
    mock_kv = MagicMock()
    mock_kv.put = AsyncMock(return_value=1)
    mock_js.key_value = AsyncMock(return_value=mock_kv)
    mock_js.pull_subscribe = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    return mock_nc, mock_js, mock_kv


class TestJetStreamWorker:
    """Tests for JetStreamWorker and PlanBootstrapper."""

    @pytest.mark.asyncio
    async def test_worker_pulls_and_boots_scheduler(self, tmp_path: Path) -> None:
        """Worker should pull a task, deserialize plan, boot and start scheduler."""
        plan = YamlPlan(
            name="test-plan",
            version="1.0",
            steps=[YamlStep(id="step1", script="test.py")],
        )
        payload = _serialize_plan(plan)
        mock_nc, mock_js, _ = _make_mock_nc()

        fake_msg = FakeMsg(payload, {"execution_id": "exec-123"})
        mock_psub = MagicMock()
        mock_psub.fetch = AsyncMock(return_value=[fake_msg])
        mock_js.pull_subscribe = AsyncMock(return_value=mock_psub)

        worker = JetStreamWorker(worker_id_path=str(tmp_path / "worker_id"))
        await worker.start(nc=mock_nc)

        result = await worker.pull_and_process_one(timeout=1.0)

        assert result is True
        assert fake_msg.acked is True
        assert worker._current_scheduler is not None
        assert worker._current_scheduler._running is True
        assert worker._current_execution_id == "exec-123"
        assert worker._current_event_bus is not None

        await worker.stop()

        assert worker._current_scheduler is None

    @pytest.mark.asyncio
    async def test_worker_publishes_step_lifecycle_events(self, tmp_path: Path) -> None:
        """Step lifecycle events should be forwarded to ate.status.{execution_id}."""
        mock_nc, _, _ = _make_mock_nc()

        worker = JetStreamWorker(worker_id_path=str(tmp_path / "worker_id"))
        worker._nc = mock_nc

        plan = YamlPlan(
            name="test", version="1.0", steps=[YamlStep(id="s1", script="t.py")],
        )
        bootstrapper = PlanBootstrapper(plan)
        bootstrapper.bootstrap()
        worker._setup_status_forwarding(bootstrapper.event_bus, "exec-456")

        await bootstrapper.event_bus.start()
        await bootstrapper.event_bus.publish(
            EventType.STEP_STARTED, {"step_id": "s1"},
        )
        await asyncio.sleep(0.2)
        await bootstrapper.event_bus.stop()

        mock_nc.publish.assert_called()
        call_args = mock_nc.publish.call_args
        assert call_args is not None
        assert call_args.args[0] == "ate.status.exec-456"
        published = json.loads(call_args.args[1])
        assert published["type"] == "STEP_STARTED"
        assert published["data"]["step_id"] == "s1"

    @pytest.mark.asyncio
    async def test_worker_heartbeat(self, tmp_path: Path) -> None:
        """Worker should update KV TTL periodically via heartbeat."""
        mock_nc, _, mock_kv = _make_mock_nc()

        worker = JetStreamWorker(
            worker_id_path=str(tmp_path / "worker_id"),
            heartbeat_interval=0.05,
        )
        await worker.start(nc=mock_nc)

        await asyncio.sleep(0.15)

        assert mock_kv.put.call_count >= 2

        first_call = mock_kv.put.call_args_list[0]
        key = first_call.args[0]
        assert key == f"workers.{worker.worker_id}"

        metadata = json.loads(first_call.args[1])
        assert "hostname" in metadata
        assert "max_concurrent_tasks" in metadata

        await worker.stop()

    def test_multiprocessing_isolation(self) -> None:
        """PlanBootstrapper must use ProcessStepExecutor with multiprocessing."""
        plan = YamlPlan(name="test", version="1.0")
        bootstrapper = PlanBootstrapper(plan)
        scheduler = bootstrapper.bootstrap()

        from ate_platform.executor.step_executor import ProcessStepExecutor

        assert isinstance(scheduler._step_executor, ProcessStepExecutor)
        assert scheduler._step_executor._process_executor._use_multiprocessing is True

    def test_worker_id_persisted(self, tmp_path: Path) -> None:
        """Worker ID should be generated once and persisted to file."""
        id_path = tmp_path / "worker_id"

        worker1 = JetStreamWorker(worker_id_path=str(id_path))
        wid1 = worker1.worker_id

        assert id_path.exists()
        assert uuid.UUID(wid1) is not None

        file_content = id_path.read_text(encoding="utf-8").strip()
        assert file_content == wid1

        worker2 = JetStreamWorker(worker_id_path=str(id_path))
        assert worker2.worker_id == wid1

    @pytest.mark.asyncio
    async def test_dict_to_yaml_plan_round_trip(self) -> None:
        """_dict_to_yaml_plan should reconstruct enums from JSON strings."""
        original = YamlPlan(
            name="round-trip",
            version="2.0",
            steps=[
                YamlStep(id="s1", script="a.py", preconditions=["s0"]),
                YamlLoop(
                    id="loop1",
                    loop_type=LoopType.FOR,
                    steps=[YamlStep(id="s2", script="b.py")],
                    count=3,
                ),
            ],
        )
        payload = _serialize_plan(original)
        data = json.loads(payload)
        restored = _dict_to_yaml_plan(data)

        assert restored.name == "round-trip"
        assert restored.version == "2.0"
        assert len(restored.steps) == 2
        assert isinstance(restored.steps[1], YamlLoop)
        assert restored.steps[1].loop_type.value == "FOR"
        assert restored.steps[1].count == 3

    def test_plan_bootstrapper_flattens_loops_recursively(self) -> None:
        """PlanBootstrapper should flatten nested YamlLoop children."""
        from shared.dsl import LoopType

        plan = YamlPlan(
            name="nested",
            version="1.0",
            steps=[
                YamlStep(id="outer", script="a.py"),
                YamlLoop(
                    id="loop1",
                    loop_type=LoopType.FOR,
                    steps=[
                        YamlStep(id="inner1", script="b.py", preconditions=["outer"]),
                        YamlLoop(
                            id="loop2",
                            loop_type=LoopType.WHILE,
                            steps=[YamlStep(id="inner2", script="c.py")],
                        ),
                    ],
                ),
            ],
        )
        bootstrapper = PlanBootstrapper(plan)
        steps = bootstrapper._flatten()

        step_ids = [s[0] for s in steps]
        assert step_ids == ["outer", "loop1", "inner1", "loop2", "inner2"]

        inner1_cond = steps[2][1]
        assert inner1_cond is not None
        assert inner1_cond.step == "outer"
        assert inner1_cond.status == "PASSED"

    def test_plan_bootstrapper_raises_on_deep_nesting(self) -> None:
        """PlanBootstrapper should raise RecursionError on deep nesting."""
        from shared.dsl import LoopType

        def _make_nested(depth: int) -> YamlLoop:
            if depth <= 0:
                return YamlLoop(id="leaf", loop_type=LoopType.FOR, steps=[])
            return YamlLoop(
                id=f"loop{depth}",
                loop_type=LoopType.FOR,
                steps=[_make_nested(depth - 1)],
            )

        plan = YamlPlan(
            name="deep",
            version="1.0",
            steps=[_make_nested(15)],
        )
        bootstrapper = PlanBootstrapper(plan)
        with pytest.raises(RecursionError):
            bootstrapper._flatten()
