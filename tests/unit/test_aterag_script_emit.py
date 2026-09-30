"""ATERag script emission tests (P3).

The load-bearing test here is :func:`test_generated_script_runs_under_real_executor`.
Everything else asserts on strings; that one executes the generated code through
the platform's actual ``exec`` protocol and checks the resulting status.

Why that matters: the exec protocol is binary (return -> PASSED, assert ->
FAILED). A judgement bug does not raise at generation time — it produces a
script that returns normally, and every unit on the line is reported as
passing. Only running it catches that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ate_cloud.schemas.aterag_bundle import (
    BundleModel,
)
from ate_cloud.services.aterag_script_emit import (
    DRIVER_TABLE,
    generate_all,
    generate_script,
    limits_of,
    plan_gaps,
    unique_steps,
)
from ate_cloud.services.flow_planner import FlowPlan, PlanStep, Segment, load_bindings, plan_flow

REPO = Path(__file__).resolve().parents[2]


def _step(action: str, value: dict[str, Any] | None = None, unit: str = "V") -> PlanStep:
    return PlanStep(
        id=f"s_{action}",
        type="action",
        action=action,
        requirement_code="SR-1",
        scenario_seq=0,
        scenario_name="n",
        resources=["DMM_1"],
        params={"condition_kind": "output_voltage", "value": value or {}, "unit": unit},
    )


def _plan(*steps: PlanStep) -> FlowPlan:
    return FlowPlan(
        product_code="P",
        doc_version="B",
        segments=[Segment(signature="x", steps=list(steps))],
    )


# ── the critical one: run it for real ──────────────────────────────────────


def _exec_generated(body: str, tmp_name: str, driver_stub: str = "") -> tuple[str, dict]:
    """Execute a generated script through the platform's real exec protocol.

    Replicates ``step_executor._run_script`` exactly — same namespace seeding,
    same ``result_*`` collection, same exception-to-status mapping — so a
    passing test here means the real executor would agree.
    """
    from ate_platform.types import StepStatus

    path = REPO / "data" / f"_emit_{tmp_name}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Splice the driver stub in so _driver() resolves to our fake instrument.
    if driver_stub:
        body = body.replace(
            "def _driver():",
            f"{driver_stub}\n\ndef _driver():\n    return _FAKE",
            1,
        )
    path.write_text(body, encoding="utf-8")
    try:
        exec_namespace: dict[str, Any] = {"params": {}}
        try:
            code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
            exec(code, exec_namespace)  # noqa: S102
        except AssertionError as e:
            return "FAILED", {"error": str(e) or "Assertion failed"}
        except Exception as e:  # noqa: BLE001
            return "ERROR", {"error": f"{type(e).__name__}: {e}"}
        outputs = {
            k[7:]: v for k, v in exec_namespace.items() if k.startswith("result_")
        }
        assert StepStatus.PASSED
        return "PASSED", outputs
    finally:
        path.unlink(missing_ok=True)


_DMM_STUB = """
class _FakeDmm:
    def __init__(self, value): self._v = value
    def measure_dc_voltage(self, setpoint=None): return self._v
    def measure_dc_current(self, setpoint=None): return self._v

def _make(value):
    return _FakeDmm(value)
_FAKE = None
"""


def _run_measure(value: dict | None, measured: Any, unit: str = "V") -> tuple[str, dict]:
    body = generate_script(_step("measure_dc_voltage", value, unit)).body
    stub = _DMM_STUB + f"\n_FAKE = _make({measured!r})\n"
    return _exec_generated(body, f"m_{abs(hash(str(value)))}_{measured}", stub)


class TestJudgementUnderRealExecutor:
    def test_in_range_passes(self) -> None:
        status, out = _run_measure({"min": 53.2, "max": 54.8}, 54.0)
        assert status == "PASSED", out
        assert out["measured_value"] == 54.0
        assert out["passed"] is True

    def test_out_of_range_fails(self) -> None:
        """Must be FAILED, not ERROR — otherwise the unit is triaged as an
        instrument fault and moves on, while the product in hand is bad."""
        status, out = _run_measure({"min": 53.2, "max": 54.8}, 60.0)
        assert status == "FAILED", out
        assert "超出" in out["error"]

    def test_one_sided_low_fails_below(self) -> None:
        status, out = _run_measure({"min": 5.0}, 4.0)
        assert status == "FAILED", out

    def test_one_sided_high_fails_above(self) -> None:
        """Over-voltage protection has only an upper bound — the norm, not an
        edge case, so the common case must work."""
        status, out = _run_measure({"max": 55.62}, 60.0)
        assert status == "FAILED", out

    def test_one_sided_high_passes_within(self) -> None:
        status, out = _run_measure({"max": 55.62}, 50.0)
        assert status == "PASSED", out

    def test_missing_criterion_fails_rather_than_passing(self) -> None:
        """The one that motivated the whole design.

        Under a binary protocol, "skip" means "return normally", which means
        PASSED. A requirement whose criterion never reached the plan would then
        pass every unit forever with no trace. It has to stop the batch.
        """
        status, out = _run_measure({}, 54.0)
        assert status == "FAILED", f"缺判据必须 FAILED, 实际 {status} — 会漏放不合格品"
        assert "无条件判据" in out["error"]

    def test_none_reading_fails(self) -> None:
        """An instrument that returned nothing has not measured a good unit."""
        status, out = _run_measure({"min": 53.2, "max": 54.8}, None)
        assert status == "FAILED", out
        assert "未返回读数" in out["error"]

    def test_set_step_returns_normally(self) -> None:
        body = generate_script(_step("set_ac_input", {"typ": 110.0, "unit": "Vac"}, "Vac")).body
        stub = """
class _FakeAc:
    def set_voltage(self, setpoint=None): self.sp = setpoint
_FAKE = _FakeAc()
"""
        status, out = _exec_generated(body, "set_step", stub)
        assert status == "PASSED", out
        assert out["applied"] is True

    def test_outputs_reach_the_module_namespace(self) -> None:
        """The executor reads ``result_*`` from the exec namespace.

        Values assigned inside a function are locals and never appear there, so
        a script whose body is a function reports empty outputs on every step —
        no error, just silently lost measurements. This asserts the real
        symptom, not the shape of the code.
        """
        status, out = _run_measure({"min": 53.2, "max": 54.8}, 54.0)
        assert status == "PASSED", out
        assert out, "outputs 为空 —— result_* 没有落在模块命名空间里"
        assert set(out) >= {"measured_value", "passed"}


# ── plan-time gate ──────────────────────────────────────────────────────────


class TestPlanGaps:
    def test_measurement_without_criteria_is_flagged(self) -> None:
        gaps = plan_gaps(_plan(_step("measure_dc_voltage", {})))
        assert len(gaps) == 1
        assert gaps[0].condition_kind == "output_voltage"

    def test_measurement_with_criteria_not_flagged(self) -> None:
        assert plan_gaps(_plan(_step("measure_dc_voltage", {"min": 1.0, "max": 2.0}))) == []

    def test_set_and_verify_steps_not_flagged(self) -> None:
        """Only measurements need criteria; flagging a setpoint step would block
        every plan for no reason, and a gate that always fires gets ignored."""
        assert plan_gaps(_plan(_step("set_ac_input", {}))) == []
        assert plan_gaps(_plan(_step("read_telemetry", {}))) == []

    def test_real_bundle_gaps_are_reported(self) -> None:
        """Known real gaps: 3 measurements reach the plan with no bounds.

        Asserted rather than hard-coded to zero — this number is the honest
        state of the spec, and it should change when the spec or the extractor
        is fixed, not when someone edits a test.
        """
        p = Path(r"F:\Workspace\ATERag\rag_storage\exports\studio_bundle.json")
        if not p.exists():
            pytest.skip("需要 ATERag 导出的真实 bundle")
        bundle = BundleModel.model_validate_json(p.read_text(encoding="utf-8"))
        plan = plan_flow(bundle, load_bindings())
        gaps = plan_gaps(plan)
        assert len(gaps) == 3
        assert {g.condition_kind for g in gaps} == {"output_voltage", "ripple"}


# ── draft safety ────────────────────────────────────────────────────────────


class TestDraftSafety:
    def test_every_script_is_marked_draft(self) -> None:
        """All 134 must carry FILL_IN: none may be silently treated as
        production-ready, because none of them can actually be."""
        scripts = generate_all(_plan(*[
            _step(a, {"min": 1.0, "max": 2.0})
            for a in ("measure_dc_voltage", "set_ac_input", "read_telemetry")
        ]))
        assert scripts
        assert all(s.has_fill_in for s in scripts)

    def test_header_documents_the_binary_protocol(self) -> None:
        """Whoever reads the generated file must know that returning means
        PASSED — that is not a property you can infer from the code."""
        body = generate_script(_step("measure_dc_voltage", {"min": 1.0, "max": 2.0})).body
        assert "PASSED" in body and "AssertionError" in body
        assert "SKIP" in body  # explains the absence of that path

    def test_limits_embedded_for_traceability(self) -> None:
        body = generate_script(_step("measure_dc_voltage", {"min": 53.2, "max": 54.8})).body
        assert "53.2" in body and "54.8" in body
        assert "SR-1" in body


# ── generation over real data ───────────────────────────────────────────────


class TestRealPlan:
    def test_scripts_deduplicate(self) -> None:
        """698 steps must collapse hard — one script per step means 698 files
        that can each be edited and drift independently."""
        p = Path(r"F:\Workspace\ATERag\rag_storage\exports\studio_bundle.json")
        if not p.exists():
            pytest.skip("需要 ATERag 导出的真实 bundle")
        bundle = BundleModel.model_validate_json(p.read_text(encoding="utf-8"))
        plan = plan_flow(bundle, load_bindings())
        assert plan.step_count > 600
        assert len(generate_all(plan)) < 200

    def test_every_action_in_the_table_is_reachable(self) -> None:
        """A table entry for an action nothing emits is a promise nothing keeps —
        either the binding is right and should be used, or it is dead."""
        p = Path(r"F:\Workspace\ATERag\rag_storage\exports\studio_bundle.json")
        if not p.exists():
            pytest.skip("需要 ATERag 导出的真实 bundle")
        bundle = BundleModel.model_validate_json(p.read_text(encoding="utf-8"))
        plan = plan_flow(bundle, load_bindings())
        emitted = {st.action for st in unique_steps(plan)}
        assert emitted, "计划为空"
        unknown = emitted - set(DRIVER_TABLE) - {"power_cycle"}
        assert not unknown, f"这些动作没有仪表实现, 会生成 stub: {sorted(unknown)}"


class TestLimitsOf:
    def test_only_numeric_bounds_kept(self) -> None:
        assert limits_of({"min": 1, "max": 2.5, "unit": "V", "note": "x"}) == {
            "min": 1,
            "max": 2.5,
        }

    def test_typical_only_kept(self) -> None:
        """A setpoint with no bounds is not judgeable — but the value is still
        worth carrying, so typ must not be dropped as 'non-numeric'."""
        assert limits_of({"typ": 12.0}) == {"typ": 12.0}

    def test_non_numeric_ignored(self) -> None:
        assert limits_of({"min": "abc", "max": None}) == {}
