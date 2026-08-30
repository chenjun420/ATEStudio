"""Integration tests for the headless simulation runner (AC-12).

Validates that the headless runner:
1. Loads v3.0/v3.2 YAML DSL plans via YamlParser.
2. Runs dry_run and full tiers without hardware/NATS/DB.
3. Emits a well-formed JUnit XML report with one testcase per step.
4. Returns a non-zero exit code when any step fails.

Run under ATE_SIMULATION_MODE=true in CI to exercise the headless path.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ate_platform.simulation.headless_runner import (
    build_junit_xml,
    run_headless,
)


@pytest.fixture
def v32_plan(tmp_path: Path) -> Path:
    """A minimal v3.2 plan exercising fixture_control/barrier/action steps."""
    content = """\
name: headless_v32
version: "3.2"
scope: production
max_concurrency: 2
steps:
  - id: fixture_clamp
    type: fixture_control
    action: clamp
    fixture_id: "fixture_ps_12v5a_v1"
  - id: power_on
    type: action
    script: dmm_measure.py
    params: { expected_value: 12.0 }
    depends_on: [fixture_clamp]
  - id: sync_power_on
    type: barrier
    barrier_name: "all_powered_on"
    depends_on: [power_on]
  - id: measure
    type: action
    script: dmm_measure.py
    params: { expected_value: 12.0 }
    depends_on: [sync_power_on]
    timeout: 10
    retry: 2
    on_failure: continue
"""
    path = tmp_path / "headless_v32.yaml"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def v32_multi_uut_plan(tmp_path: Path) -> Path:
    """A v3.2 plan with 2 UUTs exercising a real sync barrier."""
    content = """\
name: headless_v32_multi_uut
version: "3.2"
scope: production
uut_count: 2
max_concurrency: 2
steps:
  - id: fixture_clamp
    type: fixture_control
    action: clamp
    fixture_id: "fixture_ps_12v5a_v1"
  - id: power_on
    type: action
    script: dmm_measure.py
    depends_on: [fixture_clamp]
  - id: sync_power_on
    type: barrier
    barrier_name: "all_powered_on"
    depends_on: [power_on]
  - id: measure
    type: action
    script: dmm_measure.py
    depends_on: [sync_power_on]
    retry: 1
    on_failure: continue
  - id: fixture_release
    type: fixture_control
    action: release
    fixture_id: "fixture_ps_12v5a_v1"
    depends_on: [measure]
"""
    path = tmp_path / "headless_v32_multi_uut.yaml"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def v30_plan(tmp_path: Path) -> Path:
    """A legacy v3.0 plan (no type fields) — backward-compat coverage."""
    content = """\
name: headless_v30
version: "1.0"
scope: production
steps:
  - id: init
    script: dmm_measure.py
    params: { expected_value: 3.3 }
  - id: measure
    script: dmm_measure.py
    params: { expected_value: 3.3 }
    preconditions: [init]
"""
    path = tmp_path / "headless_v30.yaml"
    path.write_text(content, encoding="utf-8")
    return path


class TestHeadlessDryRun:
    """Tests for the dry_run tier."""

    def test_dry_run_v32_passes(self, v32_plan: Path) -> None:
        code = run_headless(plan_path=v32_plan, tier="dry_run")
        assert code == 0

    def test_dry_run_v30_backward_compat(self, v30_plan: Path) -> None:
        code = run_headless(plan_path=v30_plan, tier="dry_run")
        assert code == 0

    def test_dry_run_writes_junit(self, v32_plan: Path, tmp_path: Path) -> None:
        junit = tmp_path / "sim-report.xml"
        code = run_headless(plan_path=v32_plan, tier="dry_run", junit_out=junit)
        assert code == 0
        assert junit.exists()

        root = ET.parse(junit).getroot()
        assert root.tag == "testsuites"
        suite = root.find("testsuite")
        assert suite is not None
        assert suite.get("name") == "headless_v32 (v3.2, dry_run)"
        # one testcase per step: fixture_clamp, power_on, sync_power_on, measure
        cases = suite.findall("testcase")
        assert len(cases) == 4
        assert {c.get("name") for c in cases} == {
            "fixture_clamp",
            "power_on",
            "sync_power_on",
            "measure",
        }
        assert suite.get("failures") == "0"

    def test_dry_run_failed_step_returns_nonzero(
        self, tmp_path: Path
    ) -> None:
        # Plan whose steps have unsatisfiable preconditions → failed/not_reached
        content = """\
name: headless_fail
version: "1.0"
scope: production
steps:
  - id: impossible
    script: dmm_measure.py
    preconditions: [never_runs]
  - id: never_runs
    script: dmm_measure.py
    preconditions: [impossible]
"""
        plan = tmp_path / "fail_plan.yaml"
        plan.write_text(content, encoding="utf-8")
        code = run_headless(plan_path=plan, tier="dry_run")
        assert code == 1

    def test_build_junit_xml_marks_skips_and_failures(
        self, tmp_path: Path
    ) -> None:
        from shared.dsl import YamlPlan, YamlStep

        plan = YamlPlan(
            name="mixed",
            version="1.0",
            scope={"name": "production"},
            steps=[YamlStep(id="a", script="x.py")],
        )

        class FakeDecision:
            def __init__(self, step_id: str, decision: str, reason: str = "") -> None:
                self.step_id = step_id
                self.decision = decision
                self.reason = reason

        decisions = [
            FakeDecision("ok", "PASS"),
            FakeDecision("skipped", "SKIP", "skip_if true"),
            FakeDecision("blocked", "BLOCKED", "resource busy"),
            FakeDecision("bad", "FAIL", "measurement out of range"),
        ]
        root = build_junit_xml(plan, decisions, "dry_run", 1.5)
        suite = root.find("testsuite")
        assert suite is not None
        assert suite.get("tests") == "4"
        assert suite.get("failures") == "1"
        assert suite.get("skipped") == "2"
        assert len(suite.findall(".//failure")) == 1
        assert len(suite.findall(".//skipped")) == 2


class TestHeadlessFull:
    """Tests for the full-chain tier."""

    def test_full_tier_v32(self, v32_plan: Path) -> None:
        code = run_headless(plan_path=v32_plan, tier="full")
        assert code == 0

    def test_full_tier_writes_junit(self, v32_plan: Path, tmp_path: Path) -> None:
        junit = tmp_path / "sim-full.xml"
        code = run_headless(plan_path=v32_plan, tier="full", junit_out=junit)
        assert code == 0
        root = ET.parse(junit).getroot()
        suite = root.find("testsuite")
        assert suite is not None
        assert suite.get("failures") == "0"

    def test_fault_config_loaded(self, v32_plan: Path, tmp_path: Path) -> None:
        # §7.7.2 fault_injection 段格式：layer + target + trigger + action
        rules = tmp_path / "rules.yaml"
        rules.write_text(
            "rules:\n"
            "  - fault_id: r1\n"
            "    layer: instrument\n"
            "    target: DMM\n"
            "    trigger: { type: count, value: 1 }\n"
            "    action: { type: value_override, value: 5.0 }\n",
            encoding="utf-8",
        )
        code = run_headless(
            plan_path=v32_plan,
            tier="full",
            fault_config_path=rules,
        )
        # fault config loads without error; exit code reflects dry-run result
        assert code == 0


class TestHeadlessV32Semantic:
    """Tests for the v32 tier — real barrier/fixture_control semantics."""

    def test_v32_tier_passes(self, v32_multi_uut_plan: Path) -> None:
        """v32 tier runs to completion with all steps passing."""
        code = run_headless(plan_path=v32_multi_uut_plan, tier="v32")
        assert code == 0

    def test_v32_tier_writes_junit_with_barrier(
        self,
        v32_multi_uut_plan: Path,
        tmp_path: Path,
    ) -> None:
        """JUnit report includes barrier + fixture steps with zero failures."""
        junit = tmp_path / "sim-v32.xml"
        code = run_headless(
            plan_path=v32_multi_uut_plan, tier="v32", junit_out=junit,
        )
        assert code == 0
        root = ET.parse(junit).getroot()
        suite = root.find("testsuite")
        assert suite is not None
        assert suite.get("failures") == "0"
        names = [str(tc.get("name")) for tc in suite]
        # barrier step present and passed (system-out detail mentions all UUTs)
        assert "sync_power_on" in names
        barrier = next(tc for tc in suite if tc.get("name") == "sync_power_on")
        sysout = barrier.find("system-out")
        assert sysout is not None and "all UUTs" in (sysout.text or "")
        # fixture clamp/release present
        assert "fixture_clamp" in names
        assert "fixture_release" in names

    def test_v32_tier_v30_plan_backward_compat(self, v30_plan: Path) -> None:
        """Legacy v3.0 plan (no type) runs under v32 tier as script steps."""
        code = run_headless(plan_path=v30_plan, tier="v32")
        assert code == 0
