"""PA601-D54A 23-TC 虚拟 commissioning 运行器 + 步骤脚本的静态校验。

覆盖：
- ``data/commission/scripts/`` 下 23 个脚本存在、可编译、与计划 step.script
  一一对应，且每个脚本自含 ``_driver()`` / ``_judge(measured, value)`` 契约；
- ``data/commission/pa601_virtual_limits.yaml`` overlay schema 合法、覆盖
  占位步 tc05/06/08/21/22 且数值合理；
- 任一脚本经 importlib 加载后 ``_judge`` 的边界归一化语义
  （反向区间交换、单边按语义判定、边界含等号）。

这些用例全部离线（不触发 MatDeviceHttpDriver 的设备调用：脚本执行体被
``if __name__ == "__main__":`` 包住，importlib 导入不会运行）。
"""

from __future__ import annotations

import argparse
import importlib.util
import py_compile
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from ate_platform.dsl.parser import YamlParser

# tests/unit/commission/<this>.py → parents[3] = 仓库根
REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = REPO_ROOT / "scripts" / "commission_full_virtual.py"
SCRIPTS_DIR = REPO_ROOT / "data" / "commission" / "scripts"
PLAN_PATH = REPO_ROOT / "data" / "commission" / "pa601_full_commission.yaml"
OVERLAY_PATH = REPO_ROOT / "data" / "commission" / "pa601_virtual_limits.yaml"

EXPECTED_SCRIPT_COUNT = 23
OVERLAY_REQUIRED = frozenset({"tc05", "tc06", "tc08", "tc21", "tc22"})


@pytest.fixture(scope="module")
def plan() -> Any:
    """解析 23 步计划。"""
    assert PLAN_PATH.exists(), f"计划缺失: {PLAN_PATH}"
    return YamlParser().parse(PLAN_PATH)


@pytest.fixture(scope="module")
def overlay() -> dict[str, Any]:
    """读取 overlay YAML。"""
    assert OVERLAY_PATH.exists(), f"overlay 缺失: {OVERLAY_PATH}"
    return yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8"))


def _load_script(stem: str) -> ModuleType:
    """用 importlib 按文件路径加载一个步骤脚本（不触发设备调用）。"""
    path = SCRIPTS_DIR / f"{stem}.py"
    assert path.exists(), f"脚本缺失: {path}"
    spec = importlib.util.spec_from_file_location(f"_commission_script_{stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCommissionScripts:
    """23 个步骤脚本的存在性、可编译性、与计划的一一对应。"""

    def test_scripts_dir_has_exactly_23_files(self) -> None:
        files = sorted(p.name for p in SCRIPTS_DIR.glob("*.py"))
        assert len(files) == EXPECTED_SCRIPT_COUNT, f"脚本数不符: {files}"

    def test_every_plan_step_has_script(self, plan: Any) -> None:
        """计划 step.script 均有对应文件。"""
        for step in plan.steps:
            assert (SCRIPTS_DIR / step.script).exists(), f"缺脚本: {step.script}"

    def test_scripts_one_to_one_with_plan(self, plan: Any) -> None:
        """scripts/ 文件名集合 == 计划 step.script 集合（一一对应）。"""
        dir_names = {p.name for p in SCRIPTS_DIR.glob("*.py")}
        plan_names = {s.script for s in plan.steps}
        assert dir_names == plan_names, (
            f"多出={sorted(dir_names - plan_names)}, 缺失={sorted(plan_names - dir_names)}"
        )

    def test_every_script_compiles(self) -> None:
        for path in sorted(SCRIPTS_DIR.glob("*.py")):
            py_compile.compile(str(path), doraise=True)

    def test_every_script_self_contains_contract(self) -> None:
        """每个脚本自含 _driver / _judge(measured, value) / __main__ 执行体。"""
        for path in sorted(SCRIPTS_DIR.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            assert "def _driver()" in text, f"{path.name} 缺 _driver()"
            assert "def _judge(measured, value)" in text, f"{path.name} 缺 _judge 契约"
            assert 'if __name__ == "__main__":' in text, f"{path.name} 缺执行体守卫"
            assert "MatDeviceHttpDriver" in text, f"{path.name} 未用 MatDeviceHttpDriver"
            assert 'force_measured' in text, f"{path.name} 不支持故障注入"


class TestOverlay:
    """overlay schema 与占位步覆盖。"""

    def test_schema_has_version_and_steps(self, overlay: dict[str, Any]) -> None:
        assert overlay.get("version")
        steps = overlay.get("steps")
        assert isinstance(steps, dict) and steps

    def test_covers_required_placeholder_steps(self, overlay: dict[str, Any]) -> None:
        covered = {key[:4] for key in overlay["steps"]}
        assert OVERLAY_REQUIRED <= covered, f"未覆盖: {sorted(OVERLAY_REQUIRED - covered)}"

    def test_each_entry_has_bounded_value_and_note(
        self, overlay: dict[str, Any]
    ) -> None:
        for key, entry in overlay["steps"].items():
            assert isinstance(entry, dict), key
            value = entry.get("value")
            assert isinstance(value, dict), key
            assert any(k in value for k in ("min", "max")), f"{key} 无 min/max"
            assert str(entry.get("note", "")).strip(), f"{key} 缺 note"

    def test_required_values_are_reasonable(self, overlay: dict[str, Any]) -> None:
        """占位步数值合理（上界为正、单边/双边区间的 min≤max）。"""
        steps = overlay["steps"]
        tc05 = steps["tc05_power_sequence"]["value"]
        assert tc05["max"] == 500.0 and tc05["unit"] == "ms"
        tc06 = steps["tc06_inrush"]["value"]
        assert tc06["max"] == 60.0 and tc06["unit"] == "A"
        tc08 = steps["tc08_uvlo"]["value"]
        assert tc08["min"] == 70.0 and tc08["max"] == 90.0 and tc08["min"] <= tc08["max"]
        tc21 = steps["tc21_scp"]["value"]
        assert tc21["max"] == 5.0
        tc22 = steps["tc22_otp"]["value"]
        assert tc22["max"] == 125.0


class TestJudgeNormalization:
    """``_judge`` 的边界归一化与单边语义（GAP-01 同源）。"""

    def test_reversed_interval_is_swapped_then_passes(self) -> None:
        """min(-53.2) > max(-54.8) → 交换后 -53.46 落在 [-54.8, -53.2] → OK。"""
        module = _load_script("commission_measure_voltage")
        assert module._judge(-53.46, {"min": -53.2, "max": -54.8}) is True
        assert module.result_measured_value == pytest.approx(-53.46)
        # detail 含实测与归一化区间两端
        assert "-53.46" in module.result_detail
        assert "-54.8" in module.result_detail and "-53.2" in module.result_detail

    def test_single_sided_min_rejects_below(self) -> None:
        """只有 min 视为下限：-56.98 < -54.0 → FAIL。"""
        module = _load_script("commission_measure_voltage")
        with pytest.raises(AssertionError):
            module._judge(-56.98, {"min": -54.0})

    def test_boundary_equal_passes(self) -> None:
        """边界含等号：-54.0 == min → OK。"""
        module = _load_script("commission_measure_voltage")
        assert module._judge(-54.0, {"min": -54.0}) is True

    def test_single_sided_max_and_no_criterion(self) -> None:
        """只有 max 视为上限；两侧皆空 → 抛错（不得默认通过）。"""
        module = _load_script("commission_measure_voltage")
        assert module._judge(5.0, {"max": 6.0}) is True
        with pytest.raises(AssertionError):
            module._judge(5.0, {"unit": "V"})

    def test_reversed_interval_still_rejects_out_of_range(self) -> None:
        """归一化不能把判据变成无底线：交换后越界仍 FAIL。"""
        module = _load_script("commission_measure_voltage")
        with pytest.raises(AssertionError):
            module._judge(-56.0, {"min": -53.2, "max": -54.8})
        with pytest.raises(AssertionError):
            module._judge(-52.0, {"min": -53.2, "max": -54.8})


def _load_runner() -> ModuleType:
    """按文件路径加载 runner（scripts/ 非包，故用 importlib）。

    ``@dataclass`` 会查 ``sys.modules[cls.__module__]``，因此 exec 前须先把模块
    注册进 ``sys.modules``（否则 dataclass 处理时抛 AttributeError）。
    """
    assert RUNNER_PATH.exists(), f"runner 缺失: {RUNNER_PATH}"
    spec = importlib.util.spec_from_file_location("_commission_full_virtual", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestRunnerPlumbing:
    """runner 的静态自检入口与 overlay/inject 合并（不需启动 MDC）。"""

    def test_check_mode_exits_zero(self) -> None:
        runner = _load_runner()
        assert runner.main(["--check"]) == 0

    def test_prepare_plan_merges_overlay_device_and_inject(self) -> None:
        runner = _load_runner()
        args = argparse.Namespace(inject=["tc06=100.0"], mdc_repo="unused")
        instance = runner.VirtualCommissionRunner(args)
        plan = instance._prepare_plan("http://127.0.0.1:9999")

        by_tc = {runner._tc_prefix(s.id): s for s in plan.steps}
        # overlay 替换占位步 value（tc06 max=60）
        assert by_tc["tc06"].params["value"] == {"max": 60.0, "unit": "A"}
        # 计划自带限值步骤不被 overlay 改动（tc10）
        assert by_tc["tc10"].params["value"]["min"] == 51.3
        # device_id / base_url 注入
        assert by_tc["tc06"].params["device_id"] == "scope001"
        assert by_tc["tc23"].params["device_id"] == "aging001"
        assert all(s.params["base_url"] == "http://127.0.0.1:9999" for s in plan.steps)
        # 注入 force_measured 只落在目标步
        assert by_tc["tc06"].params["force_measured"] == 100.0
        assert "force_measured" not in by_tc["tc05"].params

    def test_inject_parsing_rejects_unknown_tc(self) -> None:
        runner = _load_runner()
        args = argparse.Namespace(inject=["tc99=1.0"], mdc_repo="unused")
        with pytest.raises(runner.CommissionError):
            runner.VirtualCommissionRunner(args)

    def test_virtual_drivers_registered_false_without_factory(self, tmp_path: Path) -> None:
        """无 factory.py 的仓库 → 未就绪（live 会被跳过）。"""
        runner = _load_runner()
        assert runner.virtual_drivers_registered(tmp_path) is False
