"""Task 21 — headless runner breakpoint policy.

Unattended/default headless runs must NOT suspend on a DSL
``type: breakpoint`` step: the :class:`V32PlanDispatcher` is constructed
with ``breakpoint_hook=None`` so breakpoints are no-op pass-throughs and the
run completes (exit 0).

``--allow-breakpoints`` is the explicit opt-in for attended/interactive use:
when set, a breakpoint hook (the pause gate) IS armed on the dispatcher.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ate_platform.simulation import headless_runner
from ate_platform.simulation.headless_runner import run_headless


def _write_breakpoint_plan(tmp_path: Path) -> Path:
    """v3.2 plan containing an unconditional ``type: breakpoint`` step."""
    path = tmp_path / "plan_with_breakpoint.yaml"
    path.write_text(
        """\
name: bp_plan
version: "3.2"
scope: production
steps:
  - id: bp_here
    type: breakpoint
  - id: after_bp
    type: action
    script: dmm_measure.py
    params: { expected_value: 12.0 }
    depends_on: [bp_here]
""",
        encoding="utf-8",
    )
    return path


def _write_plain_plan(tmp_path: Path) -> Path:
    """v3.2 plan without any breakpoint step (used to inspect wiring)."""
    path = tmp_path / "plan_plain.yaml"
    path.write_text(
        """\
name: plain_plan
version: "3.2"
scope: production
steps:
  - id: power_on
    type: action
    script: dmm_measure.py
    params: { expected_value: 12.0 }
""",
        encoding="utf-8",
    )
    return path


class TestDefaultBreakpointsNoOp:
    def test_default_run_with_breakpoint_completes_without_suspending(
        self, tmp_path: Path
    ) -> None:
        """Given a plan with an unconditional breakpoint, the default v32 run
        completes (exit 0) and reports pass-through rather than suspension."""
        plan = _write_breakpoint_plan(tmp_path)
        code = run_headless(plan_path=plan, tier="v32")
        assert code == 0

    def test_default_wires_no_breakpoint_hook(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given the default (no flag), the dispatcher gets breakpoint_hook=None."""
        import ate_platform.executor.v32_dispatcher as disp_mod

        captured: dict[str, Any] = {}
        orig = disp_mod.V32PlanDispatcher

        def spy(plan_arg: Any, *args: Any, **kwargs: Any) -> Any:
            captured["breakpoint_hook"] = kwargs.get("breakpoint_hook")
            return orig(plan_arg, *args, **kwargs)

        monkeypatch.setattr(disp_mod, "V32PlanDispatcher", spy)
        code = run_headless(plan_path=_write_plain_plan(tmp_path), tier="v32")
        assert code == 0
        assert captured["breakpoint_hook"] is None

    def test_cli_default_does_not_pass_allow_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given no --allow-breakpoints on the CLI, run_headless sees False."""
        import ate_platform.simulation.headless_runner as hr

        seen: dict[str, Any] = {}
        orig = hr.run_headless

        def spy(**kwargs: Any) -> int:
            seen["allow_breakpoints"] = kwargs.get("allow_breakpoints")
            return orig(**kwargs)

        monkeypatch.setattr(hr, "run_headless", spy)
        code = headless_runner.main([str(_write_plain_plan(tmp_path)), "--tier", "v32"])
        assert code == 0
        assert seen["allow_breakpoints"] is False


class TestAllowBreakpointsOptIn:
    def test_flag_arms_breakpoint_hook(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given --allow-breakpoints, the dispatcher receives a non-None hook
        (the pause gate exists in attended mode)."""
        import ate_platform.executor.v32_dispatcher as disp_mod

        captured: dict[str, Any] = {}
        orig = disp_mod.V32PlanDispatcher

        def spy(plan_arg: Any, *args: Any, **kwargs: Any) -> Any:
            captured["breakpoint_hook"] = kwargs.get("breakpoint_hook")
            return orig(plan_arg, *args, **kwargs)

        monkeypatch.setattr(disp_mod, "V32PlanDispatcher", spy)
        code = run_headless(
            plan_path=_write_plain_plan(tmp_path),
            tier="v32",
            allow_breakpoints=True,
        )
        assert code == 0
        assert callable(captured["breakpoint_hook"])

    def test_cli_flag_forwards_allow_breakpoints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given --allow-breakpoints on the CLI, run_headless sees True."""
        import ate_platform.simulation.headless_runner as hr

        seen: dict[str, Any] = {}
        orig = hr.run_headless

        def spy(**kwargs: Any) -> int:
            seen["allow_breakpoints"] = kwargs.get("allow_breakpoints")
            return orig(**kwargs)

        monkeypatch.setattr(hr, "run_headless", spy)
        code = headless_runner.main(
            [str(_write_plain_plan(tmp_path)), "--tier", "v32", "--allow-breakpoints"],
        )
        assert code == 0
        assert seen["allow_breakpoints"] is True

    def test_help_documents_flag(self) -> None:
        """--help mentions the breakpoint opt-in flag."""
        import contextlib
        import io

        buf = io.StringIO()
        with pytest.raises(SystemExit), contextlib.redirect_stdout(buf):
            headless_runner.main(["--help"])
        assert "--allow-breakpoints" in buf.getvalue()
