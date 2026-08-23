"""Headless simulation runner (AC-12: 无头仿真输出 JUnit 报告).

在无硬件、无 NATS、无数据库环境下端到端运行仿真，输出控制台摘要
与可选 JUnit XML 报告，供 CI/CD 流水线接入（设计文档 §7.11 / AC-12）。

用法：
    python -m ate_platform.simulation.headless_runner plan.yaml \\
        [--tier dry_run|full|v32] [--junit|--junit-output out.xml] \\
        [--fault-config rules.yaml] [--uut-count N] [--profile sim.yaml] \\
        [--html-report out.html]

§7.10 CI 契约新增旗标（T15）：
- --uut-count N：覆盖计划级 uut_count（驱动 v32 UUTManager 规模）
- --profile PATH：仿真 profile YAML（noise_model/noise_sigma/drift_rate/bias/seed，
  full 层注入 NoiseConfig；所有层都会校验文件）
- --html-report PATH：自包含 HTML 摘要报告（步骤表 + 通过/失败 + 耗时 +
  覆盖率小节，内联 CSS，仅标准库）
- --junit-output：--junit 的规范别名（向后兼容保留旧旗标）

- tier=dry_run：DryRunScheduler 调度遍历，只产生步骤决策
- tier=full：FullChainSimulator 全链路（调度 + 测量噪声注入 + 统计）
- tier=v32：V32PlanDispatcher 语义仿真（barrier 同步 + 夹具动作 +
  retry/on_failure，§6.5.4）——端到端验证 v3.2 步骤类型
- JUnit XML 每步骤一条 testcase（PASS→通过，SKIP/BLOCKED→skipped，
  FAIL/ERROR/NOT_REACHED→failure），可直接被 GitLab/Jenkins 消费。
"""

from __future__ import annotations

import argparse
import html
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ate_platform.dsl.parser import YamlParser
from shared.dsl import YamlPlan

from .coverage import SimulationCoverage
from .dry_run_scheduler import DryRunScheduler
from .full_chain_simulator import FullChainSimulator
from .instrument_simulator import NoiseConfig, NoiseModel

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


# ---------------------------------------------------------------------------
# §7.10 simulation profile (T15)
# ---------------------------------------------------------------------------

_NOISE_MODEL_NAMES = frozenset(model.name for model in NoiseModel)

# Dry-run loop expansion synthesizes ids like ``loop#0#child`` (nested loops
# chain more ``#i#`` segments) while SequenceCompiler expands the same nodes
# as ``child_iter0[_iter1...]``. Coverage's universe is the compiled list, so
# translate between the two formats before recording.
def _compiled_step_id(expanded_id: str) -> str:
    """Map a dry-run expanded id (``loop#i#child``) to ``child_iterI[...]``."""
    if "#" not in expanded_id:
        return expanded_id
    parts = expanded_id.split("#")
    leaf = parts[-1]
    iterations = [p for p in parts[:-1] if p.isdigit()]
    return leaf + "".join(f"_iter{i}" for i in iterations)


def _load_simulation_profile(profile_path: Path) -> dict[str, Any]:
    """Load and shape-check a simulation profile YAML (§7.10 --profile).

    The profile is a mapping with optional keys ``noise_model``
    (GAUSSIAN/DRIFT/BIAS/FULL/NONE), ``noise_sigma``, ``drift_rate``,
    ``bias`` and ``seed``. Unknown keys are tolerated for forward compat.

    Raises:
        FileNotFoundError: Propagated when the path does not exist.
        ValueError: When the YAML is not a mapping or numeric fields are
            malformed.
    """
    import yaml as yaml_lib

    raw = yaml_lib.safe_load(profile_path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        msg = (
            "simulation profile must be a YAML mapping, got "
            f"{type(raw).__name__}: {profile_path}"
        )
        raise ValueError(msg)
    return raw


def _profile_to_noise_config(profile: dict[str, Any]) -> NoiseConfig | None:
    """Convert a loaded profile dict into a NoiseConfig (None if empty).

    Raises:
        ValueError: On unknown noise_model names or malformed numbers.
    """
    if not profile:
        return None

    kwargs: dict[str, Any] = {}
    model_name = profile.get("noise_model")
    if model_name is not None:
        name = str(model_name).upper()
        if name not in _NOISE_MODEL_NAMES:
            msg = (
                f"unknown noise_model {model_name!r} in simulation profile "
                f"(expected one of {sorted(_NOISE_MODEL_NAMES)})"
            )
            raise ValueError(msg)
        kwargs["model"] = NoiseModel[name]
    for key in ("noise_sigma", "drift_rate", "bias"):
        if profile.get(key) is not None:
            kwargs[key] = float(profile[key])
    if profile.get("seed") is not None:
        kwargs["seed"] = int(profile["seed"])
    return NoiseConfig(**kwargs) if kwargs else None


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


def build_junit_xml_from_outcomes(
    plan: YamlPlan,
    outcomes: list[Any],
    tier: str,
    duration_s: float,
) -> ET.Element:
    """Build a JUnit testsuites element from v32 dispatcher outcomes.

    与 :func:`build_junit_xml` 同构，但消费 :class:`StepOutcome` 列表
    （含循环展开后的子步骤）。PASS→通过，SKIP→skipped，FAIL/BLOCKED→failure。

    Args:
        plan: 仿真的计划（用于套件命名）。
        outcomes: :class:`StepOutcome` 列表。
        tier: 仿真 tier 名（"v32"）。
        duration_s: 总墙钟耗时。

    Returns:
        根 ``<testsuites>`` 元素。
    """
    passed = 0
    failed = 0
    skipped = 0
    total = len(outcomes)

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

    for outcome in outcomes:
        step_id = str(getattr(outcome, "step_id", "unknown"))
        status = str(getattr(outcome, "status", "PASS"))
        detail = str(getattr(outcome, "detail", "") or "")

        testcase = ET.SubElement(
            testsuite,
            "testcase",
            {
                "name": step_id,
                "classname": f"ate.simulation.{tier}",
                "time": "0.000",
            },
        )

        if status == "PASS":
            passed += 1
            if detail:
                out = ET.SubElement(testcase, "system-out")
                out.text = detail
        elif status == "SKIP":
            skipped += 1
            sk = ET.SubElement(testcase, "skipped")
            sk.set("message", f"{status}: {detail}")
        else:  # FAIL / BLOCKED → failure
            failed += 1
            failure = ET.SubElement(
                testcase,
                "failure",
                {"message": f"{status}: {step_id}"},
            )
            failure.text = detail

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


# ---------------------------------------------------------------------------
# §7.10 self-contained HTML report (T15)
# ---------------------------------------------------------------------------

_HTML_CSS = """\
body{font-family:'Segoe UI',Arial,sans-serif;margin:24px;color:#1f2933;background:#f5f7fa}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:24px 0 8px}
.meta{color:#52606d;font-size:13px;margin-bottom:16px}
.cards{display:flex;gap:12px;margin:12px 0}
.card{flex:1;padding:10px 14px;border-radius:8px;background:#fff;border:1px solid #cbd2d9}
.card .num{font-size:22px;font-weight:600}.card .lbl{font-size:12px;color:#52606d}
.pass .num{color:#1b7f4d}.fail .num{color:#c0392b}.skip .num{color:#b7791f}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border:1px solid #cbd2d9;padding:6px 10px;text-align:left}
th{background:#e4e7eb}.badge{font-weight:600}
.badge.PASS,.badge.SKIP-ok{color:#1b7f4d}.badge.FAIL,.badge.ERROR,
.badge.NOT_REACHED,.badge.BLOCKED{color:#c0392b}.badge.SKIP{color:#b7791f}
.coverage{background:#fff;border:1px solid #cbd2d9;border-radius:8px;padding:12px 16px;font-size:13px}
.coverage b{font-size:15px}ul{margin:6px 0 0;padding-left:20px}
"""


def build_html_report(
    plan: YamlPlan,
    rows: list[tuple[str, str, str]],
    tier: str,
    duration_s: float,
    passed: int,
    failed: int,
    skipped: int,
    *,
    uut_count: int | None = None,
    profile_path: Path | None = None,
    coverage: dict[str, Any] | None = None,
) -> str:
    """Build a self-contained HTML summary (inline CSS, stdlib only).

    Args:
        plan: The simulated plan (name/version in the header).
        rows: ``(step_id, status, detail)`` triples, one per step.
        tier: Simulation tier name.
        duration_s: Total wall-clock duration.
        passed/failed/skipped: Outcome counters for the summary cards.
        uut_count: Effective UUT count (plan value or --uut-count override).
        profile_path: Simulation profile path when ``--profile`` was given.
        coverage: Optional :meth:`SimulationCoverage.report` dict enriching
            the page with a §7.10 coverage section.

    Returns:
        Complete HTML document string.
    """
    esc = html.escape

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{esc(plan.name)} — headless simulation report</title>",
        f"<style>{_HTML_CSS}</style>",
        "</head>",
        "<body>",
        f"<h1>{esc(plan.name)}</h1>",
        (
            f'<div class="meta">version v{esc(str(plan.version))} · '
            f"tier {esc(tier)} · UUTs {esc(str(uut_count if uut_count is not None else plan.uut_count))} · "
            f"duration {duration_s:.3f}s"
            + (f" · profile {esc(str(profile_path))}" if profile_path else "")
            + "</div>"
        ),
        '<div class="cards">',
        f'<div class="card pass"><div class="num">{passed}</div><div class="lbl">PASSED</div></div>',
        f'<div class="card fail"><div class="num">{failed}</div><div class="lbl">FAILED</div></div>',
        f'<div class="card skip"><div class="num">{skipped}</div><div class="lbl">SKIPPED</div></div>',
        "</div>",
        "<h2>Steps</h2>",
        "<table><thead><tr><th>Step</th><th>Result</th><th>Detail</th></tr></thead><tbody>",
    ]

    for step_id, status, detail in rows:
        parts.append(
            "<tr>"
            f"<td>{esc(step_id)}</td>"
            f'<td class="badge {esc(status)}">{esc(status)}</td>'
            f"<td>{esc(detail)}</td>"
            "</tr>"
        )

    parts.append("</tbody></table>")

    if coverage is not None:
        step_cov = coverage.get("step_coverage", {})
        branch_cov = coverage.get("branch_coverage", {})
        summary = coverage.get("summary", {})
        unexecuted = list(step_cov.get("unexecuted", []))
        parts += [
            "<h2>Coverage</h2>",
            '<div class="coverage">',
            f"<b>Steps:</b> {esc(str(step_cov.get('percent', 0.0)))}% "
            f"({esc(str(step_cov.get('executed', 0)))}/{esc(str(step_cov.get('planned', 0)))}) · ",
            f"<b>Branches:</b> {esc(str(branch_cov.get('percent', 0.0)))}% · ",
            f"<b>Quality gate:</b> {esc(str(summary.get('quality', 'unknown')))}",
        ]
        if unexecuted:
            items = "".join(f"<li>{esc(sid)}</li>" for sid in unexecuted)
            parts.append(f"<ul>{items}</ul>")
        parts.append("</div>")

    parts += ["</body>", "</html>"]
    return "\n".join(parts)


def run_headless(
    plan_path: Path,
    tier: str = "dry_run",
    junit_out: Path | None = None,
    fault_config_path: Path | None = None,
    uut_count: int | None = None,
    profile_path: Path | None = None,
    html_report_path: Path | None = None,
) -> int:
    """Run a headless simulation and optionally write JUnit/HTML reports.

    Args:
        plan_path: Path to the YAML DSL plan (v3.0/v3.2).
        tier: "dry_run", "full" or "v32".
        junit_out: Optional JUnit XML output path.
        fault_config_path: Optional fault-injection rules YAML path.
        uut_count: Optional --uut-count override of the plan-level UUT
            count (drives the v32 dispatcher's UUTManager size).
        profile_path: Optional simulation profile YAML (§7.10). Validated
            for every tier; the noise section feeds the full tier.
        html_report_path: Optional self-contained HTML summary output path.

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

    # §7.10 --uut-count：CLI 覆盖计划级 UUT 数（v32 层驱动 UUTManager 规模）
    if uut_count is not None:
        if uut_count < 1:
            msg = f"--uut-count must be >= 1, got {uut_count}"
            raise ValueError(msg)
        plan.uut_count = uut_count
        print(f"[headless] uut_count overridden to {uut_count}")

    # §7.10 --profile：所有层都先校验（坏路径/坏内容在任何 tier 都报错），
    # 噪声段仅注入 full 层的 NoiseConfig。
    noise_config: NoiseConfig | None = None
    if profile_path is not None:
        profile = _load_simulation_profile(profile_path)
        noise_config = _profile_to_noise_config(profile)

    fault_config: list[dict[str, Any]] | None = None
    if fault_config_path is not None:
        import yaml as yaml_lib

        raw = yaml_lib.safe_load(fault_config_path.read_text(encoding="utf-8"))
        fault_config = raw if isinstance(raw, list) else (raw or {}).get("rules", [])

    start = time.monotonic()

    # 各分支解包到同一组标量，避免 mypy 对不同 result 类型的变量合并推断
    decisions: list[Any] = []
    outcomes: list[Any] = []
    summary = ""
    passed = 0
    failed = 0
    skipped = 0
    duration = 0.0
    all_passed = False

    if tier == "v32":
        # v3.2 语义仿真：按 step.type 分发到 FixtureController / UUTManager，
        # 端到端验证 barrier 同步 + 夹具动作 + retry/on_failure（§6.5.4）。
        import asyncio

        from ate_platform.executor.v32_dispatcher import V32PlanDispatcher

        dispatcher = V32PlanDispatcher(plan, simulation=True)
        outcomes = asyncio.run(dispatcher.run())
        passed = sum(1 for o in outcomes if o.status == "PASS")
        failed = sum(1 for o in outcomes if o.status in ("FAIL", "BLOCKED"))
        skipped = sum(1 for o in outcomes if o.status == "SKIP")
        duration = time.monotonic() - start
        summary = (
            f"V32('{plan.name}'): {passed} pass, {failed} fail, "
            f"{skipped} skip, {len(outcomes)} steps"
        )
        all_passed = failed == 0
    elif tier == "full":
        sim = FullChainSimulator(noise_config=noise_config, fault_config=fault_config)
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
    total_steps = len(outcomes) if tier == "v32" else len(decisions)
    print(f"[headless] {summary}")
    print(
        f"[headless] {tier}: {passed} passed, {failed} failed, "
        f"{skipped} skipped, {total_steps} steps in {elapsed:.3f}s"
    )

    if junit_out is not None:
        if tier == "v32":
            root = build_junit_xml_from_outcomes(plan, outcomes, tier, duration)
        else:
            root = build_junit_xml(plan, decisions, tier, duration)
        tree = ET.ElementTree(root)
        ET.indent(tree, space="  ")
        junit_out.parent.mkdir(parents=True, exist_ok=True)
        tree.write(junit_out, encoding="utf-8", xml_declaration=True)
        print(f"[headless] JUnit report written to {junit_out}")

    # §7.10 --html-report：自包含 HTML 摘要（步骤表 + 汇总 + 可选覆盖率小节）
    if html_report_path is not None:
        if tier == "v32":
            rows = [
                (str(o.step_id), str(o.status), str(getattr(o, "detail", "") or ""))
                for o in outcomes
            ]
        else:
            rows = [
                (d.step_id, str(d.decision), str(d.reason or ""))
                for d in decisions
            ]

        coverage_report: dict[str, Any] | None = None
        try:
            if tier == "v32":
                executed_raw = [
                    str(o.step_id) for o in outcomes if o.status == "PASS"
                ]
                skipped_raw = [
                    str(o.step_id) for o in outcomes if o.status == "SKIP"
                ]
            else:
                executed_raw = [
                    d.step_id for d in decisions if d.decision == "PASS"
                ]
                skipped_raw = [
                    d.step_id for d in decisions if d.decision == "SKIP"
                ]
            from ate_platform.scheduler.compiler import SequenceCompiler

            compiled = SequenceCompiler().compile(plan)
            cov = SimulationCoverage(compiled)
            cov.record(
                executed_ids=[_compiled_step_id(sid) for sid in executed_raw],
                skipped_ids=[_compiled_step_id(sid) for sid in skipped_raw],
            )
            coverage_report = cov.report()
        except Exception:  # noqa: BLE001 - 覆盖率是尽力而为的富化，绝不阻断运行
            coverage_report = None

        html_content = build_html_report(
            plan=plan,
            rows=rows,
            tier=tier,
            duration_s=elapsed,
            passed=passed,
            failed=failed,
            skipped=skipped,
            uut_count=uut_count,
            profile_path=profile_path,
            coverage=coverage_report,
        )
        html_report_path.parent.mkdir(parents=True, exist_ok=True)
        html_report_path.write_text(html_content, encoding="utf-8")
        print(f"[headless] HTML report written to {html_report_path}")

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
        choices=["dry_run", "full", "v32"],
        default="dry_run",
        help="simulation tier (default: dry_run)",
    )
    # §7.10：--junit-output 为规范别名，--junit 保留向后兼容（同一 dest）
    parser.add_argument(
        "--junit",
        "--junit-output",
        type=Path,
        default=None,
        help="JUnit XML output path (--junit-output is the canonical alias)",
    )
    parser.add_argument(
        "--uut-count",
        type=int,
        default=None,
        help="override the plan-level UUT count (v3.2 multi-UUT, >= 1)",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="simulation profile YAML (noise_model/noise_sigma/drift_rate/bias/seed)",
    )
    parser.add_argument(
        "--html-report",
        type=Path,
        default=None,
        help="self-contained HTML summary report output path",
    )
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
            uut_count=args.uut_count,
            profile_path=args.profile,
            html_report_path=args.html_report,
        )
    except FileNotFoundError as exc:
        print(f"[headless] error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"[headless] error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
