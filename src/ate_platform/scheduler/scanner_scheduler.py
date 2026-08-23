"""Scanner Scheduler for ATE Platform.

This module provides the core scanning scheduler that monitors
step conditions and notifies when steps become ready for execution.

Key features:
- Reactive dispatch: event handlers trigger immediate step evaluation
- Watchdog scan loop (5s default interval) as safety net only
- Subscribes to MEASUREMENT_RECORDED, STEP_STATUS_CHANGED, RESOURCE_RELEASED
- Detects ready steps via StepRegistry.get_ready_steps()
- Emits STEP_STARTED events when conditions are met
- Deadlock detection via WatchDog health monitor (external asyncio task)
- Loop-aware scanning: YamlLoop steps are executed via LoopExecutor
- Dependency index for O(1) lookup of dependents
- Pending-dispatch deduplication prevents double-dispatch
- Heartbeat counter for WatchDog health monitoring
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

# 直接导入子模块（绕过 simulation/__init__ 的重导出链）——fault_injector
# 仅依赖 stdlib+simpleeval，与 scheduler 包无环（§7.7 调度层注入）
from ..exceptions import ExecutionAborted
from ..simulation.fault_injector import SchedulerFaultError
from ..types import Condition, LoopResult, StepStatus
from .adaptive_skip import AdaptiveConditionEvaluator, SkipConditions
from .condition_evaluator import ConditionEvaluator
from .event_bus import Event, EventBus, EventType
from .resource_manager import ResourceManager
from .step_registry import StepRegistry
from .uut_sync import UUTManager, UUTState
from .variable_space import VariableSpace

# Sentinel for "no event loop available" in _schedule_dispatch
_NO_LOOP = object()

# 崩溃时非终态步骤的快照值 → 恢复为 PENDING（重跑）
_TERMINAL_STATES = {"PASSED", "FAILED", "SKIPPED", "ERROR"}


def _is_in_async_context() -> bool:
    """Check if we're currently inside an async event loop."""
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False

if TYPE_CHECKING:
    from ..executor.step_executor import StepExecutor
    from ..simulation.fault_injector import FaultInjector
    from .step_registry import StepExecutionConfig
    from .watchdog import WatchDog

logger = logging.getLogger(__name__)


class DeadlockDetectedError(Exception):
    """Raised when the scheduler detects a potential deadlock.

    A deadlock is detected when the scanner has scanned multiple times
    without any progress (no steps becoming ready and no steps executing).
    """
    pass


class ScannerScheduler:
    """Event-driven scanner that monitors step conditions and triggers execution.

    The ScannerScheduler is the core innovation of the ATE Platform scheduling
    system. It uses reactive dispatch — event handlers trigger immediate step
    evaluation when state changes — with a 5-second watchdog scan loop as a
    safety net for any events that might be missed.

    Architecture:
        - Reactive: on_step_status_changed → _evaluate_dependents → dispatch
        - Reactive: on_variable_changed → dispatch for variable-dependent steps
        - Reactive: on_resource_released → dispatch for resource-blocked steps
        - Watchdog: 5s scan loop runs _emergency_scan() only if no progress
        - Dedup: _pending_dispatch set prevents double-dispatch
        - Index: _dependency_index maps step/variable/resource → dependent steps

    Thread Safety:
        All async operations use asyncio primitives.
        Dependencies (StepRegistry, etc.) have their own thread safety.

    Example:
        >>> scheduler = ScannerScheduler(event_bus, registry, evaluator, variable_space, resource_manager)
        >>> await scheduler.start()
        >>> # ... steps are registered and executed ...
        >>> await scheduler.stop()
    """

    # Default scan interval in seconds (5s watchdog — reactive dispatch handles
    # the fast path; this is only a safety net)
    DEFAULT_SCAN_INTERVAL: float = 5.0

    # Deadlock detection threshold: max consecutive scans without progress
    DEADLOCK_THRESHOLD: int = 100

    def __init__(
        self,
        event_bus: EventBus,
        registry: StepRegistry,
        evaluator: ConditionEvaluator,
        variable_space: VariableSpace,
        resource_manager: ResourceManager,
        scan_interval: float = DEFAULT_SCAN_INTERVAL,
        step_executor: StepExecutor | None = None,
        adaptive_skip_evaluator: AdaptiveConditionEvaluator | None = None,
        snapshot_dir: str | None = None,
        instrument_reset_callback: Callable[[], Awaitable[None]] | None = None,
        fault_injector: FaultInjector | None = None,
        uut_manager: UUTManager | None = None,
    ) -> None:
        """Initialize the scanner scheduler.

        Args:
            event_bus: EventBus for publishing STEP_READY events
            registry: StepRegistry for tracking step statuses
            evaluator: ConditionEvaluator for checking conditions
            variable_space: VariableSpace for variable resolution
            resource_manager: ResourceManager for resource availability
            scan_interval: Time between scans in seconds (default 100ms)
            step_executor: Optional StepExecutor for step execution.
                Defaults to ProcessStepExecutor if not provided.
            adaptive_skip_evaluator: Optional AdaptiveConditionEvaluator for
                context-aware skip conditions (SPC Cpk, fault probability,
                product context). When None, only basic skip_if expressions
                are evaluated.
            snapshot_dir: 启用状态快照（§6.6 崩溃恢复）。提供后，每次步骤
                状态变更会原子落盘；start() 检测到可恢复快照时自动恢复
                步骤状态与变量；正常完成时清理快照。
            instrument_reset_callback: 崩溃恢复时对所有仪器发 *RST 的回调
                （awaitable）。崩溃时仪器可能处于未知状态，恢复必须先重置
                到已知状态再继续（§6.6.4）。None 则跳过仪器重置。
            fault_injector: 可选 FaultInjector（§7.7 调度层故障注入）。
                提供后，每个步骤派发前经 check_scheduler_raise 检查，
                命中规则则按失败处理而非派发。None 表示未接注入器，
                钩子为单次属性判空直通。
            uut_manager: 可选 UUTManager（T2 UUT 亲和调度）。提供后，
                specific 亲和的步骤只在所指 UUT 槽位空闲时才就绪；
                'any'/未注册亲和与未接管理器时行为与之前逐字节一致。
        """
        self._event_bus: EventBus = event_bus
        self._registry: StepRegistry = registry
        self._evaluator: ConditionEvaluator = evaluator
        self._variable_space: VariableSpace = variable_space
        self._resource_manager: ResourceManager = resource_manager
        self._scan_interval: float = scan_interval
        self._fault_injector: FaultInjector | None = fault_injector
        self._uut_manager: UUTManager | None = uut_manager
        self._adaptive_skip_evaluator: AdaptiveConditionEvaluator | None = (
            adaptive_skip_evaluator
        )

        # T2 UUT 亲和调度状态：
        # - _uut_affinities: step_id → 'any' | 具体 UUT id（register_uut_affinities）
        # - _affinity_claims: uut_id → 已派发未终态的 step_id（同 UUT 串行化）
        self._uut_affinities: dict[str, str] = {}
        self._affinity_claims: dict[str, str] = {}

        # T3 DSL BARRIER 步骤注册表：step_id → barrier_name
        # （register_barrier_steps；派发后由 _run_barrier_step 驱动同步）
        self._barrier_steps: dict[str, str] = {}
        self._barrier_default_timeout: float = 60.0

        # Step executor — defaults to ProcessStepExecutor
        if step_executor is not None:
            self._step_executor: StepExecutor = step_executor
        else:
            from ..executor.step_executor import ProcessStepExecutor

            self._step_executor = ProcessStepExecutor(
                max_workers=4,
                script_timeout=60.0,
                event_bus=event_bus,
            )

        # Runtime state
        self._running: bool = False
        self._scan_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

        # Deadlock detection
        self._consecutive_no_progress: int = 0
        self._last_ready_count: int = 0

        # Track steps that have already been notified as ready
        self._notified_ready: set[str] = set()

        # Pending dispatch deduplication: steps currently being dispatched
        # or scheduled for dispatch. Prevents double-dispatch from both
        # reactive handlers and the watchdog scan.
        self._pending_dispatch: set[str] = set()

        # Dependency index: maps source → set of step_ids that depend on it.
        # Built at compile_plan() time for O(1) reactive dispatch lookups.
        # Keys: step_id, variable_name (e.g. "scope.voltage"), resource_name
        self._dependency_index: dict[str, set[str]] = {}

        # Skip condition registry: maps step_id → (skip_if_expression, skip_reason).
        # Populated via register_skip_conditions() before start().
        self._skip_conditions: dict[str, tuple[str, str | None]] = {}

        # Adaptive skip conditions: maps step_id → SkipConditions (typed).
        # Populated via register_adaptive_skip_conditions() before start().
        # When present, _evaluate_step_skip() checks these first, falling back
        # to basic skip_if expressions when no adaptive conditions are registered.
        self._adaptive_skip_conditions: dict[str, SkipConditions] = {}

        # Timestamp of last reactive dispatch — used by watchdog to detect
        # whether any progress happened since the last scan.
        self._last_dispatch_time: float = 0.0

        # Heartbeat counter — incremented at top of each _scan_loop iteration.
        # The WatchDog monitors this to detect scan loop freezes.
        self._heartbeat: int = 0

        # WatchDog health monitor (created in start())
        self._watchdog: WatchDog | None = None

        # F5 执行增强：暂停/强制下一步
        self._paused: bool = False
        self._force_next_pending: bool = False
        # 暂停事件：pause() clear / resume() set，_dispatch_step 等待
        self._pause_event: asyncio.Event = asyncio.Event()
        self._pause_event.set()

        # §6.6 状态快照与崩溃恢复
        self._instrument_reset_callback: Callable[[], Awaitable[None]] | None = (
            instrument_reset_callback
        )
        self._snapshot: StateSnapshot | None = None
        if snapshot_dir is not None:
            from .state_snapshot import StateSnapshot

            self._snapshot = StateSnapshot(snapshot_dir)
        # 本次运行是否从快照恢复（决定 stop() 是否清理快照）
        self._resumed_from_snapshot: bool = False

        # Event handlers (stored for potential unsubscription)
        self._handlers: dict[EventType, Callable[[Event], None]] = {}

    async def start(self) -> None:
        """Start the scanning loop and WatchDog health monitor.

        Subscribes to relevant events, creates the WatchDog, and begins
        the scanning task.

        Safe to call multiple times - subsequent calls are ignored if already running.
        """
        if self._running:
            return

        self._running = True
        self._stop_event.clear()

        # Reset heartbeat counter for fresh monitoring
        self._heartbeat = 0

        # Create and start the WatchDog health monitor
        from .watchdog import WatchDog

        self._watchdog = WatchDog(
            heartbeat_counter=lambda: self._heartbeat,
            scan_interval=5.0,  # WatchDog checks every 5s
            event_bus=self._event_bus,
            emergency_shutdown_callback=self._emergency_shutdown,
        )
        self._watchdog.start()

        # §6.6 崩溃恢复：在订阅事件前恢复（恢复产生的状态事件无订阅者，安全）
        await self._maybe_resume()

        # Subscribe to events that might change step readiness
        self._setup_event_handlers()

        # Start the scanning task
        self._scan_task = asyncio.create_task(self._scan_loop())

        logger.info("ScannerScheduler started with interval %.3fs", self._scan_interval)

    async def stop(self) -> None:
        """Stop the scanning loop and WatchDog gracefully.

        Cancels the scanning task and WatchDog, waits for both to complete.
        Safe to call even if not running.
        """
        if not self._running:
            return

        self._running = False
        self._stop_event.set()

        # Stop WatchDog first — it's independent of the scan task
        if self._watchdog is not None:
            await self._watchdog.stop()
            self._watchdog = None

        # Unsubscribe from events
        self._teardown_event_handlers()

        if self._scan_task is not None:
            try:
                await asyncio.wait_for(self._scan_task, timeout=5.0)
            except TimeoutError:
                if self._scan_task is not None:
                    self._scan_task.cancel()
                    try:
                        await self._scan_task
                    except asyncio.CancelledError:
                        pass
            self._scan_task = None

        # §6.6 正常完成才清理快照：全部步骤到终态视为执行完成。
        # 中途停止（用户中断/异常）保留快照，下次启动可断点续跑。
        if self._snapshot is not None and self._all_steps_terminal():
            self._snapshot.cleanup()
            logger.info("Snapshot cleaned after graceful completion")

        logger.info("ScannerScheduler stopped")

    # ------------------------------------------------------------------
    # §6.6 状态快照与崩溃恢复
    # ------------------------------------------------------------------
    async def _maybe_resume(self) -> None:
        """检测可恢复快照并恢复执行状态（在事件订阅前调用）。

        恢复流程（§6.6）：
        1. 恢复步骤状态到 StepRegistry（终态步骤不再重跑）；
        2. 恢复变量（VariableSpace.restore）；
        3. 重置仪器（*RST）——崩溃时仪器可能处于未知状态；
        4. UUT/夹具状态恢复：本调度器无对应引用，留待上层恢复。

        无快照或无恢复需要时为空操作。
        """
        if self._snapshot is None or not self._snapshot.can_resume():
            return

        snapshot = self._snapshot.load()
        if not snapshot:
            return

        # 1. 恢复步骤状态
        step_states = snapshot.get("step_states", {})
        restored = 0
        for step_id, raw_state in step_states.items():
            if not isinstance(raw_state, str):
                continue
            # 崩溃时 RUNNING/PENDING 视为未完成，重跑（回退 PENDING）
            target = raw_state if raw_state in _TERMINAL_STATES else "PENDING"
            try:
                self._registry.update_status(step_id, StepStatus(target))
                restored += 1
            except ValueError:
                logger.warning(
                    "Snapshot restore: invalid status for %s: %s", step_id, raw_state
                )

        # 2. 恢复变量
        variables = snapshot.get("variables")
        if isinstance(variables, dict):
            self._variable_space.restore(variables)

        # 3. 重置仪器（崩溃后强制 *RST，确保已知状态）
        if self._instrument_reset_callback is not None:
            try:
                await self._instrument_reset_callback()
                logger.info("Instruments reset after crash recovery")
            except Exception:
                logger.exception("Instrument reset failed during recovery")

        self._resumed_from_snapshot = True
        logger.info(
            "Crash recovery: restored %d/%d step states",
            restored,
            len(step_states),
        )

    def _get_state(self) -> dict[str, Any]:
        """构建当前执行状态快照（§6.6）。"""
        state: dict[str, Any] = {
            "step_states": {
                step_id: status.value
                for step_id, status in self._registry.get_all_steps().items()
            },
            "variables": self._variable_space.snapshot(),
            "timestamp": time.time(),
        }
        return state

    def _maybe_snapshot(self) -> None:
        """步骤状态变更后原子保存快照（仅在启用时）。"""
        if self._snapshot is None:
            return
        try:
            self._snapshot.save(self._get_state())
        except Exception:
            # 快照失败不应中断执行：记录并继续（下个状态变更会重试）
            logger.exception("State snapshot save failed")

    def _all_steps_terminal(self) -> bool:
        """全部已注册步骤是否都已到达终态（PASSED/FAILED/SKIPPED/ERROR）。"""
        states = self._registry.get_all_steps()
        if not states:
            return False
        return all(
            s.value in _TERMINAL_STATES for s in states.values()
        )

    def compile_plan(self, steps: list[tuple[str, Condition | None]]) -> None:
        """Build the dependency index from a list of (step_id, condition) pairs.

        Must be called after all steps are registered but before start().
        The index enables O(1) lookup of which steps depend on a given
        step, variable, or resource — powering reactive dispatch.

        Args:
            steps: List of (step_id, condition) tuples for all plan steps.
                condition may be None for unconditional steps.
        """
        self._dependency_index.clear()

        for step_id, condition in steps:
            if condition is None:
                continue

            # Step dependency: condition.step → this step
            if condition.step is not None:
                self._dependency_index.setdefault(condition.step, set()).add(step_id)

            # Variable dependency: parse ${...} references in expression
            if condition.expression is not None:
                import re
                for match in re.finditer(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)*)\}", condition.expression):
                    var_name = match.group(1)
                    self._dependency_index.setdefault(var_name, set()).add(step_id)

            # Resource dependency: resource_available → this step
            if condition.resource_available is not None:
                for resource_name in condition.resource_available:
                    self._dependency_index.setdefault(resource_name, set()).add(step_id)

        logger.debug(
            "Dependency index built: %d source keys, %d total edges",
            len(self._dependency_index),
            sum(len(v) for v in self._dependency_index.values()),
        )

    def register_skip_conditions(
        self,
        skip_conditions: dict[str, tuple[str, str | None]],
    ) -> None:
        """Register skip_if expressions for steps and loops.

        These are evaluated before dispatching a step. If the expression
        evaluates to True, the step is marked SKIPPED instead of being
        dispatched for execution.

        Args:
            skip_conditions: Mapping of step_id to (skip_if_expression, skip_reason).
                skip_reason may be None.
        """
        self._skip_conditions = dict(skip_conditions)

    def register_adaptive_skip_conditions(
        self,
        adaptive_skip_conditions: dict[str, SkipConditions],
    ) -> None:
        """Register context-aware adaptive skip conditions for steps.

        These are evaluated before dispatching a step, taking precedence
        over basic skip_if expressions. The AdaptiveConditionEvaluator
        integrates SPC Cpk, FaultPredictor probability, step results,
        and product context variables to decide whether a step should
        be skipped.

        If a step has both adaptive and basic skip_if conditions,
        adaptive conditions are checked first. If adaptive conditions
        match, the step is skipped immediately. Otherwise, the basic
        skip_if expression is evaluated as a fallback.

        Args:
            adaptive_skip_conditions: Mapping of step_id → SkipConditions (typed).
        """
        self._adaptive_skip_conditions = dict(adaptive_skip_conditions)

    def register_uut_affinities(self, affinities: dict[str, str]) -> None:
        """Register per-step UUT affinity（T2）。

        Each entry maps step_id → ``'any'`` or a specific UUT id. A
        specific affinity makes the step wait until that UUT's slot is
        free before it becomes schedulable; ``'any'`` keeps today's
        default semantics.

        When a UUTManager is attached, an affinity naming a nonexistent
        UUT id raises ValueError here — plan validation fails fast
        before start() instead of surfacing at first scan.

        Args:
            affinities: Mapping of step_id to affinity value.

        Raises:
            ValueError: If a specific affinity does not match any UUT
                in the attached pool.
        """
        if self._uut_manager is not None:
            known = set(self._uut_manager.uut_ids)
            for step_id, affinity in affinities.items():
                if affinity and affinity != "any" and affinity not in known:
                    msg = (
                        f"Step '{step_id}' declares uut_affinity '{affinity}' "
                        f"but no such UUT exists in the pool {sorted(known)}"
                    )
                    raise ValueError(msg)
        self._uut_affinities.update(affinities)

    def register_barrier_steps(
        self,
        barriers: dict[str, str],
        *,
        default_timeout: float = 60.0,
    ) -> None:
        """Register DSL BARRIER steps（T3）。

        Each entry maps step_id → barrier_name（SyncBarrier 分组名）。当
        这样的步骤被派发（响应式或看门狗路径）时，调度器在 STEP_STARTED
        之后调用 :meth:`UUTManager.wait_barrier` 驱动全池同步 —— 屏障
        参与者始终是管理器内的全部 UUT，满员放行、超时缺员按失败处理。

        Args:
            barriers: Mapping of step_id to barrier name.
            default_timeout: 每次屏障等待的默认超时秒数。

        Raises:
            ValueError: If a barrier name is empty/whitespace — plan
                validation fails fast before start() (T2 fail-fast style).
        """
        for step_id, name in barriers.items():
            if not name or not name.strip():
                msg = f"Step '{step_id}' declares an empty barrier_name"
                raise ValueError(msg)
        self._barrier_steps = dict(barriers)
        self._barrier_default_timeout = default_timeout

    def _can_schedule(self, step_id: str) -> bool:
        """UUT-affinity readiness gate（T2，_scan_ready/_can_schedule 语义）。

        - 'any'/未注册亲和：立即放行，不触碰 UUT 池 —— 与 T2 前行为
          逐字节一致。
        - specific 亲和：所指 UUT 槽位必须空闲 —— 既不能在 UUTManager
          中处于 TESTING，也不能已被另一个已派发未终态的步骤声明占用。
        - 未接 UUTManager：直通（向后兼容）。

        本门只是**额外的就绪前置条件**：通过后仍照常走资源检查与
        ResourceLock 授予，绝不绕过或替代 ResourceManager。

        Args:
            step_id: The step to gate.

        Returns:
            True if the step may proceed toward dispatch.
        """
        affinity = self._uut_affinities.get(step_id)
        if not affinity or affinity == "any":
            return True
        manager = self._uut_manager
        if manager is None:
            return True
        uut = manager.get(affinity)
        if uut is None:
            # register_uut_affinities 已校验；防御性直通
            return True
        if uut.busy:
            return False
        occupant = self._affinity_claims.get(affinity)
        return occupant is None or occupant == step_id

    def _claim_uut(self, step_id: str) -> None:
        """Record that a dispatched specific-affinity step occupies its UUT.

        Called on both dispatch paths right before STEP_STARTED emission;
        released when the step reaches a terminal status.
        """
        affinity = self._uut_affinities.get(step_id)
        if affinity and affinity != "any" and self._uut_manager is not None:
            self._affinity_claims.setdefault(affinity, step_id)

    def _release_uut_claim(self, step_id: str) -> None:
        """Drop the UUT claim held by step_id（终态时经状态事件调用）。"""
        affinity = self._uut_affinities.get(step_id)
        if affinity and affinity != "any":
            if self._affinity_claims.get(affinity) == step_id:
                del self._affinity_claims[affinity]

    def _setup_event_handlers(self) -> None:
        """Set up event subscriptions with reactive dispatch wiring."""
        scheduler_self = self  # Capture for closures

        # Variable changed handler — triggers dispatch for variable-dependent steps
        def on_variable_changed(event: Event) -> None:
            name = event.data.get("name", "unknown")
            logger.debug("Measurement recorded: %s — evaluating dependents", name)
            scheduler_self._schedule_dispatch_for_key(name)

        # Step status changed handler — triggers _evaluate_dependents and dispatch
        def on_step_status_changed(event: Event) -> None:
            step_id = event.data.get("step_id")
            new_status = event.data.get("new_status")

            # If step completed, remove from notified_ready to allow re-scan
            if new_status in ("PASSED", "FAILED", "SKIPPED", "ERROR"):
                scheduler_self._notified_ready.discard(step_id)
                # Remove from pending_dispatch since it's done
                scheduler_self._pending_dispatch.discard(step_id)
                # T2：终态释放 UUT 占用，等待同 UUT 的 pinned 步骤可就绪
                scheduler_self._release_uut_claim(step_id)

            logger.debug("Step status changed: %s -> %s — evaluating dependents", step_id, new_status)
            # §6.6 状态变更后原子保存快照（崩溃恢复用）
            scheduler_self._maybe_snapshot()
            # Trigger reactive dispatch for steps that depend on this step
            if step_id is not None:
                scheduler_self._schedule_dispatch_for_key(step_id)

        # Resource released handler — triggers dispatch for resource-blocked steps
        def on_resource_released(event: Event) -> None:
            resource_id = event.data.get("resource_id", "unknown")
            logger.debug("Resource released: %s — evaluating dependents", resource_id)
            scheduler_self._schedule_dispatch_for_key(resource_id)

        # Store handlers — MEASUREMENT_RECORDED replaces deprecated VARIABLE_CHANGED
        self._handlers[EventType.MEASUREMENT_RECORDED] = on_variable_changed
        self._handlers[EventType.STEP_STATUS_CHANGED] = on_step_status_changed
        self._handlers[EventType.RESOURCE_RELEASED] = on_resource_released

        # Subscribe
        for event_type, handler in self._handlers.items():
            self._event_bus.subscribe(event_type, handler)

    def _teardown_event_handlers(self) -> None:
        """Remove event subscriptions."""
        for event_type, handler in self._handlers.items():
            _ = self._event_bus.unsubscribe(event_type, handler)
        self._handlers.clear()

    async def _scan_loop(self) -> None:
        """Watchdog scanning loop (5s default interval).

        This is NOT the primary dispatch mechanism — reactive event handlers
        handle immediate dispatch. This loop runs as a safety net to catch
        any events that might have been missed or state changes not
        surfaced through the EventBus.

        Only runs _emergency_scan() if no progress has been detected
        since the last iteration (i.e., _last_dispatch_time hasn't changed).

        Heartbeat counter is incremented at the top of each iteration
        for WatchDog health monitoring.
        """

        while self._running and not self._stop_event.is_set():
            # Increment heartbeat for WatchDog health monitoring
            self._heartbeat += 1

            try:
                # Check if any reactive dispatch happened since last scan
                current_dispatch_time = self._last_dispatch_time
                await asyncio.sleep(0)  # Yield to let reactive handlers run

                if current_dispatch_time == self._last_dispatch_time and current_dispatch_time > 0:
                    # No progress since last scan — run emergency scan
                    await self._emergency_scan()
                elif current_dispatch_time == 0.0:
                    # First scan or no dispatch yet — do a full scan
                    await self._emergency_scan()
            except Exception as e:
                logger.error("Error during watchdog scan: %s", e)

            # Wait for next scan interval or stop signal
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._scan_interval
                )
                # If we reach here, stop_event was set
                break
            except TimeoutError:
                # Normal timeout, continue scanning
                pass

    async def _emergency_scan(self) -> None:
        """Emergency scan: full get_ready_steps() + dispatch.

        Called by the watchdog scan loop when no reactive progress has been
        detected. This is the fallback path for catching missed events.
        Also runs on the first iteration after start().

        Note: Deadlock detection has been moved to the WatchDog health
        monitor (watchdog.py) which runs in its own independent asyncio task.
        """
        from shared.events import StepStartedData

        # Get all steps that are ready to execute
        ready_steps = self._registry.get_ready_steps(
            variable_space=self._variable_space,
            resource_manager=self._resource_manager,
        )

        # Emit STEP_STARTED for newly ready steps (dedup via _pending_dispatch)
        for step_id in ready_steps:
            if step_id in self._notified_ready or step_id in self._pending_dispatch:
                continue

            # Check skip conditions before dispatching (adaptive + basic)
            if self._evaluate_step_skip(step_id):
                reason = self._get_skip_reason(step_id)
                await self._handle_step_skipped(step_id, reason)
                continue

            # Get condition for the step (if any)
            condition = self._registry.get_condition(step_id)

            # Verify condition is actually met (double-check)
            if condition is not None and not self._check_condition(condition):
                continue

            # T2 UUT 亲和门：specific 亲和的步骤等所指 UUT 槽位空闲
            # （在故障注入之前 —— 注入计数应反映真实派发尝试）
            if not self._can_schedule(step_id):
                continue

            # §7.7 调度层故障注入：命中则按失败处理，跳过本次派发
            if await self._check_scheduler_fault(step_id):
                continue

            # Mark as pending to prevent double-dispatch
            self._pending_dispatch.add(step_id)

            # T2：派发即声明占用所指 UUT（终态时经状态事件释放）
            self._claim_uut(step_id)

            # Emit STEP_STARTED event with normalized schema
            event_data = asdict(StepStartedData(
                step_id=step_id,
                condition=str(condition) if condition else None,
            ))
            await self._event_bus.publish(
                EventType.STEP_STARTED,
                event_data,
            )

            # Mark as notified
            self._notified_ready.add(step_id)
            self._last_dispatch_time = __import__("time").time()

            # T3：BARRIER 步骤派发即驱动全池同步（复用 UUTManager.wait_barrier）。
            # 必须在 notified_ready 登记之后：屏障结果经状态事件内联路由，
            # 终态分支要从 notified_ready/pending_dispatch 摘除本步骤，
            # repeat 策略置回的 PENDING 才能被下一次扫描重新派发。
            if step_id in self._barrier_steps:
                await self._run_barrier_step(step_id)

            logger.debug("Emergency scan dispatched: %s", step_id)

    def _schedule_dispatch_for_key(self, source_key: str) -> None:
        """Schedule reactive dispatch for steps depending on source_key.

        Called from event handlers (sync callbacks). Looks up the
        dependency index and schedules async dispatch for all dependent
        steps that are not already pending.

        Args:
            source_key: The step_id, variable name, or resource name
                that changed.
        """
        dependent_steps = self._dependency_index.get(source_key, set())
        if not dependent_steps:
            return

        for step_id in dependent_steps:
            # Skip if already dispatched or pending
            if step_id in self._notified_ready or step_id in self._pending_dispatch:
                continue

            self._pending_dispatch.add(step_id)

            # Schedule async dispatch — try to use the running event loop
            loop = self._get_event_loop()
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._dispatch_step(step_id), loop
                )
            elif _is_in_async_context():
                # We're in an async context — create a task
                _ = asyncio.create_task(self._dispatch_step(step_id))
            else:
                # No loop available — remove from pending, let watchdog catch it
                self._pending_dispatch.discard(step_id)
                logger.debug(
                    "No event loop for reactive dispatch of %s — deferring to watchdog",
                    step_id,
                )

    async def _dispatch_step(self, step_id: str) -> None:
        """Dispatch a single step via reactive path.

        Checks if the step is ready and emits STEP_STARTED if so.
        Removes from _pending_dispatch when done (success or failure).

        Uses _registry.get_ready_steps() rather than the stale
        _evaluator to check conditions. The registry builds a fresh
        ConditionEvaluator from current step statuses on each call.

        Args:
            step_id: The step to dispatch.
        """
        import time

        try:
            from shared.events import StepStartedData

            # 暂停时阻塞直到 resume()（F5）
            await self._pause_event.wait()

            # Verify step is still pending (status might have changed)
            try:
                status = self._registry.get_status(step_id)
            except KeyError:
                return  # Step was unregistered

            if status.value != "PENDING":
                return  # No longer pending

            # force_next 一次性消耗：绕过 skip 检查（F5）
            force_next = self._force_next_pending
            if force_next:
                self._force_next_pending = False

            # Check skip conditions before any other work (adaptive + basic + config)
            if not force_next and self._evaluate_step_skip(step_id):
                reason = self._get_skip_reason(step_id)
                await self._handle_step_skipped(step_id, reason)
                return

            # Check if already notified (race with emergency scan)
            if step_id in self._notified_ready:
                return

            # Check if step is actually ready according to the registry.
            # The registry's get_ready_steps() builds a fresh ConditionEvaluator
            # from current step statuses — no stale evaluator risk.
            ready_steps = self._registry.get_ready_steps(
                variable_space=self._variable_space,
                resource_manager=self._resource_manager,
            )
            if step_id not in ready_steps:
                return  # Condition not met yet

            # T2 UUT 亲和门：specific 亲和的步骤等所指 UUT 槽位空闲。
            # 不标记 notified —— 看门狗后续扫描会在 UUT 释放后重派。
            if not self._can_schedule(step_id):
                return

            # §7.7 调度层故障注入：派发前最后检查（命中则按失败处理，
            # 不标记 notified —— 重试路径可再次派发）
            if await self._check_scheduler_fault(step_id):
                return

            # Mark as notified
            self._notified_ready.add(step_id)
            self._last_dispatch_time = time.time()

            # Get condition for logging
            condition = self._registry.get_condition(step_id)

            # Check pool exhaustion before emitting STEP_STARTED
            await self._check_pool_exhaustion(step_id, condition)

            # T2：派发即声明占用所指 UUT（终态时经状态事件释放）
            self._claim_uut(step_id)

            # Emit STEP_STARTED event
            event_data = asdict(StepStartedData(
                step_id=step_id,
                condition=str(condition) if condition else None,
            ))
            await self._event_bus.publish(
                EventType.STEP_STARTED,
                event_data,
            )

            # T3：BARRIER 步骤派发即驱动全池同步（复用 UUTManager.wait_barrier）
            if step_id in self._barrier_steps:
                await self._run_barrier_step(step_id)

            logger.debug("Reactive dispatch: %s", step_id)
        except Exception as e:
            logger.error("Reactive dispatch failed for %s: %s", step_id, e)
        finally:
            self._pending_dispatch.discard(step_id)

    async def _check_pool_exhaustion(
        self, step_id: str, condition: Any,
    ) -> None:
        """Check worker pool saturation and detect deadlock risk.

        Called before emitting STEP_STARTED. Checks pool utilization and
        cross-references the step's required resources with currently-held
        locks. If the pool is full and the step's resources are held by
        currently-running workers, there is a deadlock risk.

        Args:
            step_id: The step about to be dispatched
            condition: The step's Condition (may be None)
        """
        # Get pool stats
        stats = self._step_executor.pool_stats()
        utilization = stats.get("utilization", 0.0)

        # Only check when pool is saturated (utilization >= 1.0)
        if utilization < 1.0:
            return

        # Determine resources this step needs
        required_resources: list[str] = []
        if condition is not None and hasattr(condition, "resource_available"):
            ra = getattr(condition, "resource_available", None)
            if ra is not None:
                required_resources = list(ra)

        # No resource requirements → just saturation, not deadlock
        if not required_resources:
            logger.warning(
                "Pool saturated (%.1f%%), step %s queued (no resource risk)",
                utilization * 100,
                step_id,
            )
            return

        # Get active locks from ResourceManager
        active_locks = self._resource_manager.get_active_locks()

        # Cross-reference: are any required resources held by active workers?
        blocked_resources: list[str] = []
        holding_workers: list[str] = []

        for resource_id in required_resources:
            lock_info = active_locks.get(resource_id)
            if lock_info is not None:
                blocked_resources.append(resource_id)
                owner = lock_info.get("owner", "unknown")
                if owner not in holding_workers:
                    holding_workers.append(owner)

        if blocked_resources:
            # Deadlock risk detected — publish WORKER_EXHAUSTED alarm
            from dataclasses import asdict

            from shared.events import EventType, WorkerExhaustedData

            alarm_data = asdict(WorkerExhaustedData(
                pool_name="default",
                active_workers=stats.get("active", 0),
                max_workers=stats.get("max", 0),
                severity="warning",
                recoverable=True,
                deadlock_risk=True,
                blocked_resources=blocked_resources,
                holding_workers=holding_workers,
            ))
            await self._event_bus.publish(
                EventType.WORKER_EXHAUSTED,
                alarm_data,
            )
            logger.warning(
                "Pool saturated — deadlock risk for step %s: "
                "needs resources %s held by workers %s",
                step_id,
                blocked_resources,
                holding_workers,
            )
        else:
            # Pool saturated but no deadlock risk
            logger.warning(
                "Pool saturated (%.1f%%), step %s queued "
                "(resources available, awaiting worker slot)",
                utilization * 100,
                step_id,
            )

    def _get_event_loop(self) -> asyncio.AbstractEventLoop | None:
        """Get the event loop stored on the EventBus, if any.

        The EventBus stores a loop reference via set_event_loop().
        We use it for scheduling reactive dispatch from sync callbacks.
        """
        return getattr(self._event_bus, "_loop", None)

    def _evaluate_skip_expression(self, expression: str) -> bool:
        """Evaluate a skip_if expression against the current variable space.

        Creates a fresh ConditionEvaluator with the current variable space
        and step registry state, then delegates to evaluate_skip_condition().

        Args:
            expression: The skip_if expression string (e.g. '${scope.skip_tests}')

        Returns:
            True if the step should be skipped, False otherwise
        """
        # Build step_results dict from current registry state
        all_steps = self._registry.get_all_steps()
        step_results: dict[str, Any] = {}
        for sid, st in all_steps.items():
            step_results[sid] = {"status": st, "outputs": {}}

        evaluator = ConditionEvaluator(
            step_results,
            resource_manager=self._resource_manager,
            variable_space=self._variable_space,
        )
        return evaluator.evaluate_skip_condition(expression)

    def _evaluate_step_skip(self, step_id: str) -> bool:
        """Evaluate whether a step should be skipped (adaptive + basic).

        Checks adaptive skip conditions first (context-aware: SPC Cpk,
        fault probability, product context, step results). If adaptive
        conditions are registered for this step and match, returns True.

        Falls back to basic skip_if expression evaluation when no
        adaptive conditions are registered or when adaptive conditions
        don't match but a basic skip_if expression exists.

        Args:
            step_id: The step to evaluate.

        Returns:
            True if the step should be skipped, False otherwise.
        """
        # Phase 1: Check adaptive skip conditions (context-aware)
        adaptive_cond = self._adaptive_skip_conditions.get(step_id)
        if adaptive_cond is not None and self._adaptive_skip_evaluator is not None:
            # Update step_results in the evaluator from current registry state
            all_steps = self._registry.get_all_steps()
            step_results: dict[str, Any] = {}
            for sid, st in all_steps.items():
                step_results[sid] = {"status": st, "outputs": {}}
            self._adaptive_skip_evaluator._step_results = step_results
            self._adaptive_skip_evaluator._variable_space = self._variable_space

            if self._adaptive_skip_evaluator.should_skip(adaptive_cond):
                return True

        # Phase 2: Fall back to basic skip_if expression
        skip_info = self._skip_conditions.get(step_id)
        if skip_info is not None:
            skip_expr, _skip_reason = skip_info
            return self._evaluate_skip_expression(skip_expr)

        # Phase 3: StepExecutionConfig.skip_if（F5，注册时携带的配置）
        try:
            config = self._registry.get_config(step_id)
        except KeyError:
            config = None
        if config is not None and config.skip_if:
            return self._evaluate_skip_expression(config.skip_if)

        return False

    def _get_skip_reason(self, step_id: str) -> str:
        """Get the human-readable skip reason for a step.

        Checks adaptive skip conditions first (for their declared reason),
        then falls back to the basic skip_if reason or expression.

        Args:
            step_id: The step to get the reason for.

        Returns:
            Human-readable reason string.
        """
        # Adaptive reason
        adaptive_cond = self._adaptive_skip_conditions.get(step_id)
        if adaptive_cond is not None and self._adaptive_skip_evaluator is not None:
            reason = self._adaptive_skip_evaluator.evaluate_skip_reason(adaptive_cond)
            if reason:
                return reason

        # Basic skip_if reason
        skip_info = self._skip_conditions.get(step_id)
        if skip_info is not None:
            skip_expr, skip_reason = skip_info
            return skip_reason or f"skip_if: {skip_expr}"

        # StepExecutionConfig.skip_if reason（F5）
        try:
            config = self._registry.get_config(step_id)
        except KeyError:
            config = None
        if config is not None and config.skip_if:
            return f"skip_if: {config.skip_if}"

        return "Adaptive skip condition met"

    async def _check_scheduler_fault(self, step_id: str) -> bool:
        """调度层故障注入派发前检查（§7.7）。

        命中注入规则时把步骤置为 FAILED、发布 STEP_FAILED（错误文本携带
        ``layer=scheduler`` 归因与步骤/规则上下文），并走
        :meth:`handle_step_result` 的既有重试/重复决策矩阵 —— 不中断
        调度循环。

        只捕获 SchedulerFaultError；其他异常（如中止类）原样向上传播。

        Args:
            step_id: 即将派发的步骤。

        Returns:
            True 表示已按注入失败处理，调用方应停止本次派发。
        """
        injector = self._fault_injector
        if injector is None:
            return False
        try:
            injector.check_scheduler_raise(step_id)
        except SchedulerFaultError as exc:
            logger.warning(
                "Scheduler fault injected before dispatch of %s: %s", step_id, exc
            )
            try:
                self._registry.update_status(step_id, StepStatus.FAILED)
            except KeyError:
                self._registry.register(step_id)
                self._registry.update_status(step_id, StepStatus.FAILED)
            from shared.events import StepFailedData

            await self._event_bus.publish(
                EventType.STEP_FAILED,
                asdict(StepFailedData(step_id=step_id, error=str(exc))),
            )
            _ = await self.handle_step_result(step_id, StepStatus.FAILED)
            return True
        return False

    async def _run_barrier_step(self, step_id: str) -> None:
        """驱动已派发的 BARRIER 步骤完成多 UUT 同步（T3，§6.3.7）。

        复用 :meth:`UUTManager.wait_barrier` / ``SyncBarrier`` —— 调度器
        只负责**驱动到达**，绝不重新实现屏障逻辑：

        - 屏障参与者始终是管理器内的全部 UUT（wait_barrier 内部以
          全池 id 集合构造 SyncBarrier）；
        - 空闲 UUT 各自一线程并发到达（``threading.Condition`` 的阻塞
          等待放在工作线程，不卡事件循环）；
        - 忙态 UUT 正在别处执行、无法到达 —— 是天然的缺员；
        - 超时强制解除：已到达者放行继续；缺员 UUT 标记 FAILED
          （wait_barrier 内部标非忙缺员，忙态缺员在此补标）；
        - 满员/超时后屏障被消费清理，同名屏障可在后续同步点复用。

        结果路由与既有失败路径一致：成功置 PASSED；超时缺员发布
        STEP_FAILED 并经 :meth:`handle_step_result` 决策矩阵处理
        （repeat/retry 策略照常生效）。

        Args:
            step_id: 已派发的 BARRIER 步骤标识。
        """
        barrier_name = self._barrier_steps.get(step_id)
        manager = self._uut_manager
        if manager is None or not barrier_name:
            # 未接管理器：无同步对象，直通通过（与 T2 未接直通语义一致）
            logger.debug("Barrier step %s has no UUT pool — pass-through", step_id)
            self._registry.update_status(step_id, StepStatus.PASSED)
            _ = await self.handle_step_result(step_id, StepStatus.PASSED)
            return

        timeout = self._barrier_default_timeout
        idle_ids: list[str] = []
        for uid in manager.uut_ids:
            uut = manager.get(uid)
            if uut is None or not uut.busy:
                idle_ids.append(uid)

        if not idle_ids:
            # 全池皆忙：无人能到达，立即按缺员失败处理（不空等超时窗口）
            missing = set(manager.uut_ids)
        else:
            results = await asyncio.gather(
                *(
                    asyncio.to_thread(manager.wait_barrier, barrier_name, uid, timeout)
                    for uid in idle_ids
                )
            )
            missing: set[str] = set()
            for result in results:
                missing |= result.missing

        if not missing:
            # 全员到达 → 步骤通过（状态事件同步触发依赖派发与占用释放）
            self._registry.update_status(step_id, StepStatus.PASSED)
            _ = await self.handle_step_result(step_id, StepStatus.PASSED)
            return

        # 缺员兜底标记：wait_barrier 内部只标非忙缺员；忙态缺员在此补标
        for uid in sorted(missing):
            uut = manager.get(uid)
            if uut is not None and uut.state != UUTState.FAILED:
                uut.finish(passed=False)
                uut.last_error = f"Barrier '{barrier_name}' timeout"

        error = f"Barrier '{barrier_name}' timeout: missing {sorted(missing)}"
        logger.warning("Barrier step %s failed: %s", step_id, error)
        try:
            self._registry.update_status(step_id, StepStatus.FAILED)
        except KeyError:
            self._registry.register(step_id)
            self._registry.update_status(step_id, StepStatus.FAILED)

        from shared.events import StepFailedData

        await self._event_bus.publish(
            EventType.STEP_FAILED,
            asdict(StepFailedData(step_id=step_id, error=error)),
        )
        _ = await self.handle_step_result(step_id, StepStatus.FAILED)

    async def _handle_step_skipped(self, step_id: str, reason: str) -> None:
        """Mark a step as SKIPPED and publish the STEP_SKIPPED event.

        Sets the step status to SKIPPED in the registry, publishes
        a STEP_SKIPPED event, and triggers cascade evaluation for
        dependent steps (which treat SKIPPED as a satisfied
        precondition).

        Args:
            step_id: The step to mark as skipped
            reason: Human-readable reason for skipping
        """
        import time

        from shared.events import StepSkippedData

        logger.info("Step %s skipped: %s", step_id, reason)

        # Update registry status
        try:
            self._registry.update_status(step_id, StepStatus.SKIPPED)
        except KeyError:
            # Step may not be registered yet — register and set SKIPPED
            self._registry.register(step_id)
            self._registry.update_status(step_id, StepStatus.SKIPPED)

        # Remove from notified/pending sets
        self._notified_ready.discard(step_id)
        self._last_dispatch_time = time.time()

        # Publish STEP_SKIPPED event
        event_data = asdict(StepSkippedData(
            step_id=step_id,
            reason=reason,
        ))
        await self._event_bus.publish(
            EventType.STEP_SKIPPED,
            event_data,
        )

        # Cascade to dependents — SKIPPED satisfies preconditions,
        # so dependents can now proceed
        self._schedule_dispatch_for_key(step_id)

    def _check_condition(self, condition: Condition) -> bool:
        """Check if a condition is currently met.

        Args:
            condition: The condition to check

        Returns:
            True if the condition is met, False otherwise
        """
        try:
            return self._evaluator.evaluate(condition)
        except Exception:
            return False

    async def _handle_potential_deadlock(self) -> None:
        """Handle a potential deadlock situation.

        Logs a warning and emits a DEADLOCK_DETECTED alarm event with severity/recoverable.
        """
        from shared.events import DeadlockDetectedData

        all_steps = self._registry.get_all_steps()
        pending_steps = [
            sid for sid, status in all_steps.items()
            if status.value == "PENDING"
        ]

        logger.warning(
            "Potential deadlock detected: %d consecutive scans without progress. "
            "Pending steps: %s",
            self._consecutive_no_progress,
            pending_steps
        )

        # Reset the counter to allow continued operation
        self._consecutive_no_progress = 0

        # Emit a DEADLOCK_DETECTED alarm event with normalized schema
        event_data = asdict(DeadlockDetectedData(
            pending_steps=pending_steps,
            consecutive_scans=self._consecutive_no_progress,
            severity="critical",
            recoverable=False,
        ))
        await self._event_bus.publish(
            EventType.DEADLOCK_DETECTED,
            event_data,
        )

    async def _emergency_shutdown(self) -> None:
        """Emergency shutdown callback invoked by WatchDog on heartbeat loss.

        Stops the scheduler gracefully: sets running flag to False,
        cancels the scan task, and logs the shutdown reason.
        This method is called as an async callback by the WatchDog.
        """
        logger.critical(
            "ScannerScheduler: emergency shutdown triggered by WatchDog (heartbeat lost)"
        )

        # Stop the scheduler's own scan loop
        self._running = False
        self._stop_event.set()

        if self._scan_task is not None and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
            self._scan_task = None

    def force_scan(self) -> None:
        """Force an immediate scan (non-blocking).

        Schedules an emergency scan on the event loop. Useful for
        triggering an immediate check after external state changes
        that don't go through the EventBus.
        """
        loop = self._get_event_loop()
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(self._emergency_scan(), loop)
        elif _is_in_async_context():
            _ = asyncio.create_task(self._emergency_scan())

    async def execute_loop_step(
        self,
        loop: Any,
        run_id: str | None = None,
    ) -> LoopResult:
        """Execute a YamlLoop step using LoopExecutor.

        When the scheduler encounters a YamlLoop step, this method creates
        a LoopExecutor and executes the loop. After completion, the loop
        step is marked as PASSED/FAILED in the StepRegistry and the result
        is stored in VariableSpace under loop.<loop_id>.result.

        Args:
            loop: The YamlLoop to execute
            run_id: Optional execution run identifier

        Returns:
            LoopResult with aggregated status and per-iteration results
        """
        from ..executor.loop_executor import LoopExecutor

        # Create a LoopExecutor with the scheduler's step executor
        loop_executor = LoopExecutor(
            executor=self._step_executor,
            event_bus=self._event_bus,
            variable_space=self._variable_space,
        )

        # Mark the loop step as RUNNING in the registry
        try:
            self._registry.update_status(loop.id, StepStatus.RUNNING)
        except KeyError:
            # Loop step not registered — register it first
            self._registry.register(loop.id)
            self._registry.update_status(loop.id, StepStatus.RUNNING)

        # Execute the loop
        result = await loop_executor.execute_loop(loop, run_id=run_id)

        # Update the registry with the loop's final status
        try:
            self._registry.update_status(loop.id, result.status)
        except KeyError:
            pass

        return result

    # ------------------------------------------------------------------
    # F5 执行增强：暂停 / 强制下一步 / 重试重复
    # ------------------------------------------------------------------

    @property
    def is_paused(self) -> bool:
        """是否处于暂停状态。"""
        return self._paused

    def pause(self) -> None:
        """暂停调度：_dispatch_step 将阻塞直到 resume()。

        幂等：重复调用安全。已派发的步骤不受影响（只拦新派发）。
        """
        self._paused = True
        self._pause_event.clear()

    def resume(self) -> None:
        """恢复调度：放行被 pause() 阻塞的派发。

        幂等：重复调用安全。
        """
        self._paused = False
        self._pause_event.set()

    def force_next(self) -> None:
        """强制下一步：下一次派发绕过 skip_if（一次性）。

        用于人工介入后强制执行本会被跳过的步骤。
        """
        self._force_next_pending = True

    async def handle_step_result(self, step_id: str, status: StepStatus) -> bool:
        """根据步骤结果决策是否重试/重复（F5，§6.4 决策矩阵）。

        决策矩阵：
        - ERROR（含超时 —— 执行器把 TimeoutError 映射为 ERROR）
          -> 重试：retry_count < max_retries 时递增计数、置回 PENDING，
             按 retry_delay_ms 延迟后返回 True；否则重试耗尽进入终态处置。
        - FAILED  -> 重复：repeat_on_measurement_fail 时，force_repeat 无视
                     repeat_limit 强制重复；否则 repeat_count < repeat_limit
                     才重复；无策略或重复耗尽进入终态处置。
        - PASSED  -> 清零 retry/repeat 计数，返回 False。

        终态处置（StepExecutionConfig.on_failure）：
        - 'abort'   -> 抛出 ExecutionAborted（调用方不得吞掉）；
        - 'skip'    -> 步骤标记 SKIPPED 后继续（依赖方按满足前置处理）；
        - 'continue'-> 保持既有终态返回 False（默认，向后兼容）。

        Args:
            step_id: 步骤标识。
            status: 步骤结束状态。

        Returns:
            True 表示调度器应重试/重复该步骤（已置回 PENDING）。

        Raises:
            ExecutionAborted: 终态失败且 on_failure='abort'。
        """
        try:
            config = self._registry.get_config(step_id)
        except KeyError:
            return False

        if status == StepStatus.ERROR:
            retries = self._registry.get_retry_count(step_id)
            if config.max_retries > 0 and retries < config.max_retries:
                self._registry.increment_retry_count(step_id)
                self._registry.update_status(step_id, StepStatus.PENDING)
                if config.retry_delay_ms > 0:
                    await asyncio.sleep(config.retry_delay_ms / 1000.0)
                return True
            # 重试耗尽：终态处置（abort→抛 / skip→SKIPPED / continue→保持 ERROR）
            self._settle_terminal_failure(step_id, config, StepStatus.ERROR)
            return False

        if status == StepStatus.FAILED:
            if not config.repeat_on_measurement_fail:
                # 无重复策略：FAILED 即终态候选，同样走 on_failure 处置
                self._settle_terminal_failure(step_id, config, StepStatus.FAILED)
                return False
            repeats = self._registry.get_repeat_count(step_id)
            if config.force_repeat or config.repeat_limit == 0 or repeats < config.repeat_limit:
                self._registry.increment_repeat_count(step_id)
                self._registry.update_status(step_id, StepStatus.PENDING)
                return True
            # 重复耗尽：终态处置
            self._settle_terminal_failure(step_id, config, StepStatus.FAILED)
            return False

        if status == StepStatus.PASSED:
            # 通过后清零计数，避免影响下次循环执行
            self._registry.reset_retry_count(step_id)
            self._registry.reset_repeat_count(step_id)
            return False

        return False

    def _settle_terminal_failure(
        self,
        step_id: str,
        config: StepExecutionConfig,
        failed_status: StepStatus,
    ) -> None:
        """终态失败处置 —— 决策矩阵的 on_failure 分支（§6.4）。

        在重试/重复耗尽或无策略可用时调用：

        - 'abort'：先把状态落为失败终态再抛 ExecutionAborted（崩溃恢复
          快照语义与 continue 一致），异常向上传播、不得被吞掉；
        - 'skip'：把步骤标记为 SKIPPED（依赖方按满足前置继续）；
        - 'continue'（默认）：保持既有终态。ERROR 耗尽时步骤可能仍处于
          重试留下的 PENDING，需显式落回 ERROR；FAILED 分支调用方已置
          FAILED，update_status 对相同状态不重复发事件，幂等安全。

        Args:
            step_id: 终态失败的步骤标识。
            config: 该步骤的执行配置（提供 on_failure 策略）。
            failed_status: 失败终态（ERROR 或 FAILED）。

        Raises:
            ExecutionAborted: on_failure='abort' 时必然抛出。
        """
        if config.on_failure == "abort":
            self._registry.update_status(step_id, failed_status)
            raise ExecutionAborted(
                f"Step '{step_id}' settled {failed_status.value} "
                "with on_failure=abort"
            )
        if config.on_failure == "skip":
            self._registry.update_status(step_id, StepStatus.SKIPPED)
            return
        # 'continue'（默认）：保持既有终态语义
        self._registry.update_status(step_id, failed_status)

    def get_status(self) -> dict[str, Any]:
        """Get current scheduler status for monitoring.

        Returns:
            Dictionary with status information
        """
        return {
            "running": self._running,
            "scan_interval": self._scan_interval,
            "heartbeat": self._heartbeat,
            "notified_ready_count": len(self._notified_ready),
            "pending_dispatch_count": len(self._pending_dispatch),
            "dependency_index_size": len(self._dependency_index),
            "last_dispatch_time": self._last_dispatch_time,
            "watchdog_running": self._watchdog is not None and self._watchdog.is_running,
            "adaptive_skip_enabled": self._adaptive_skip_evaluator is not None,
            "adaptive_skip_count": len(self._adaptive_skip_conditions),
            "snapshot_enabled": self._snapshot is not None,
            "snapshot_resumable": bool(
                self._snapshot is not None and self._snapshot.can_resume()
            ),
            "resumed_from_snapshot": self._resumed_from_snapshot,
            "paused": self._paused,
            "force_next_pending": self._force_next_pending,
        }
