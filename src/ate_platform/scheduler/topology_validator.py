"""TopologyValidator — 工装拓扑与序列执行联动校验（设计文档 §6.7.2，F10）。

工装拓扑不仅用于展示，还参与调度执行前的路由校验与资源分配；
校验不通过则拒绝执行（error 阻断 / warning 提示）。

校验内容：
1. 序列中引用的仪器必须在拓扑中存在。
2. 序列步骤的资源需求与拓扑接线 / UUT 亲和一致。
3. 并行步骤的仪器互斥校验：同一仪器被多个并行步骤共享时必须经矩阵开关隔离。
4. 夹具控制步骤与拓扑夹具元件能力匹配（F9 联动）。

输入：
- topology: 共享 FixtureTopology 模型 或 dict（§8.3.2 结构）。
- plan: shared.dsl.YamlPlan 或 dict（steps 列表）。

输出：
- ValidationResult（复用共享模型），含 errors/warnings/summary。
"""

from __future__ import annotations

from typing import Any, cast

import structlog

from shared.dsl import YamlLoop, YamlPlan, YamlStep
from shared.fixture_topology import (
    FixtureTopology,
    LinkEndpointType,
    ValidationIssue,
    ValidationResult,
)

logger = structlog.get_logger(__name__)


class TopologyValidator:
    """校验拓扑与序列的一致性（§6.7.2）。

    Attributes:
        topology: 工装拓扑（共享 FixtureTopology 模型）。
        plan: 执行计划（YamlPlan 或 dict）。
        strictness: 校验严格度（error 阻断 / warning 提示）。
    """

    #: 检查项代码
    CHECK_INSTRUMENT_EXISTS = "instrument_exists"
    CHECK_RESOURCE_WIRING = "resource_wiring"
    CHECK_PARALLEL_MUTEX = "parallel_mutex"
    CHECK_FIXTURE_CAPABILITY = "fixture_capability"

    def __init__(
        self,
        topology: FixtureTopology | dict[str, Any],
        plan: YamlPlan | dict[str, Any],
        strictness: str = "error",
    ) -> None:
        """初始化校验器。

        Args:
            topology: 工装拓扑（模型或 dict）。
            plan: 执行计划（YamlPlan 或 dict，含 steps）。
            strictness: 'error'（默认）冲突类检查为 error；'warning' 降级。
        """
        self.topology: FixtureTopology = (
            topology if isinstance(topology, FixtureTopology)
            else FixtureTopology.model_validate(topology)
        )
        self.plan: YamlPlan = (
            plan if isinstance(plan, YamlPlan) else self._plan_from_dict(plan)
        )
        self._strictness: str = strictness

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def validate(self) -> ValidationResult:
        """执行全部联动校验。

        Returns:
            ValidationResult 包含 errors/warnings。
        """
        result = ValidationResult()
        self._check_instruments_exist(result)
        self._check_resource_wiring(result)
        self._check_parallel_mutex(result)
        self._check_fixture_capability(result)
        logger.info(
            "topology_validation_done",
            plan=self.plan.name,
            errors=len(result.errors),
            warnings=len(result.warnings),
        )
        return result

    # ------------------------------------------------------------------
    # 检查项
    # ------------------------------------------------------------------

    def _check_instruments_exist(self, result: ValidationResult) -> None:
        """检查 1：序列中引用的仪器必须在拓扑中存在。"""
        topo_ids = {i.id for i in self.topology.instruments}
        for step in self._iter_steps():
            for inst_id in self._step_instruments(step):
                if inst_id not in topo_ids:
                    self._add(
                        result,
                        self.CHECK_INSTRUMENT_EXISTS,
                        f"Step {step.id} 引用的仪器 {inst_id} 不在拓扑中",
                        f"step:{step.id}.instrument:{inst_id}",
                    )

    def _check_resource_wiring(self, result: ValidationResult) -> None:
        """检查 2：步骤资源需求与拓扑接线 / UUT 亲和一致。"""
        dut_ids = {d.id for d in self.topology.duts}
        # 已接线的 DUT 测试点（拓扑链路终点）
        wired_duts: set[str] = {
            l.to.entity_id
            for l in self.topology.links
            if l.to.entity_type == LinkEndpointType.DUT_TESTPOINT
        }
        for step in self._iter_steps():
            uut_affinity = self._step_uut(step)
            if uut_affinity is None:
                continue
            if uut_affinity not in dut_ids:
                self._add(
                    result,
                    self.CHECK_RESOURCE_WIRING,
                    f"Step {step.id} 的 UUT 亲和 {uut_affinity} 不在拓扑 DUT 中",
                    f"step:{step.id}.uut:{uut_affinity}",
                )
                continue
            # 步骤使用的仪器必须能接通该 UUT
            for inst_id in self._step_instruments(step):
                if not self._instrument_reaches_dut(inst_id, uut_affinity):
                    self._add(
                        result,
                        self.CHECK_RESOURCE_WIRING,
                        f"Step {step.id} 仪器 {inst_id} 无法接通 UUT {uut_affinity}",
                        f"step:{step.id}.instrument:{inst_id}.uut:{uut_affinity}",
                        level="warning",
                    )

    def _check_parallel_mutex(self, result: ValidationResult) -> None:
        """检查 3：并行步骤的仪器互斥校验。

        并行组内步骤共享同一仪器时，仪器必须经矩阵开关（routeId 链路）
        隔离；否则报 error（§6.7.2 检查 3）。
        """
        groups = self._parallel_groups()
        for group in groups:
            shared = self._shared_resources(group)
            for inst_id in shared:
                if not self._has_mutex_switch(inst_id):
                    step_ids = [s.id for s in group if inst_id in self._step_instruments(s)]
                    self._add(
                        result,
                        self.CHECK_PARALLEL_MUTEX,
                        f"并行步骤 {', '.join(step_ids)} 共享仪器 {inst_id} "
                        f"但无矩阵开关隔离",
                        f"parallel.group:{inst_id}",
                    )

    def _check_fixture_capability(self, result: ValidationResult) -> None:
        """检查 4：夹具控制步骤与拓扑夹具元件能力匹配（§6.7.2 检查 4）。"""
        fixture_caps: dict[str, set[str]] = {}
        for fixture in self.topology.fixtures:
            # ActuatorType/RelayType 均为 str 子类枚举，统一收窄为 set[str]
            caps: set[str] = {a.type for a in fixture.actuators}
            caps |= {r.type for r in fixture.relays}
            fixture_caps[fixture.id] = caps

        for step in self._iter_steps():
            fixture_id = self._step_fixture(step)
            if fixture_id is None:
                continue
            action = self._step_fixture_action(step)
            if fixture_id not in fixture_caps:
                self._add(
                    result,
                    self.CHECK_FIXTURE_CAPABILITY,
                    f"Step {step.id} 引用的夹具 {fixture_id} 不在拓扑中",
                    f"step:{step.id}.fixture:{fixture_id}",
                )
                continue
            if action and not self._fixture_supports(fixture_caps[fixture_id], action):
                self._add(
                    result,
                    self.CHECK_FIXTURE_CAPABILITY,
                    f"夹具 {fixture_id} 不支持动作 {action}（Step {step.id}）",
                    f"step:{step.id}.fixture:{fixture_id}.action:{action}",
                )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _add(
        self,
        result: ValidationResult,
        code: str,
        message: str,
        path: str,
        level: str | None = None,
    ) -> None:
        """按严格度添加问题。"""
        effective_level = level or self._strictness
        issue = ValidationIssue(
            level=effective_level,  # type: ignore[arg-type]
            code=code, message=message, path=path,
        )
        if effective_level == "error":
            result.errors.append(issue)
        else:
            result.warnings.append(issue)

    def _iter_steps(self) -> list[YamlStep]:
        """递归收集所有步骤（含循环内嵌套）。"""
        steps: list[YamlStep] = []

        def walk(items: list[Any]) -> None:
            for item in items:
                if isinstance(item, YamlStep):
                    steps.append(item)
                elif isinstance(item, YamlLoop):
                    walk(item.steps)

        walk(self.plan.steps)
        return steps

    def _step_instruments(self, step: YamlStep) -> list[str]:
        """从步骤 resources 提取仪器 id。"""
        ids: list[str] = []
        resources = step.resources or {}
        if not isinstance(resources, dict):
            return ids
        for key, value in resources.items():
            if key in ("instrument", "instrument_id"):
                if isinstance(value, str):
                    ids.append(value)
            elif key in ("instruments",):
                if isinstance(value, list):
                    ids.extend(i for i in value if isinstance(i, str))
                elif isinstance(value, dict):
                    ids.extend(v for v in value.values() if isinstance(v, str))
        return ids

    def _step_uut(self, step: YamlStep) -> str | None:
        """取步骤 UUT 亲和（resources.uut_affinity / params.uut）。"""
        resources = step.resources or {}
        if isinstance(resources, dict):
            uut = resources.get("uut_affinity") or resources.get("uut")
            if isinstance(uut, str):
                return uut
        params = step.params or {}
        uut = params.get("uut")
        return uut if isinstance(uut, str) else None

    def _step_fixture(self, step: YamlStep) -> str | None:
        """取步骤夹具引用（params.fixture_id / resources.fixture）。"""
        params = step.params or {}
        fixture = params.get("fixture_id")
        if isinstance(fixture, str):
            return fixture
        resources = step.resources or {}
        if isinstance(resources, dict):
            f = resources.get("fixture")
            if isinstance(f, str):
                return f
        return None

    def _step_fixture_action(self, step: YamlStep) -> str | None:
        """取夹具控制动作（params.action / params.operation）。"""
        params = step.params or {}
        action = params.get("action") or params.get("operation")
        return action if isinstance(action, str) else None

    def _parallel_groups(self) -> list[list[YamlStep]]:
        """推导并行步骤组。

        顶层步骤中，若计划 max_concurrency > 1（或存在并行 LOOP），
        将彼此无依赖关系（preconditions 不互指）的步骤归入同一并行组。
        """
        top_steps = [s for s in self.plan.steps if isinstance(s, YamlStep)]
        if len(top_steps) < 2:
            return []

        parallel_enabled = self.plan.max_concurrency > 1 or any(
            isinstance(item, YamlLoop) and item.execution_mode.value == "PARALLEL"
            for item in self.plan.steps
        )
        if not parallel_enabled:
            return []

        groups: list[list[YamlStep]] = []
        remaining = list(top_steps)
        while remaining:
            group = [remaining.pop(0)]
            group_ids = {group[0].id}
            for other in list(remaining):
                if not self._depends_on(other, group_ids):
                    group.append(other)
                    group_ids.add(other.id)
                    remaining.remove(other)
            groups.append(group)
        # 只有大于 1 步的组才算并行组
        return [g for g in groups if len(g) > 1]

    @staticmethod
    def _depends_on(step: YamlStep, ids: set[str]) -> bool:
        """步骤是否依赖给定 id 集合中的步骤。"""
        return bool(set(step.preconditions or []) & ids)

    def _shared_resources(self, group: list[YamlStep]) -> set[str]:
        """并行组内被多个步骤共享的仪器集合。"""
        per_step = [set(self._step_instruments(s)) for s in group]
        shared: set[str] = set()
        for i, insts in enumerate(per_step):
            for other in per_step[i + 1:]:
                shared |= insts & other
        return shared

    def _has_mutex_switch(self, resource_id: str) -> bool:
        """检查仪器到多目标之间是否有矩阵开关/继电器隔离。

        拓扑中存在关联 routeId 的链路（经过矩阵开关）即视为有隔离。
        """
        for link in self.topology.links:
            if link.routeId:
                return True
            if link.from_endpoint.entity_id == resource_id:
                # 起点为该仪器且终点是夹具继电器触点 -> 有开关
                if link.to.entity_type == LinkEndpointType.RELAY_CONTACT:
                    return True
        return False

    def _instrument_reaches_dut(self, inst_id: str, dut_id: str) -> bool:
        """检查仪器是否能经链路接通 DUT（直接或经夹具端子）。"""
        reachable_entities: set[str] = {inst_id}
        changed = True
        while changed:
            changed = False
            for link in self.topology.links:
                src, dst = link.from_endpoint, link.to
                if src.entity_id in reachable_entities and dst.entity_id not in reachable_entities:
                    reachable_entities.add(dst.entity_id)
                    changed = True
                elif dst.entity_id in reachable_entities and src.entity_id not in reachable_entities:
                    reachable_entities.add(src.entity_id)
                    changed = True
        return dut_id in reachable_entities

    @staticmethod
    def _fixture_supports(caps: set[str], action: str) -> bool:
        """夹具是否支持某动作（按能力集合模糊匹配）。"""
        normalized = action.lower()
        for cap in caps:
            if cap in normalized or normalized in cap:
                return True
        return False

    @staticmethod
    def _plan_from_dict(plan: dict[str, Any]) -> YamlPlan:
        """将 dict 形式计划转换为 YamlPlan（递归转换 steps）。"""
        from shared.dsl import ExecutionMode, LoopType

        def conv_step(data: dict[str, Any]) -> YamlStep:
            return YamlStep(
                id=data.get("id", "?"),
                script=data.get("script", ""),
                params=data.get("params", {}),
                preconditions=list(data.get("preconditions", [])),
                resources=data.get("resources", {}),
                timeout=data.get("timeout", 60),
                retry=data.get("retry", 0),
                on_fail=data.get("on_fail"),
                skip_if=data.get("skip_if"),
                skip_reason=data.get("skip_reason"),
            )

        def conv_loop(data: dict[str, Any]) -> YamlLoop:
            loop_type = LoopType(data.get("loop_type", "FOR"))
            return YamlLoop(
                id=data.get("id", "?"),
                loop_type=loop_type,
                steps=[conv_item(s) for s in data.get("steps", [])],
                count=data.get("count"),
                condition=data.get("condition"),
                collection=data.get("collection"),
                iterator_var=data.get("iterator_var"),
                execution_mode=ExecutionMode(data.get("execution_mode", "SERIAL")),
                max_iterations=data.get("max_iterations", 1000),
            )

        def conv_item(item: Any) -> YamlStep | YamlLoop:
            if isinstance(item, dict):
                if "steps" in item:
                    return conv_loop(item)
                return conv_step(item)
            # 非 dict 项直接透传（调用方保证为 YamlStep/YamlLoop）
            return cast(YamlStep | YamlLoop, item)

        return YamlPlan(
            name=plan.get("name", "?"),
            version=plan.get("version", "1.0"),
            scope=plan.get("scope", {}),
            max_concurrency=plan.get("max_concurrency", 1),
            steps=[conv_item(s) for s in plan.get("steps", [])],
        )


# re-export 便于调用方统一导入
__all__ = ["TopologyValidator"]
