# allow: SIZE_OK — PlanBootstrapper + JetStreamWorker + dict→YamlPlan helpers
# must coexist in one file per task constraints (only 2 files allowed).
# Natural seam: split PlanBootstrapper into its own module when next edit adds lines.
"""JetStream Worker — standalone platform process pulling tasks from NATS.

Pulls task messages from the ATE_TASKS durable consumer, deserializes the
YamlPlan JSON payload, boots a ScannerScheduler with all five dependencies,
and drives execution through the scheduler's event-driven architecture.
Worker identity persists to ~/.ate_platform/worker_id (configurable via
ATE_PLATFORM_WORKER_ID_PATH). Registers in ate-workers KV with 15s heartbeat.
Core NATS push subscription on ate.control.* handles abort signals.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from pathlib import Path
from typing import Any

import nats
from nats.aio.client import Client as NatsClient
from nats.errors import TimeoutError as NatsTimeoutError

from shared.dsl import ExecutionMode, LoopType, YamlLoop, YamlPlan, YamlStep
from shared.events import Event, EventType
from shared.types import Condition

from .condition_evaluator import ConditionEvaluator
from .event_bus import EventBus
from .resource_manager import ResourceManager
from .scanner_scheduler import ScannerScheduler
from .step_registry import StepRegistry
from .variable_space import VariableSpace

logger = logging.getLogger(__name__)

_WORKER_KV_BUCKET = "ate-workers"
_TASKS_SUBJECT = "ate.tasks.*"
_TASKS_DURABLE = "ate-worker"
_CONTROL_SUBJECT = "ate.control.*"
_HEARTBEAT_INTERVAL: float = 15.0
_MAX_RECURSION_DEPTH = 10
_DEFAULT_WORKER_ID_PATH = str(Path.home() / ".ate_platform" / "worker_id")

_FORWARDED_EVENT_TYPES = (
    EventType.STEP_STARTED,
    EventType.STEP_COMPLETED,
    EventType.STEP_FAILED,
    EventType.STEP_SKIPPED,
    EventType.STEP_STATUS_CHANGED,
)


class PlanBootstrapper:
    """Flattens a YamlPlan into scheduler-ready state.

    Creates all five ScannerScheduler dependencies, registers steps with
    derived Conditions, and compiles the plan. YamlLoop children are
    flattened recursively (max depth 10).
    """

    def __init__(self, plan: YamlPlan) -> None:
        self._plan = plan
        self.event_bus = EventBus()
        self.variable_space = VariableSpace(event_bus=self.event_bus)
        self.resource_manager = ResourceManager(event_bus=self.event_bus)
        self.step_registry = StepRegistry(event_bus=self.event_bus)
        self.evaluator = ConditionEvaluator(
            {},
            resource_manager=self.resource_manager,
            variable_space=self.variable_space,
        )
        from ..executor.step_executor import ProcessStepExecutor

        self._executor = ProcessStepExecutor(
            max_workers=4,
            script_timeout=60.0,
            event_bus=self.event_bus,
            use_multiprocessing=True,
        )
        self.scheduler = ScannerScheduler(
            event_bus=self.event_bus,
            registry=self.step_registry,
            evaluator=self.evaluator,
            variable_space=self.variable_space,
            resource_manager=self.resource_manager,
            step_executor=self._executor,
        )

    def bootstrap(self, dut_id: str | None = None) -> ScannerScheduler:
        """Register all steps, compile plan, return the booted scheduler."""
        steps = self._flatten()
        for step_id, condition in steps:
            if not self.step_registry.has_step(step_id):
                self.step_registry.register(step_id, condition)
        self.scheduler.compile_plan(steps, dut_id=dut_id)
        return self.scheduler

    def _flatten(self) -> list[tuple[str, Condition | None]]:
        result: list[tuple[str, Condition | None]] = []
        self._flatten_items(self._plan.steps, result, 0)
        return result

    def _flatten_items(
        self,
        items: list[YamlStep | YamlLoop],
        result: list[tuple[str, Condition | None]],
        depth: int,
    ) -> None:
        if depth > _MAX_RECURSION_DEPTH:
            raise RecursionError(
                f"YamlLoop nesting exceeds max depth {_MAX_RECURSION_DEPTH}"
            )
        for item in items:
            if isinstance(item, YamlStep):
                result.append((item.id, self._step_condition(item)))
            else:
                result.append((item.id, self._loop_condition(item)))
                self._flatten_items(item.steps, result, depth + 1)

    @staticmethod
    def _step_condition(step: YamlStep) -> Condition | None:
        if step.skip_if is not None:
            return Condition(expression=step.skip_if, step=None, status=None)
        if step.preconditions:
            return Condition(step=step.preconditions[0], status="PASSED")
        return None

    @staticmethod
    def _loop_condition(loop: YamlLoop) -> Condition | None:
        if loop.skip_if is not None:
            return Condition(expression=loop.skip_if, step=None, status=None)
        return None


def _dict_to_yaml_plan(data: dict[str, Any]) -> YamlPlan:
    """Reconstruct YamlPlan from a JSON-deserialized dict.

    JSON round-trip converts enums to plain strings; this rebuilds
    proper YamlPlan/YamlStep/YamlLoop with correct enum types.
    """
    steps = [_dict_to_step_or_loop(s) for s in data.get("steps", [])]
    return YamlPlan(
        name=data["name"],
        version=data["version"],
        scope=data.get("scope", {}),
        max_concurrency=data.get("max_concurrency", 1),
        steps=steps,
    )


def _dict_to_step_or_loop(data: dict[str, Any]) -> YamlStep | YamlLoop:
    if "loop_type" in data:
        return YamlLoop(
            id=data["id"],
            loop_type=LoopType(data["loop_type"]),
            steps=[_dict_to_step_or_loop(s) for s in data.get("steps", [])],
            count=data.get("count"),
            condition=data.get("condition"),
            collection=data.get("collection"),
            iterator_var=data.get("iterator_var"),
            execution_mode=ExecutionMode(data.get("execution_mode", "SERIAL")),
            max_iterations=data.get("max_iterations", 1000),
            skip_if=data.get("skip_if"),
            skip_reason=data.get("skip_reason"),
        )
    return YamlStep(
        id=data["id"],
        script=data["script"],
        params=data.get("params", {}),
        preconditions=data.get("preconditions", []),
        resources=data.get("resources", {}),
        timeout=data.get("timeout", 60),
        retry=data.get("retry", 0),
        on_fail=data.get("on_fail"),
        export_outputs=data.get("export_outputs", False),
        skip_if=data.get("skip_if"),
        skip_reason=data.get("skip_reason"),
    )


class JetStreamWorker:
    """Standalone platform process pulling tasks from NATS JetStream.

    Pulls task messages from the ATE_TASKS durable consumer, boots a
    ScannerScheduler via PlanBootstrapper, and drives execution through
    the scheduler's event bus. Registers in ate-workers KV with 15s
    heartbeat. Core NATS push subscription on ate.control.* for abort.
    """

    def __init__(
        self,
        nats_url: str = "nats://localhost:4222",
        worker_id_path: str | None = None,
        max_concurrent_tasks: int = 1,
        heartbeat_interval: float = _HEARTBEAT_INTERVAL,
    ) -> None:
        self._nats_url = nats_url
        self._worker_id_path = worker_id_path or os.environ.get(
            "ATE_PLATFORM_WORKER_ID_PATH", _DEFAULT_WORKER_ID_PATH,
        )
        self._worker_id = self._load_or_create_worker_id()
        self._max_concurrent_tasks = max_concurrent_tasks
        self._heartbeat_interval = heartbeat_interval
        self._nc: NatsClient | None = None
        self._js: Any = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._control_sub: Any = None
        self._running = False
        self._current_scheduler: ScannerScheduler | None = None
        self._current_execution_id: str | None = None
        self._current_event_bus: EventBus | None = None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def _load_or_create_worker_id(self) -> str:
        path = Path(self._worker_id_path)
        if path.exists():
            wid = path.read_text(encoding="utf-8").strip()
            if wid:
                return wid
        wid = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(wid, encoding="utf-8")
        return wid

    def _worker_metadata(self) -> dict[str, object]:
        return {
            "hostname": socket.gethostname(),
            "capabilities": ["script_execution"],
            "max_concurrent_tasks": self._max_concurrent_tasks,
            "current_tasks": 0 if self._current_scheduler is None else 1,
        }

    async def start(self, nc: NatsClient | None = None) -> None:
        """Connect to NATS, register in KV, start heartbeat and control sub."""
        if nc is not None:
            self._nc = nc
        else:
            self._nc = await nats.connect(self._nats_url)
        self._js = self._nc.jetstream()

        kv = await self._js.key_value(_WORKER_KV_BUCKET)
        key = f"workers.{self._worker_id}"
        await kv.put(key, json.dumps(self._worker_metadata()).encode("utf-8"))

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(kv))
        self._control_sub = await self._nc.subscribe(
            _CONTROL_SUBJECT, cb=self._on_control_message,
        )
        self._running = True
        logger.info("JetStreamWorker %s started", self._worker_id)

    async def _heartbeat_loop(self, kv: Any) -> None:
        key = f"workers.{self._worker_id}"
        while self._running:
            try:
                payload = json.dumps(self._worker_metadata()).encode("utf-8")
                await kv.put(key, payload)
            except Exception as e:
                logger.warning("Heartbeat failed: %s", e)
            await asyncio.sleep(self._heartbeat_interval)

    async def _on_control_message(self, msg: Any) -> None:
        run_id = msg.subject.rsplit(".", 1)[-1] if "." in msg.subject else ""
        logger.info("Received control signal for run %s", run_id)
        if self._current_execution_id != run_id:
            return

        # Parse the control message payload
        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            payload = {}

        action = payload.get("action", "abort")
        logger.info("Control action '%s' for run %s", action, run_id)

        if action == "abort":
            await self._abort_current()
        elif action == "pause":
            if self._current_scheduler is not None:
                self._current_scheduler.pause()
        elif action == "resume":
            if self._current_scheduler is not None:
                self._current_scheduler.resume()
        elif action == "force_next":
            if self._current_scheduler is not None:
                self._current_scheduler.force_next()
        else:
            logger.warning("Unknown control action '%s' for run %s", action, run_id)

    async def _abort_current(self) -> None:
        if self._current_scheduler is None:
            return
        from ate_platform.types import StepStatus

        registry = self._current_scheduler._registry
        for step_id, status in list(registry.get_all_steps().items()):
            if status == StepStatus.PENDING:
                try:
                    registry.update_status(step_id, StepStatus.ERROR)
                except KeyError:
                    pass
        await self._current_scheduler.stop()
        self._current_scheduler = None
        self._current_execution_id = None

    def _setup_status_forwarding(
        self, event_bus: EventBus, execution_id: str,
    ) -> None:
        """Forward step lifecycle events to NATS ate.status.{execution_id}."""
        subject = f"ate.status.{execution_id}"
        nc = self._nc

        async def forwarder(event: Event) -> None:
            if nc is not None:
                payload = json.dumps(
                    {"type": event.type.value, "data": event.data},
                ).encode("utf-8")
                try:
                    await nc.publish(subject, payload)
                except Exception as e:
                    logger.warning("Status forward failed: %s", e)

        for event_type in _FORWARDED_EVENT_TYPES:
            event_bus.subscribe(event_type, forwarder)

    async def pull_and_process_one(self, timeout: float = 30.0) -> bool:
        """Pull one task, boot scheduler, start event-driven execution."""
        if self._js is None:
            raise RuntimeError("Worker not started — call start() first")

        sub = await self._js.pull_subscribe(_TASKS_SUBJECT, durable=_TASKS_DURABLE)
        try:
            messages = await sub.fetch(batch=1, timeout=timeout)
        except (NatsTimeoutError, TimeoutError):
            return False

        if not messages:
            return False

        msg = messages[0]
        execution_id = ""
        if msg.headers is not None:
            execution_id = msg.headers.get("execution_id", "")
        self._current_execution_id = execution_id

        try:
            data = json.loads(msg.data.decode("utf-8"))
            plan = _dict_to_yaml_plan(data)

            bootstrapper = PlanBootstrapper(plan)
            self._current_scheduler = bootstrapper.bootstrap(dut_id=execution_id)
            self._current_event_bus = bootstrapper.event_bus

            self._setup_status_forwarding(bootstrapper.event_bus, execution_id)

            await bootstrapper.event_bus.start()
            await self._current_scheduler.start()

            await msg.ack()
            logger.info("Booted scheduler for execution %s", execution_id)
            return True
        except Exception:
            logger.exception("Failed to process task %s", execution_id)
            await msg.nak()
            self._current_scheduler = None
            self._current_execution_id = None
            return False

    async def stop(self) -> None:
        """Stop heartbeat, unsubscribe, close NATS connection."""
        self._running = False

        if self._current_scheduler is not None:
            await self._current_scheduler.stop()
            self._current_scheduler = None

        if self._current_event_bus is not None:
            await self._current_event_bus.stop()
            self._current_event_bus = None

        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._control_sub is not None:
            await self._control_sub.unsubscribe()
            self._control_sub = None

        if self._nc is not None:
            await self._nc.close()
            self._nc = None

        logger.info("JetStreamWorker %s stopped", self._worker_id)
