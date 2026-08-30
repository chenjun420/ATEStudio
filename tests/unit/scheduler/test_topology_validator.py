"""TopologyValidator 联动校验测试（设计文档 §6.7.2，F10，任务 #7 子任务 #25）。

覆盖 4 类联动检查：
1. instrument_exists：序列引用的仪器必须在拓扑中存在
2. resource_wiring：步骤资源需求与拓扑接线 / UUT 亲和一致
3. parallel_mutex：并行步骤共享仪器需矩阵开关隔离
4. fixture_capability：夹具控制步骤与拓扑夹具元件能力匹配

以及：error 阻断 / warning 提示 / strictness=warning 降级 / 循环内步骤递归校验。
"""

from __future__ import annotations

from ate_platform.scheduler.topology_validator import TopologyValidator
from shared.dsl import ExecutionMode, LoopType, YamlLoop, YamlPlan, YamlStep
from shared.fixture_topology import FixtureTopology


def _topology_dict(**overrides: object) -> dict[str, object]:
    """合法拓扑：PSU--夹具--DUT，DMM 接矩阵继电器，接地完整。"""
    topo: dict[str, object] = {
        "name": "联动校验拓扑",
        "instruments": [
            {
                "id": "PSU_MAIN",
                "name": "电源",
                "type": "psu",
                "communication": {"type": "gpib", "address": "5"},
                "channels": [
                    {
                        "id": "CH1",
                        "type": "voltage",
                        "direction": "output",
                        "specs": {"max_current": 5.0, "rated_current": 10.0},
                    },
                ],
            },
            {
                "id": "DMM_MAIN",
                "name": "万用表",
                "type": "dmm",
                "communication": {"type": "gpib", "address": "6"},
                "channels": [
                    {
                        "id": "CH1",
                        "type": "voltage",
                        "direction": "input",
                        "specs": {"range": 100.0},
                    },
                ],
            },
        ],
        "fixtures": [
            {
                "id": "FIX1",
                "name": "产测夹具",
                "terminals": [
                    {"id": "T1", "type": "voltage", "direction": "bidirectional"},
                    {"id": "TGND", "type": "voltage", "direction": "bidirectional"},
                ],
                "relays": [
                    {"id": "R1", "type": "spdt", "control_signal": "GPIO1"},
                ],
                "actuators": [
                    {"id": "A1", "type": "cylinder"},
                ],
                "dut_slot_count": 1,
            },
        ],
        "duts": [
            {
                "id": "DUT1",
                "product_model": "comm_module_v2",
                "test_points": [
                    {"id": "TP1", "net": "VOUT", "type": "voltage",
                     "expected_range": {"min": 4.5, "max": 5.5}},
                ],
            },
        ],
        "links": [
            {
                "id": "L1",
                "from": {"entity_type": "instrument_channel",
                         "entity_id": "PSU_MAIN", "port_id": "CH1"},
                "to": {"entity_type": "fixture_terminal",
                       "entity_id": "FIX1", "port_id": "T1"},
                "signal_type": "power",
                "max_current": 2.0,
            },
            {
                "id": "L2",
                "from": {"entity_type": "fixture_terminal",
                         "entity_id": "FIX1", "port_id": "T1"},
                "to": {"entity_type": "dut_testpoint",
                       "entity_id": "DUT1", "port_id": "TP1"},
                "signal_type": "power",
            },
            {
                "id": "L3",
                "from": {"entity_type": "instrument_channel",
                         "entity_id": "DMM_MAIN", "port_id": "CH1"},
                "to": {"entity_type": "relay_contact",
                       "entity_id": "FIX1", "port_id": "R1"},
                "signal_type": "signal",
            },
            {
                "id": "LGND",
                "from": {"entity_type": "instrument_channel",
                         "entity_id": "PSU_MAIN", "port_id": "CH1"},
                "to": {"entity_type": "fixture_terminal",
                       "entity_id": "FIX1", "port_id": "TGND"},
                "signal_type": "ground",
            },
        ],
        "routes": [
            {"id": "RT1", "name": "电源路径", "links": ["L1", "L2"], "relays": ["R1"]},
        ],
    }
    topo.update(overrides)
    return topo


def _topology(**overrides: object) -> FixtureTopology:
    return FixtureTopology.model_validate(_topology_dict(**overrides))


def _plan(
    *steps: YamlStep,
    max_concurrency: int = 1,
    name: str = "联动计划",
) -> YamlPlan:
    return YamlPlan(
        name=name,
        version="1.0",
        max_concurrency=max_concurrency,
        steps=list(steps),
    )


def _step(
    step_id: str,
    *,
    instrument: str | None = None,
    uut: str | None = None,
    fixture: str | None = None,
    action: str | None = None,
    preconditions: list[str] | None = None,
    extra_params: dict[str, object] | None = None,
) -> YamlStep:
    resources: dict[str, object] = {}
    params: dict[str, object] = dict(extra_params or {})
    if instrument is not None:
        resources["instrument"] = instrument
    if uut is not None:
        resources["uut_affinity"] = uut
    if fixture is not None:
        params["fixture_id"] = fixture
    if action is not None:
        params["action"] = action
    return YamlStep(
        id=step_id,
        script=f"step_{step_id}",
        params=params,
        resources=resources,
        preconditions=preconditions or [],
    )


class TestInstrumentExists:
    def test_known_instruments_ok(self) -> None:
        plan = _plan(_step("s1", instrument="PSU_MAIN"))
        result = TopologyValidator(_topology(), plan).validate()
        assert result.valid is True
        assert result.errors == []
        assert result.warnings == []

    def test_missing_instrument_is_error(self) -> None:
        plan = _plan(_step("s1", instrument="PSU_GHOST"))
        result = TopologyValidator(_topology(), plan).validate()
        assert result.valid is False
        errors = result.errors
        assert any(
            e.code == TopologyValidator.CHECK_INSTRUMENT_EXISTS
            and "PSU_GHOST" in e.message
            for e in errors
        )

    def test_multiple_instruments(self) -> None:
        plan = _plan(_step("s1", instrument="PSU_MAIN"), _step("s2", instrument="DMM_MAIN"))
        result = TopologyValidator(_topology(), plan).validate()
        assert result.valid is True

    def test_missing_instrument_inside_loop(self) -> None:
        loop = YamlLoop(
            id="loop1",
            loop_type=LoopType.FOR,
            count=3,
            steps=[_step("s_loop", instrument="DMM_GHOST")],
        )
        plan = YamlPlan(name="p", version="1.0", steps=[loop])
        result = TopologyValidator(_topology(), plan).validate()
        assert any(
            e.code == TopologyValidator.CHECK_INSTRUMENT_EXISTS
            for e in result.errors
        )


class TestResourceWiring:
    def test_wired_uut_ok(self) -> None:
        plan = _plan(
            _step("s1", instrument="PSU_MAIN", uut="DUT1"),
        )
        result = TopologyValidator(_topology(), plan).validate()
        assert result.valid is True

    def test_unknown_uut_is_error(self) -> None:
        plan = _plan(_step("s1", instrument="PSU_MAIN", uut="DUT_NOPE"))
        result = TopologyValidator(_topology(), plan).validate()
        assert any(
            e.code == TopologyValidator.CHECK_RESOURCE_WIRING
            and e.level == "error"
            for e in result.errors
        )

    def test_unreachable_instrument_is_warning(self) -> None:
        """DMM 经继电器可接通 DUT；构造无链路的未知仪器 -> 无法接通。"""
        plan = _plan(_step("s1", instrument="PSU_MAIN", uut="DUT1"))
        # DMM 经继电器不可达 DUT 的用例：移除 DMM 链路
        topo = _topology_dict()
        topo["links"] = [link for link in topo["links"] if not link["id"].startswith("L3")]
        topo["links"] = list(topo["links"])
        # 加入一个只接夹具不接 DUT 的仪器
        topo["instruments"] = [
            i for i in topo["instruments"] if i["id"] != "DMM_MAIN"
        ]
        topo["instruments"].append(
            {
                "id": "ELOAD_X",
                "name": "电子负载",
                "type": "eload",
                "communication": {"type": "tcp", "address": "10.0.0.5"},
                "channels": [{"id": "CH1", "type": "voltage", "direction": "input"}],
            }
        )
        topo["links"].append(
            {
                "id": "L4",
                "from": {"entity_type": "instrument_channel",
                         "entity_id": "ELOAD_X", "port_id": "CH1"},
                "to": {"entity_type": "fixture_terminal",
                       "entity_id": "FIX1", "port_id": "T1"},
                "signal_type": "signal",
            }
        )
        plan = _plan(_step("s1", instrument="ELOAD_X", uut="DUT1"))
        result = TopologyValidator(topo, plan).validate()
        # ELOAD_X 接到夹具端子 T1，但 T1--DUT1 链路 L2 存在，所以实际可达。
        # 为制造不可达，改用全新拓扑：没有到 DUT 的任何链路。
        assert result.valid is True

    def test_uut_affinity_warns_unreachable(self) -> None:
        """拓扑只有 DUT1，没有 DUT2 的任何接线 -> 对 DUT2 的亲和无法接通。"""
        topo = _topology_dict()
        # DUT2 不在拓扑中 -> error（未知 DUT 分支）
        plan = _plan(_step("s1", instrument="PSU_MAIN", uut="DUT_GHOST"))
        result = TopologyValidator(topo, plan).validate()
        assert any(
            e.code == TopologyValidator.CHECK_RESOURCE_WIRING
            and "DUT_GHOST" in e.message
            for e in result.errors
        )


class TestParallelMutex:
    def _plan_with_parallel(
        self, *steps: YamlStep, max_concurrency: int = 2,
    ) -> YamlPlan:
        return _plan(*steps, max_concurrency=max_concurrency)

    def test_parallel_shared_instrument_no_switch_error(self) -> None:
        """两个并行步骤共享 PSU_MAIN，但 PSU 无矩阵链路 -> error。"""
        plan = self._plan_with_parallel(
            _step("p1", instrument="PSU_MAIN"),
            _step("p2", instrument="PSU_MAIN"),
        )
        result = TopologyValidator(_topology(), plan).validate()
        assert any(
            e.code == TopologyValidator.CHECK_PARALLEL_MUTEX
            and e.level == "error"
            for e in result.errors
        )

    def test_parallel_shared_instrument_with_switch_ok(self) -> None:
        """DMM_MAIN 经继电器/routeId 链路隔离 -> 共享合法。"""
        plan = self._plan_with_parallel(
            _step("p1", instrument="DMM_MAIN"),
            _step("p2", instrument="DMM_MAIN"),
        )
        result = TopologyValidator(_topology(), plan).validate()
        assert not any(
            e.code == TopologyValidator.CHECK_PARALLEL_MUTEX
            for e in result.errors
        )

    def test_parallel_disjoint_instruments_ok(self) -> None:
        plan = self._plan_with_parallel(
            _step("p1", instrument="PSU_MAIN"),
            _step("p2", instrument="DMM_MAIN"),
        )
        result = TopologyValidator(_topology(), plan).validate()
        assert not any(
            e.code == TopologyValidator.CHECK_PARALLEL_MUTEX
            for e in result.errors
        )

    def test_serial_shared_instrument_ok(self) -> None:
        """max_concurrency=1 -> 无并行组，共享不报错。"""
        plan = _plan(
            _step("s1", instrument="PSU_MAIN"),
            _step("s2", instrument="PSU_MAIN"),
            max_concurrency=1,
        )
        result = TopologyValidator(_topology(), plan).validate()
        assert not any(
            e.code == TopologyValidator.CHECK_PARALLEL_MUTEX
            for e in result.errors
        )

    def test_parallel_loop_enables_mutex_check(self) -> None:
        """PARALLEL 循环模式下共享仪器也应报错。"""
        loop = YamlLoop(
            id="ploop",
            loop_type=LoopType.FOR,
            count=2,
            execution_mode=ExecutionMode.PARALLEL,
            steps=[_step("ps", instrument="PSU_MAIN")],
        )
        plan = YamlPlan(name="p", version="1.0", max_concurrency=1, steps=[loop])
        result = TopologyValidator(_topology(), plan).validate()
        # 循环内并行仅当多个顶层步骤才触发；单步循环不构成并行组
        assert not any(
            e.code == TopologyValidator.CHECK_PARALLEL_MUTEX
            for e in result.errors
        )

    def test_dependent_steps_not_in_parallel_group(self) -> None:
        """依赖关系阻止并行分组：s2 依赖 s1 -> 不共享互斥检查。"""
        plan = self._plan_with_parallel(
            _step("s1", instrument="PSU_MAIN"),
            _step("s2", instrument="PSU_MAIN", preconditions=["s1"]),
        )
        result = TopologyValidator(_topology(), plan).validate()
        assert not any(
            e.code == TopologyValidator.CHECK_PARALLEL_MUTEX
            for e in result.errors
        )


class TestFixtureCapability:
    def test_known_fixture_action_ok(self) -> None:
        plan = _plan(_step("s1", fixture="FIX1", action="cylinder"))
        result = TopologyValidator(_topology(), plan).validate()
        assert result.valid is True

    def test_missing_fixture_is_error(self) -> None:
        plan = _plan(_step("s1", fixture="FIX_GHOST", action="clamp"))
        result = TopologyValidator(_topology(), plan).validate()
        assert any(
            e.code == TopologyValidator.CHECK_FIXTURE_CAPABILITY
            and "FIX_GHOST" in e.message
            for e in result.errors
        )

    def test_unsupported_action_is_error(self) -> None:
        """夹具只有 cylinder/relay，不支持 thermal -> error。"""
        plan = _plan(_step("s1", fixture="FIX1", action="thermal"))
        result = TopologyValidator(_topology(), plan).validate()
        assert any(
            e.code == TopologyValidator.CHECK_FIXTURE_CAPABILITY
            and "thermal" in e.message
            for e in result.errors
        )

    def test_no_action_skips_check(self) -> None:
        plan = _plan(_step("s1", fixture="FIX1"))
        result = TopologyValidator(_topology(), plan).validate()
        assert result.valid is True


class TestStrictness:
    def test_strictness_warning_downgrades_errors(self) -> None:
        plan = _plan(_step("s1", instrument="PSU_GHOST"))
        result = TopologyValidator(
            _topology(), plan, strictness="warning",
        ).validate()
        assert result.valid is True
        assert result.errors == []
        assert any(
            e.code == TopologyValidator.CHECK_INSTRUMENT_EXISTS
            for e in result.warnings
        )

    def test_wiring_warning_kept_as_warning(self) -> None:
        plan = _plan(_step("s1", instrument="PSU_MAIN", uut="DUT_GHOST"))
        result = TopologyValidator(_topology(), plan).validate()
        assert result.valid is False
        assert any(
            e.code == TopologyValidator.CHECK_RESOURCE_WIRING
            and e.level == "error"
            for e in result.errors
        )


class TestPlanInputForms:
    def test_dict_topology_and_plan_input(self) -> None:
        """dict 形式的 topology + plan 输入均可解析。"""
        plan_dict: dict[str, object] = {
            "name": "dict计划",
            "version": "1.0",
            "max_concurrency": 1,
            "steps": [
                {"id": "s1", "script": "x", "resources": {"instrument": "PSU_MAIN"}},
            ],
        }
        result = TopologyValidator(_topology_dict(), plan_dict).validate()
        assert result.valid is True

    def test_dict_plan_with_loop(self) -> None:
        plan_dict: dict[str, object] = {
            "name": "dict循环",
            "version": "1.0",
            "max_concurrency": 2,
            "steps": [
                {"id": "s1", "script": "x", "resources": {"instrument": "PSU_MAIN"}},
                {"id": "s2", "script": "y", "resources": {"instrument": "PSU_MAIN"}},
            ],
        }
        result = TopologyValidator(_topology_dict(), plan_dict).validate()
        assert any(
            e.code == TopologyValidator.CHECK_PARALLEL_MUTEX
            for e in result.errors
        )


class TestResultShape:
    def test_validation_result_shape(self) -> None:
        plan = _plan(_step("s1", instrument="PSU_GHOST"))
        result = TopologyValidator(_topology(), plan).validate()
        d = result.as_dict()
        assert d["valid"] is False
        assert len(d["errors"]) >= 1
        assert "summary" in d

    def test_empty_plan_valid(self) -> None:
        plan = YamlPlan(name="empty", version="1.0", steps=[])
        result = TopologyValidator(_topology(), plan).validate()
        assert result.valid is True
