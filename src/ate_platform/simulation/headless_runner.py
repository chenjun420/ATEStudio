"""Headless simulation runner (AC-12: 无头仿真输出 JUnit 报告).

在无硬件、无 NATS、无数据库环境下端到端运行仿真，输出控制台摘要
与可选 JUnit XML 报告，供 CI/CD 流水线接入（设计文档 §7.11 / AC-12）。

用法：
    python -m ate_platform.simulation.headless_runner plan.yaml \\
        [--tier dry_run|full] [--junit out.xml] [--fault-config rules.yaml]

- tier=dry_run：DryRunScheduler 调度遍历，只产生步骤决策
- tier=full：FullChainSimulator 全链路（调度 + 测量噪声注入 + 统计）
- JUnit XML 每步骤一条 testcase（PASS→通过，SKIP/BLOCKED→skipped，
  FAIL/ERROR/NOT_REACHED→failure），可直接被 GitLab/Jenkins 消费。
"""

from __future__ import annotations

import argparse
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ate_platform.dsl.parser import YamlParser
from shared.dsl import YamlPlan

from .dry_run_scheduler import DryRunScheduler
from .full_chain_simulator import FullChainSimulator

# 决策 → JUnit 结果映射
_PASS_DECISIONS = {"PASS"}
_SKIP_DECISIONS = {"SKIP", "BLOCKED"}
_FAIL_DECISIONS = {"FAIL", "ERROR", "NOT_REACHED"}


def _escape_xml(text: str) -> str:
    """Escape XML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _safe_text(value: Any) -> str:
    """Convert a value to a safe text string."""
    return _escape_xml("" if value is None else str(value))


def build_junit_xml(
    plan: YamlPlan,
    decisions: list[Any],
    tier: str,
    duration_s: float,
) -> ET.Element:
    """Build a JUnit testsuites element from dry-run decisions.

    Args:
        plan: The simulated plan (name/version for suite naming).
        decisions: List of StepDecision records from the dry run.
        tier: Simulation tier name ("dry_run" or "full").
        duration_s: Total wall-clock duration.

    Returns:
        Root ``<testsuites>`` element.
    """
    passed = 0
    failed = 0
    skipped = 0
    total = len(decisions)

    testsuite = ET.Element(
        "testsuite",
        {
            "name": f"{plan.name} (v{plan.version}, {tier})",
            "tests": str(total),
            "failures": "0",
            "errors": "0",
            "skipped": "0",
            "time": f"{duration_s:.3f}",
        },
    )

    for decision in decisions:
        step_id = getattr(decision, "step_id", "unknown")
        decision_value = str(getattr(decision, "decision", "PASS"))
        reason = str(getattr(decision, "reason", "") or "")

        testcase = ET.SubElement(
            testsuite,
            "testcase",
            {
                "name": step_id,
                "classname": f"ate.simulation.{tier}",
                "time": "0.000",
            },
        )

        if decision_value in _PASS_DECISIONS:
            passed += 1
            if reason:
                out = ET.SubElement(testcase, "system-out")
                out.text = reason
        elif decision_value in _SKIP_DECISIONS:
            skipped += 1
            sk = ET.SubElement(testcase, "skipped")
            sk.set("message", f"{decision_value}: {reason}")
        else:  # FAIL / ERROR / NOT_REACHED → failure
            failed += 1
            failure = ET.SubElement(
                testcase,
                "failure",
                {"message": f"{decision_value}: {step_id}"},
            )
            failure.text = reason

    testsuite.set("failures", str(failed))
    testsuite.set("skipped", str(skipped))

    testsuites = ET.Element(
        "testsuites",
        {
            "name": "ATE-SIM",
            "tests": str(total),
            "failures": str(failed),
            "errors": "0",
            "skipped": str(skipped),
            "time": f"{duration_s:.3f}",
        },
    )
    testsuites.append(testsuite)
    return testsuites


def run_headless(
    plan_path: Path,
    tier: str = "dry_run",
    junit_out: Path | None = None,
    fault_config_path: Path | None = None,
) -> int:
    """Run a headless simulation and optionally write a JUnit report.

    Args:
        plan_path: Path to the YAML DSL plan (v3.0/v3.2).
        tier: "dry_run" or "full".
        junit_out: Optional JUnit XML output path.
        fault_config_path: Optional fault-injection rules YAML path.

    Returns:
        Process exit code (0 = all steps passed, 1 = any step failed).
    """
    parser = YamlParser()
    plan = parser.parse(plan_path)

    # 校验计划：语义错误在仿真前暴露（而不是静默通过）
    errors = parser.validate(plan)
    if errors:
        print("[headless] plan validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    fault_config: list[dict[str, Any]] | None = None
    if fault_config_path is not None:
        import yaml as yaml_lib

        raw = yaml_lib.safe_load(fault_config_path.read_text(encoding="utf-8"))
        fault_config = raw if isinstance(raw, list) else (raw or {}).get("rules", [])

    start = time.monotonic()

    # 两分支解包到同一组标量，避免 mypy 对不同 result 类型的变量合并推断
    decisions: list[Any] = []
    summary = ""
    passed = 0
    failed = 0
    skipped = 0
    duration = 0.0
    all_passed = False

    if tier == "full":
        sim = FullChainSimulator(fault_config=fault_config)
        full_result = sim.run(plan, assume_pass=True)
        dry = full_result.dry_run_result
        decisions = dry.decisions
        summary = full_result.summary
        passed = dry.passed
        failed = dry.failed
        skipped = dry.skipped
        duration = full_result.total_duration_s
        # blocked/errors/not_reached 均视为失败（含循环依赖死锁）
        all_passed = dry.all_passed
    else:
        scheduler = DryRunScheduler()
        dry_result = scheduler.dry_run(plan, assume_pass=True)
        decisions = dry_result.decisions
        summary = dry_result.summary
        passed = dry_result.passed
        failed = dry_result.failed
        skipped = dry_result.skipped
        duration = dry_result.duration_s
        all_passed = dry_result.all_passed

    elapsed = time.monotonic() - start

    # 控制台摘要
    print(f"[headless] {summary}")
    print(
        f"[headless] {tier}: {passed} passed, {failed} failed, "
        f"{skipped} skipped, {len(decisions)} steps in {elapsed:.3f}s"
    )

    if junit_out is not None:
        root = build_junit_xml(plan, decisions, tier, duration)
        tree = ET.ElementTree(root)
        ET.indent(tree, space="  ")
        junit_out.parent.mkdir(parents=True, exist_ok=True)
        tree.write(junit_out, encoding="utf-8", xml_declaration=True)
        print(f"[headless] JUnit report written to {junit_out}")

    # all_passed 涵盖 blocked/errors/not_reached（循环依赖死锁等）
    return 0 if all_passed else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="ate-headless-sim",
        description="ATE Platform headless simulation runner (AC-12)",
    )
    parser.add_argument("plan", type=Path, help="YAML DSL plan file (v3.0/v3.2)")
    parser.add_argument(
        "--tier",
        choices=["dry_run", "full"],
        default="dry_run",
        help="simulation tier (default: dry_run)",
    )
    parser.add_argument("--junit", type=Path, default=None, help="JUnit XML output path")
    parser.add_argument(
        "--fault-config",
        type=Path,
        default=None,
        help="fault-injection rules YAML (list or {rules: [...]})",
    )
    args = parser.parse_args(argv)

    try:
        return run_headless(
            plan_path=args.plan,
            tier=args.tier,
            junit_out=args.junit,
            fault_config_path=args.fault_config,
        )
    except FileNotFoundError as exc:
        print(f"[headless] error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"[headless] error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
