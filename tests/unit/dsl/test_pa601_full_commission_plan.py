"""PA601-D54A 全量 commissioning 计划（23-TC）解析与结构校验。

校验 ``data/commission/pa601_full_commission.yaml``：
- 可被 ``ate_platform.dsl.parser.YamlParser`` 解析（优先 ``parse()``）；
- 恰好 23 个 step，覆盖 TC-01..TC-23；
- 所有 step id 唯一；
- 依赖顺序合法：每个 ``depends_on`` 指向已声明的**更早** step（无前向引用、
  无环），即拓扑序与声明序一致；
- 每个 step 是脚本类 action（type=ACTION）且含 script；
- ``validate()`` 返回空错误列表。

注意：``YamlParser.parse()`` **不校验 script 文件是否存在**，故本模板中大量
TBD（待编写）脚本名不影响解析；真机执行到 TBD 步骤由执行器 fail-fast 中止。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ate_platform.dsl.parser import YamlParser
from shared.dsl import StepType, YamlPlan, YamlStep

# tests/unit/dsl/<this>.py → parents[3] = 仓库根
REPO_ROOT = Path(__file__).resolve().parents[3]
PLAN_PATH = REPO_ROOT / "data" / "commission" / "pa601_full_commission.yaml"

#: 期望覆盖的 23 个 TC 编号。
EXPECTED_TC_COUNT = 23


@pytest.fixture(scope="module")
def plan() -> YamlPlan:
    """解析全量 commissioning 计划（不存在的文件应 fail-fast）。"""
    assert PLAN_PATH.exists(), f"计划文件缺失: {PLAN_PATH}"
    return YamlParser().parse(PLAN_PATH)


class TestPa601FullCommissionPlan:
    """23-TC 全量 commissioning 计划的结构断言。"""

    def test_plan_parses_with_expected_metadata(self, plan: YamlPlan) -> None:
        """计划可解析且顶层字段齐备。"""
        assert plan.name
        assert plan.version
        assert plan.scope
        assert plan.max_concurrency == 1
        assert plan.uut_count == 1

    def test_has_23_steps(self, plan: YamlPlan) -> None:
        """恰好 23 个 step（TC-01..TC-23）。"""
        assert len(plan.steps) == EXPECTED_TC_COUNT

    def test_step_ids_unique_and_cover_all_tcs(self, plan: YamlPlan) -> None:
        """step id 唯一，且按序覆盖 tc01..tc23。"""
        ids = [s.id for s in plan.steps]
        assert len(ids) == len(set(ids)), f"存在重复 id: {ids}"
        expected_prefixes = [f"tc{tc:02d}_" for tc in range(1, EXPECTED_TC_COUNT + 1)]
        for step_id, prefix in zip(ids, expected_prefixes, strict=True):
            assert step_id.startswith(prefix), f"id 顺序/命名不符: {step_id} != {prefix}*"

    def test_dependency_order_is_legal(self, plan: YamlPlan) -> None:
        """依赖顺序合法：depends_on 只引用更早声明的 step（无前向引用、无环）。"""
        index_by_id = {s.id: i for i, s in enumerate(plan.steps)}
        for i, step in enumerate(plan.steps):
            for dep in step.depends_on:
                assert dep in index_by_id, f"'{step.id}' 依赖未声明的步骤 '{dep}'"
                assert index_by_id[dep] < i, (
                    f"'{step.id}'(#{i}) 依赖了更晚声明的 '{dep}'(#{index_by_id[dep]})："
                    "依赖顺序非法/成环"
                )

    def test_steps_are_action_scripts(self, plan: YamlPlan) -> None:
        """每个 step 均为脚本类 action 且含 script。"""
        for step in plan.steps:
            assert isinstance(step, YamlStep)
            assert step.type == StepType.ACTION, f"'{step.id}' type 非 action"
            assert step.script and step.script.strip(), f"'{step.id}' 缺 script"

    def test_each_step_declares_condition_params(self, plan: YamlPlan) -> None:
        """每个 step 的 params 声明 condition_kind / condition_text / value 限值。"""
        for step in plan.steps:
            params = step.params
            assert params.get("condition_kind"), f"'{step.id}' 缺 condition_kind"
            assert params.get("condition_text"), f"'{step.id}' 缺 condition_text"
            assert "value" in params, f"'{step.id}' 缺 value 限值"

    def test_validate_returns_no_errors(self, plan: YamlPlan) -> None:
        """``validate()`` 对本计划返回空错误列表。"""
        assert YamlParser().validate(plan) == []
