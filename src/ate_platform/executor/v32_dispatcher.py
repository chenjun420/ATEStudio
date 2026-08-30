"""V32PlanDispatcher — DSL v3.2 步骤语义执行器（设计文档 §6.5.4 / §7.11）。

把解析后的 :class:`YamlPlan` 按依赖序执行，并按 ``step.type`` 分发到
运行时组件（此前 parser 可解析 v3.2 步骤但无引擎真正消费）：

- ``FIXTURE_CONTROL`` → :class:`FixtureController`（clamp/release/set_route/read_sensor）
- ``BARRIER`` → :class:`UUTManager` 同步屏障（每 UUT 到达，全部到齐放行）
- ``ACTION`` / ``SCRIPT`` → 脚本步骤（模拟模式下仅做决策记录）
- ``LOOP`` → 递归执行子步骤 ``count`` 次（子步骤计入 JUnit testcase）

``retry`` 与 ``on_failure``（abort/continue/skip）在决策逻辑中生效：
失败按 ``1 + retry`` 次尝试；on_failure=abort 中止整个运行，
continue 记录失败后继续，skip 将该步标记 SKIP。

模拟模式（默认，无硬件）：FixtureController 以 ``proxy_client=None`` 构造
（动作只记录状态、传感器返回默认值），脚本步骤不真正执行——与 headless
仿真（§7.11 / AC-12）协同，供 CI 无硬件端到端验证 v3.2 语义。

用法：
    plan = YamlParser().parse(Path("plan.yaml"))
    outcomes = await V32PlanDispatcher(plan).run()
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ate_platform.fixture import FixtureController, FixtureError, FixtureTimeoutError
from ate_platform.scheduler.uut_sync import BarrierResult, UUTManager
from shared.dsl import StepType, YamlLoop, YamlPlan, YamlStep

logger = logging.getLogger(__name__)

#: fixture_control 步骤支持的合法动作
_FIXTURE_ACTIONS = {"clamp", "release", "set_route", "read_sensor"}


@dataclass
class StepOutcome:
    """单步执行结果（供 JUnit 报告 / 控制台摘要消费）。

    Attributes:
        step_id: 步骤标识（循环子步骤形如 ``loop_id.iterN.child_id``）。
        step_type: 实际分发的步骤类型（script/action/barrier/fixture_control/loop）。
        status: PASS / FAIL / SKIP / BLOCKED。
        detail: 人可读的补充说明（动作名、屏障缺员等）。
        attempts: 实际尝试次数（含 retry）。
    """

    step_id: str
    step_type: str
    status: str
    detail: str = ""
    attempts: int = 1


#: 脚本步骤执行器：输入步骤，返回是否成功。模拟模式默认恒成功，
#: 测试可注入失败执行器以验证 retry/on_failure。
ScriptExecutor = Callable[[YamlStep], Awaitable[bool]]


class V32PlanDispatcher:
    """DSL v3.2 计划执行器。

    Attributes:
        plan: 待执行的 :class:`YamlPlan`。
        simulation: 为 True 时使用模拟模式（无硬件，默认）。
        uut_timeout: 同步屏障超时（秒）。
        script_executor: 可注入的脚本步骤执行器（见 :data:`ScriptExecutor`）。
        fixtures: fixture_id → :class:`FixtureController` 注册表。
        uut_manager: 多 UUT 池与同步屏障管理器。
    """

    def __init__(
        self,
        plan: YamlPlan,
        *,
        simulation: bool = True,
        uut_timeout: float = 60.0,
        script_executor: ScriptExecutor | None = None,
        missing_uuts: set[str] | None = None,
    ) -> None:
        self._plan = plan
        self._simulation = simulation
        self._uut_timeout = uut_timeout
        self._script_executor = script_executor
        self._missing_uuts = missing_uuts or set()
        self.fixtures: dict[str, FixtureController] = {}
        self.uut_manager = UUTManager(count=plan.uut_count)
        self._aborted = False

    async def run(self) -> list[StepOutcome]:
        """按依赖序执行全部顶层步骤（含循环展开）。

        Returns:
            全部步骤的 :class:`StepOutcome` 列表（含循环子步骤，
            顺序即执行顺序）。
        """
        self._aborted = False
        outcomes: list[StepOutcome] = []
        await self._run_items(list(self._plan.steps), outcomes)
        return outcomes

    # ------------------------------------------------------------------
    # 顶层 / 容器执行
    # ------------------------------------------------------------------
    async def _run_items(
        self,
        items: list[YamlStep | YamlLoop],
        outcomes: list[StepOutcome],
    ) -> None:
        """按 ``depends_on`` 依赖序执行一组项（顶层步骤或循环子步骤）。

        每次取出所有依赖已满足的项执行；若无进展则剩余项标记 BLOCKED
        （依赖缺失/成环，§6.3.7 死锁防护）。
        """
        remaining: list[YamlStep | YamlLoop] = list(items)
        completed: set[str] = set()

        while remaining:
            ready = [
                item
                for item in remaining
                if all(dep in completed for dep in self._deps(item))
            ]
            if not ready:
                for item in remaining:
                    outcomes.append(
                        StepOutcome(
                            step_id=self._item_id(item),
                            step_type=self._item_type(item),
                            status="BLOCKED",
                            detail=f"Unmet dependencies: {self._deps(item)}",
                        )
                    )
                    if self._is_abort(item):
                        self._aborted = True
                break

            for item in ready:
                if self._aborted:
                    # on_failure=abort 已触发：后续步骤不再执行
                    remaining.remove(item)
                    continue
                await self._execute_item(item, outcomes)
                completed.add(self._item_id(item))
                remaining.remove(item)

    def _execute_item(
        self,
        item: YamlStep | YamlLoop,
        outcomes: list[StepOutcome],
    ) -> Awaitable[None]:
        if isinstance(item, YamlLoop):
            return self._execute_loop(item, outcomes)
        return self._execute_step(item, outcomes)

    async def _execute_loop(
        self,
        loop: YamlLoop,
        outcomes: list[StepOutcome],
    ) -> None:
        """执行循环容器：子步骤按 ``count`` 次迭代展开。

        子步骤的 ``step_id`` 以 ``loop_id.iterN.child_id`` 标识，
        每次迭代作为独立 JUnit testcase。
        """
        iterations = loop.count if loop.count is not None else 1
        for iteration in range(iterations):
            if self._aborted:
                break
            iter_outcomes: list[StepOutcome] = []
            await self._run_items(list(loop.steps), iter_outcomes)
            for oc in iter_outcomes:
                oc.step_id = f"{loop.id}.iter{iteration}.{oc.step_id}"
            outcomes.extend(iter_outcomes)

    async def _execute_step(
        self,
        step: YamlStep,
        outcomes: list[StepOutcome],
    ) -> None:
        """执行单个步骤：按 effective step type 分发。"""
        step_type = step.type if step.type is not None else StepType.SCRIPT
        if step_type is StepType.FIXTURE_CONTROL:
            outcome = await self._dispatch_fixture(step)
        elif step_type is StepType.BARRIER:
            outcome = await self._dispatch_barrier(step)
        elif step_type is StepType.LOOP:
            outcome = StepOutcome(
                step_id=step.id,
                step_type="loop",
                status="PASS",
                detail="Loop container (children dispatched separately)",
            )
        elif step_type in (StepType.ACTION, StepType.SCRIPT):
            outcome = await self._dispatch_script(step)
        else:
            outcome = StepOutcome(
                step_id=step.id,
                step_type=step_type.value,
                status="SKIP",
                detail=f"Unsupported step type '{step_type.value}' in this dispatcher",
            )
        outcomes.append(outcome)

    # ------------------------------------------------------------------
    # 分派器
    # ------------------------------------------------------------------
    async def _dispatch_fixture(self, step: YamlStep) -> StepOutcome:
        """分派 fixture_control 步骤到 FixtureController。"""
        fixture_id = step.fixture_id or self._plan.fixture_id
        if not fixture_id:
            return StepOutcome(
                step_id=step.id,
                step_type="fixture_control",
                status="FAIL",
                detail="fixture_control step requires 'fixture_id'",
            )
        action = step.action or ""
        if action not in _FIXTURE_ACTIONS:
            return StepOutcome(
                step_id=step.id,
                step_type="fixture_control",
                status="FAIL",
                detail=f"Unknown fixture action '{action}' "
                f"(valid: {sorted(_FIXTURE_ACTIONS)})",
            )

        controller = self._get_fixture(fixture_id)
        try:
            if action == "clamp":
                await controller.clamp()
            elif action == "release":
                await controller.release()
            elif action == "set_route":
                relay_id = str(step.params.get("relay_id", ""))
                route = str(step.params.get("route", "DUT1"))
                if not relay_id:
                    return StepOutcome(
                        step_id=step.id,
                        step_type="fixture_control",
                        status="FAIL",
                        detail="set_route requires params.relay_id",
                    )
                await controller.set_route(relay_id, route)
            elif action == "read_sensor":
                sensor_id = str(step.params.get("sensor_id", ""))
                if not sensor_id:
                    return StepOutcome(
                        step_id=step.id,
                        step_type="fixture_control",
                        status="FAIL",
                        detail="read_sensor requires params.sensor_id",
                    )
                value = await controller.read_sensor(sensor_id)
                return StepOutcome(
                    step_id=step.id,
                    step_type="fixture_control",
                    status="PASS",
                    detail=f"read_sensor {sensor_id} = {value}",
                )
            return StepOutcome(
                step_id=step.id,
                step_type="fixture_control",
                status="PASS",
                detail=f"{action} on fixture '{fixture_id}'",
            )
        except FixtureTimeoutError as e:
            return StepOutcome(
                step_id=step.id,
                step_type="fixture_control",
                status="FAIL",
                detail=f"Fixture action '{action}' timed out: {e}",
            )
        except FixtureError as e:
            return StepOutcome(
                step_id=step.id,
                step_type="fixture_control",
                status="FAIL",
                detail=f"Fixture action '{action}' failed: {e}",
            )

    async def _dispatch_barrier(self, step: YamlStep) -> StepOutcome:
        """分派 barrier 步骤到 UUTManager 同步屏障。

        ``UUTManager.wait_barrier`` 是阻塞式（``threading.Condition``），
        因此每个 UUT 的到达通过 :func:`asyncio.to_thread` 并发执行——
        所有 UUT 同时到达后屏障放行（§6.3.7）。并发等待全部结束后取首个
        超时结果；超时缺员则标记 FAIL（UUTManager 会把缺员 UUT 置为
        failed，死锁防护）。

        ``missing_uuts``（构造参数）用于仿真缺员场景：指定 UUT 不参与
        到达，其余 UUT 等待直至超时（模拟真实多 UUT 中某 UUT 提前失败
        未到达屏障）。
        """
        barrier_name = step.barrier_name or step.id
        timeout = self._uut_timeout

        async def _arrive(uut_id: str) -> BarrierResult:
            return await asyncio.to_thread(
                self.uut_manager.wait_barrier,
                barrier_name,
                uut_id,
                timeout,
            )

        arrivers = [
            u for u in self.uut_manager.uut_ids if u not in self._missing_uuts
        ]
        if not arrivers:
            arrivers = list(self.uut_manager.uut_ids)
        barrier_results: list[BarrierResult] = list(
            await asyncio.gather(*(_arrive(u) for u in arrivers))
        )

        timed_out = [r for r in barrier_results if r.timed_out]
        if timed_out:
            missing: set[str] = set()
            for r in timed_out:
                missing |= set(r.missing)
            return StepOutcome(
                step_id=step.id,
                step_type="barrier",
                status="FAIL",
                detail=(
                    f"Barrier '{barrier_name}' timed out; "
                    f"missing UUTs: {sorted(missing)}"
                ),
            )
        return StepOutcome(
            step_id=step.id,
            step_type="barrier",
            status="PASS",
            detail=f"Barrier '{barrier_name}' reached by all UUTs",
        )

    async def _dispatch_script(self, step: YamlStep) -> StepOutcome:
        """分派 action/script 步骤，honor retry 与 on_failure。

        模拟模式下脚本步骤默认成功；通过注入 :attr:`_script_executor`
        可模拟失败以验证重试与失败策略。
        """
        attempts = 0
        max_attempts = 1 + (step.retry if step.retry else 0)
        last_failed = False

        for _attempt in range(max_attempts):
            attempts += 1
            if self._script_executor is not None:
                try:
                    ok = await self._script_executor(step)
                except Exception as e:  # noqa: BLE001 — 执行器失败视为步骤失败
                    logger.warning("script_executor raised: %s", e)
                    ok = False
            else:
                ok = True
            if ok:
                return StepOutcome(
                    step_id=step.id,
                    step_type="script",
                    status="PASS",
                    detail=f"script '{step.script}' (attempt {attempts})",
                    attempts=attempts,
                )
            last_failed = True

        return self._failure_outcome(step, attempts, last_failed)

    def _failure_outcome(
        self,
        step: YamlStep,
        attempts: int,
        last_failed: bool,
    ) -> StepOutcome:
        """根据 on_failure 策略生成失败步骤的结果并设置中止标志。"""
        on_failure = step.on_failure or step.on_fail
        detail = f"script '{step.script}' failed after {attempts} attempt(s)"

        if on_failure == "continue":
            return StepOutcome(
                step_id=step.id,
                step_type="script",
                status="FAIL",
                detail=f"{detail}; on_failure=continue",
                attempts=attempts,
            )
        if on_failure == "skip":
            return StepOutcome(
                step_id=step.id,
                step_type="script",
                status="SKIP",
                detail=f"{detail}; on_failure=skip",
                attempts=attempts,
            )
        # abort（默认）：标记中止，后续步骤不再执行
        self._aborted = True
        return StepOutcome(
            step_id=step.id,
            step_type="script",
            status="FAIL",
            detail=f"{detail}; on_failure=abort (default)",
            attempts=attempts,
        )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _get_fixture(self, fixture_id: str) -> FixtureController:
        """按 fixture_id 获取（惰性创建）FixtureController。"""
        controller = self.fixtures.get(fixture_id)
        if controller is None:
            controller = FixtureController(
                fixture_id=fixture_id,
                proxy_client=None if self._simulation else None,
            )
            self.fixtures[fixture_id] = controller
        return controller

    @staticmethod
    def _deps(item: YamlStep | YamlLoop) -> list[str]:
        """项的依赖集合（depends_on，兼容旧 preconditions）。"""
        deps = list(item.depends_on or [])
        if isinstance(item, YamlStep) and item.preconditions:
            deps = deps + [d for d in item.preconditions if d not in deps]
        return deps

    @staticmethod
    def _item_id(item: YamlStep | YamlLoop) -> str:
        return item.id

    @staticmethod
    def _item_type(item: YamlStep | YamlLoop) -> str:
        if isinstance(item, YamlLoop):
            return "loop"
        return (item.type.value if item.type is not None else "script")

    @staticmethod
    def _is_abort(item: YamlStep | YamlLoop) -> bool:
        """该失败项的 on_failure 是否为 abort（用于 BLOCKED 时的中止传播）。"""
        if isinstance(item, YamlStep):
            return (item.on_failure or item.on_fail) in (None, "abort")
        return False


__all__ = ["StepOutcome", "V32PlanDispatcher"]
