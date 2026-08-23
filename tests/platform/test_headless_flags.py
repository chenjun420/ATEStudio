"""Tests for headless_runner CLI flags (T15, 设计文档 §7.10 CI contract).

Covers the four §7.10 flags on top of the existing --tier/--fault-config
surface:
- ``--uut-count N``   overrides plan.uut_count (drives v32 UUTManager size)
- ``--profile PATH``  loads a simulation profile YAML (noise config for full)
- ``--html-report P`` writes a self-contained HTML summary (stdlib only)
- ``--junit-output``  canonical alias of the legacy ``--junit`` flag

Exit-code contract preserved: 0 = all passed, 1 = step failures,
2 = usage/input errors (bad profile path, malformed profile, uut-count < 1).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from ate_platform.simulation import headless_runner
from ate_platform.simulation.headless_runner import run_headless

REPO_FIXTURE = Path("tests/fixtures/plan_v32_production.yaml")


def _write_plan(tmp_path: Path, uut_count: int = 1) -> Path:
    """Minimal v3.2 plan with an explicit plan-level uut_count."""
    path = tmp_path / "plan.yaml"
    path.write_text(
        f"""\
name: flags_plan
version: "3.2"
scope: production
uut_count: {uut_count}
steps:
  - id: fixture_clamp
    type: fixture_control
    action: clamp
    fixture_id: "fx1"
  - id: power_on
    type: action
    script: dmm_measure.py
    params: {{ expected_value: 12.0 }}
    depends_on: [fixture_clamp]
  - id: sync_power_on
    type: barrier
    barrier_name: "all_powered_on"
    depends_on: [power_on]
""",
        encoding="utf-8",
    )
    return path


def _write_profile(
    tmp_path: Path,
    body: str = "noise_model: NONE\nnoise_sigma: 0.002\nseed: 7\n",
) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# --junit-output canonical alias of --junit
# ---------------------------------------------------------------------------


class TestJunitOutputAlias:
    def test_junit_output_flag_writes_wellformed_xml(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plan = _write_plan(tmp_path)
        out = tmp_path / "out.xml"
        code = headless_runner.main(
            [str(plan), "--tier", "dry_run", "--junit-output", str(out)],
        )
        assert code == 0
        assert out.exists()
        root = ET.parse(out).getroot()  # well-formed XML
        assert root.tag == "testsuites"
        suite = root.find("testsuite")
        assert suite is not None
        assert suite.get("failures") == "0"
        assert len(suite.findall("testcase")) == 3
        # reports are not dumped to stdout — only status lines
        captured = capsys.readouterr().out
        assert "<testsuite" not in captured

    def test_legacy_junit_flag_still_works(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plan = _write_plan(tmp_path)
        out = tmp_path / "legacy.xml"
        code = headless_runner.main([str(plan), "--tier", "dry_run", "--junit", str(out)])
        assert code == 0
        assert out.exists()
        assert ET.parse(out).getroot().tag == "testsuites"


# ---------------------------------------------------------------------------
# --uut-count overrides plan.uut_count
# ---------------------------------------------------------------------------


class TestUutCountFlag:
    def test_uut_count_overrides_plan_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI flag wins over the YAML's own uut_count (drives UUTManager)."""
        plan = _write_plan(tmp_path, uut_count=1)
        captured: dict[str, Any] = {}

        import ate_platform.executor.v32_dispatcher as disp_mod

        orig = disp_mod.V32PlanDispatcher

        def spy(plan_arg: Any, *args: Any, **kwargs: Any) -> Any:
            captured["uut_count"] = plan_arg.uut_count
            return orig(plan_arg, *args, **kwargs)

        monkeypatch.setattr(disp_mod, "V32PlanDispatcher", spy)
        code = run_headless(plan_path=plan, tier="v32", uut_count=4)
        assert code == 0
        assert captured["uut_count"] == 4

    def test_uut_count_zero_rejected_with_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plan = _write_plan(tmp_path)
        code = headless_runner.main(
            [str(plan), "--tier", "dry_run", "--uut-count", "0"],
        )
        assert code == 2
        assert "uut" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# --profile simulation profile YAML
# ---------------------------------------------------------------------------


class TestProfileFlag:
    def test_profile_happy_path_full_tier(self, tmp_path: Path) -> None:
        plan = _write_plan(tmp_path)
        profile = _write_profile(tmp_path)
        code = run_headless(plan_path=plan, tier="full", profile_path=profile)
        assert code == 0

    def test_missing_profile_path_exits_nonzero_with_clear_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plan = _write_plan(tmp_path)
        missing = tmp_path / "nope.yaml"
        code = headless_runner.main(
            [str(plan), "--tier", "dry_run", "--profile", str(missing)],
        )
        assert code != 0
        err = capsys.readouterr().err
        assert "profile" in err.lower()

    def test_malformed_profile_content_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plan = _write_plan(tmp_path)
        bad = _write_profile(tmp_path, body="- just\n- a\n- list\n")
        code = headless_runner.main(
            [str(plan), "--tier", "dry_run", "--profile", str(bad)],
        )
        assert code == 2
        assert "profile" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# --html-report self-contained HTML summary
# ---------------------------------------------------------------------------


class TestHtmlReportFlag:
    def test_html_report_contains_step_rows_and_summary(
        self, tmp_path: Path
    ) -> None:
        plan = _write_plan(tmp_path)
        html_out = tmp_path / "report.html"
        code = run_headless(
            plan_path=plan, tier="dry_run", html_report_path=html_out,
        )
        assert code == 0
        content = html_out.read_text(encoding="utf-8")
        assert "<table" in content
        for step_id in ("fixture_clamp", "power_on", "sync_power_on"):
            assert step_id in content
        assert "PASS" in content
        assert "3" in content  # totals somewhere in the summary
        assert "duration" in content.lower()

    def test_html_report_is_self_contained_inline_css(
        self, tmp_path: Path
    ) -> None:
        plan = _write_plan(tmp_path)
        html_out = tmp_path / "report.html"
        code = run_headless(
            plan_path=plan, tier="dry_run", html_report_path=html_out,
        )
        assert code == 0
        content = html_out.read_text(encoding="utf-8")
        assert "<style" in content  # inline CSS
        assert "<link" not in content  # no external stylesheets
        assert "src=" not in content  # no external scripts/images

    def test_html_report_includes_coverage_section(self, tmp_path: Path) -> None:
        """§7.10 quality gate: coverage stats enrich the HTML report."""
        plan = _write_plan(tmp_path)
        html_out = tmp_path / "report.html"
        code = run_headless(
            plan_path=plan, tier="dry_run", html_report_path=html_out,
        )
        assert code == 0
        content = html_out.read_text(encoding="utf-8")
        assert "Coverage" in content
        assert "100" in content  # all steps executed → 100%


# ---------------------------------------------------------------------------
# Combined acceptance scenario + exit-code preservation
# ---------------------------------------------------------------------------


class TestCombinedAndExitCodes:
    def test_all_flags_combined_exit_zero_and_write_artifacts(
        self, tmp_path: Path
    ) -> None:
        """The §7.10 acceptance invocation against the repo e2e fixture."""
        junit_out = tmp_path / "out.xml"
        html_out = tmp_path / "out.html"
        profile = _write_profile(tmp_path)
        code = headless_runner.main(
            [
                str(REPO_FIXTURE),
                "--tier", "dry_run",
                "--uut-count", "2",
                "--profile", str(profile),
                "--junit-output", str(junit_out),
                "--html-report", str(html_out),
            ],
        )
        assert code == 0
        assert junit_out.exists()
        assert html_out.exists()
        assert ET.parse(junit_out).getroot().tag == "testsuites"

    def test_failure_exit_code_preserved_with_new_flags(
        self, tmp_path: Path
    ) -> None:
        plan = tmp_path / "fail.yaml"
        plan.write_text(
            """\
name: flags_fail
version: "1.0"
scope: production
steps:
  - id: impossible
    script: dmm_measure.py
    preconditions: [never_runs]
""",
            encoding="utf-8",
        )
        junit_out = tmp_path / "fail.xml"
        html_out = tmp_path / "fail.html"
        code = headless_runner.main(
            [
                str(plan),
                "--tier", "dry_run",
                "--junit-output", str(junit_out),
                "--html-report", str(html_out),
            ],
        )
        assert code == 1
        assert junit_out.exists()
        assert html_out.exists()
