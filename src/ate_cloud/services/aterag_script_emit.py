"""ATERag step-script emission (P3) — turn a flow plan into runnable scripts.

Naming
------
Deliberately *not* ``script_generator``: this repo already has one, and it
generates scripts a different way — an LLM writes them from a prompt, with a
circuit breaker and version history. Mine is a template renderer driven by the
binding table. Two modules with the same name and different contracts is how
someone ends up importing the wrong one, so the distinction is in the name.

The contract this generates against
-----------------------------------
Read from ``ate_platform/executor/step_executor.py``, not assumed:

* The executor ``exec``s the script file with ``params`` in the namespace.
* Outputs are module-level ``result_*`` names (``result_voltage`` → ``voltage``).
* **The protocol is binary**: a script that returns normally is
  ``StepStatus.PASSED``. ``AssertionError`` → ``FAILED``. Anything else →
  ``ERROR``. There is no way for a script to report SKIPPED.

That last fact is the most important one here, and it dictated everything below.

Why a missing criterion must FAIL, not skip
-------------------------------------------
The obvious design — "no limits, so skip it" — silently becomes PASSED under
this protocol, because skipping means not raising. A requirement whose criterion
never made it out of the spec would then be reported as a pass on every unit,
forever, with no trace. That is precisely the failure this whole pipeline exists
to prevent, reintroduced in its last step.

So:

1. criterion present, reading in range → return normally → PASSED
2. criterion present, reading out of range → ``raise AssertionError`` → FAILED
3. **no criterion, or no reading → ``raise AssertionError``** → FAILED, with a
   message naming the requirement, so the batch stops and a human looks

A line that stops on a specification gap is inconvenient. A line that passes
units it never measured is dangerous, and it is invisible.

Detecting this before the run is the real fix, which is why
:func:`plan_gaps` exists — ``push_to_studio.py`` refuses to upload a plan
containing unjudgeable measurements, so the gap surfaces at a desk.

Why templates, not 26 hand-written scripts
------------------------------------------
The scripts differ only in which driver method they call and how they shape the
output. A table of differences cannot drift out of sync with the binding table;
hand-written scripts will, and a drifted measurement script fails *open* on
hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ate_cloud.services.flow_planner import FlowPlan, PlanStep

#: Marker left wherever site-specific wiring is required. Scanning for these is
#: how a draft plan is kept off the line.
FILL_IN = "FILL_IN"

#: (driver kind, method, action nature) per binding action.
#: nature:
#:   measure -> scalar to be judged against criteria
#:   set     -> configures an instrument, no judgement
#:   verify  -> reads a capability/alert state, non-numeric
DRIVER_TABLE: dict[str, tuple[str, str, str]] = {
    "set_ac_input": ("ac_source", "set_voltage", "set"),
    "set_line_frequency": ("ac_source", "set_frequency", "set"),
    "select_input_source": ("relay_matrix", "select_route", "set"),
    "set_electronic_load": ("eload", "set_load", "set"),
    "set_capacitive_load": ("cap_bank", "set_capacitance", "set"),
    "set_load_duty": ("eload", "set_duty", "set"),
    "set_chamber_temperature": ("chamber", "set_temperature", "set"),
    "configure_measurement": ("scope", "configure", "set"),
    "configure_measurement_point": ("dmm", "configure_channel", "set"),
    "configure_pf_measurement": ("power_analyzer", "configure_pf", "set"),
    "issue_command": ("control_if", "send_command", "set"),
    "set_test_mode": ("control_if", "set_mode", "set"),
    "trigger_power_event": ("ac_source", "trigger_event", "set"),
    "inject_fault": ("relay_matrix", "inject_fault", "set"),
    "measure_dc_voltage": ("dmm", "measure_dc_voltage", "measure"),
    "measure_dc_current": ("dmm", "measure_dc_current", "measure"),
    "measure_input_current": ("dmm", "measure_dc_current", "measure"),
    "measure_output_power": ("power_analyzer", "measure_output_power", "measure"),
    "measure_ripple_20mhz": ("scope", "measure_ripple", "measure"),
    "measure_efficiency": ("power_analyzer", "measure_efficiency", "measure"),
    "measure_transient_response": ("scope", "measure_transient", "measure"),
    "read_signal_state": ("control_if", "read_signal", "verify"),
    "read_telemetry": ("control_if", "read_telemetry", "verify"),
    "verify_protection_action": ("control_if", "verify_protection", "verify"),
    "verify_capability_present": ("control_if", "verify_capability", "verify"),
    "verify_cap_load_capability": ("control_if", "verify_capability", "verify"),
    "power_cycle": ("ac_source", "power_cycle", "set"),
}

#: Setpoint keys forwarded to the driver adapter verbatim. Anything else in the
#: condition is context for a human and must not reach an instrument.
SETPOINT_KEYS = ("min", "typ", "max", "unit", "mode", "percent", "event", "value")


@dataclass(slots=True)
class GeneratedScript:
    """One generated step script."""

    name: str
    body: str

    @property
    def has_fill_in(self) -> bool:
        return FILL_IN in self.body


@dataclass(slots=True)
class PlanGap:
    """A measurement the plan cannot judge — blocks upload."""

    requirement_code: str
    condition_kind: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "requirement_code": self.requirement_code,
            "condition_kind": self.condition_kind,
            "reason": self.reason,
        }


def unique_steps(plan: FlowPlan) -> list[PlanStep]:
    """Deduplicate steps into one script per (action, kind, value).

    698 steps collapse to a few dozen: every scenario measuring -54V output
    voltage runs the same measurement. One script per step would mean 698
    near-identical files, each editable and each able to drift.
    """
    seen: dict[str, PlanStep] = {}
    for seg in plan.segments:
        for st in seg.steps:
            key = f"{st.action}|{st.params.get('condition_kind')}|{st.params.get('value')}"
            seen.setdefault(key, st)
    return list(seen.values())


def limits_of(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in ("min", "typ", "max"):
        if isinstance(value.get(k), int | float):
            out[k] = value[k]
    return out


def plan_gaps(plan: FlowPlan) -> list[PlanGap]:
    """Measurements with no usable criterion.

    Blocking: the script would have nothing to judge against, and under the
    exec protocol "nothing to judge" resolves to PASSED. The gate consuming
    this runs before upload, so the gap is found at a desk rather than on a
    line, mid-batch.
    """
    gaps: list[PlanGap] = []
    seen: set[tuple[str, str]] = set()
    for st in unique_steps(plan):
        _, _, nature = DRIVER_TABLE.get(st.action, ("", "", "stub"))
        if nature != "measure":
            continue
        if limits_of(st.params.get("value") or {}):
            continue
        key = (st.requirement_code, str(st.params.get("condition_kind")))
        if key in seen:
            continue
        seen.add(key)
        gaps.append(
            PlanGap(
                requirement_code=st.requirement_code,
                condition_kind=str(st.params.get("condition_kind")),
                reason=(
                    "测量步骤无数值判据, 运行时只会报通过 —— "
                    "上机前须补判据或改为定性确认"
                ),
            )
        )
    return gaps


def generate_script(step: PlanStep) -> GeneratedScript:
    """Render one step's script."""
    driver, method, nature = DRIVER_TABLE.get(step.action, ("stub", "noop", "stub"))
    value = step.params.get("value") or {}
    return GeneratedScript(
        name=f"{step.action}.py",
        body=_render(step, driver, method, nature, value),
    )


def generate_all(plan: FlowPlan) -> list[GeneratedScript]:
    """Generate every distinct script the plan needs."""
    return [generate_script(st) for st in unique_steps(plan)]


def _setpoint_expr(step: PlanStep, value: dict[str, Any]) -> str:
    payload: dict[str, Any] = {k: value[k] for k in SETPOINT_KEYS if value.get(k) is not None}
    if step.params.get("rail"):
        payload["rail"] = step.params["rail"]
    if step.params.get("scenario_bindings"):
        payload["scenario"] = step.params["scenario_bindings"]
    if step.method_ref:
        payload["method_ref"] = step.method_ref
    return repr(payload)


def _render(
    step: PlanStep, driver: str, method: str, nature: str, value: dict[str, Any]
) -> str:
    ck = str(step.params.get("condition_kind", ""))
    unit = str(step.params.get("unit", ""))
    limits = limits_of(value)
    resource = (step.resources or ["UNKNOWN"])[0]
    origin = f"{step.requirement_code} / 场景 {step.scenario_seq} / {step.scenario_name}"

    if nature == "measure":
        body_call = (
            f"result_measured = _drv.{method}(setpoint=_setpoint)\n"
            f"_judge(result_measured)\n"
        )
    elif nature == "verify":
        body_call = (
            f"result_state = _drv.{method}(setpoint=_setpoint)\n"
            f"result_present = True\n"
        )
    elif nature == "set":
        body_call = f"_drv.{method}(setpoint=_setpoint)\nresult_applied = True\n"
    else:
        body_call = (
            f"raise AssertionError(\n"
            f'    "{FILL_IN}: {step.action} 尚未绑定仪表实现 "\n'
            f'    "(预期 {driver}.{method})。补 config/test_method_bindings.yaml "\n'
            f'    "并补本站仪表接线后重新生成。"\n'
            f")\n"
        )

    judge = _judge_fn(limits, unit) if nature == "measure" else ""

    return f'''"""由 ATERag 流程规划器生成 —— 草稿, 上机前须补齐 {FILL_IN} 标记。

动作      : {step.action}
条件种类  : {ck}
来源      : {origin}
仪表资源  : {resource}   (须与夹具拓扑一致)
量纲      : {unit or "(无量纲)"}
判据      : {_describe_limits(limits) or "无(仅设置/确认, 不做数值判定)"}

不要手改本文件 —— 改 config/test_method_bindings.yaml 后重新生成。
手改的副本会在下次生成时被覆盖, 且与绑定表失去同步。

执行协议 (据 ate_platform/executor/step_executor.py):
  * 正常返回              -> PASSED
  * raise AssertionError  -> FAILED
  * 其它异常              -> ERROR
注意这个协议**没有** SKIP 通道: 想"不判定"只能不抛异常, 而那等于 PASSED。
所以缺判据时本脚本抛 AssertionError(FAILED), 而不是安静跳过。
"""

#: 本步骤占用的仪表资源, 与 test_method_bindings.yaml 一致。
RESOURCES = {list(step.resources)!r}

#: 规格书抽取出的判据。单边限值是常态(过压只有上限), 故 min/max 均可为 None,
#: 含义是"这一侧不要求"而非"忘了填"。
LIMITS = {limits!r}

#: 判据来源, 便于从不合格品记录反查规格书条款。
ORIGIN = {origin!r}

_setpoint = {_setpoint_expr(step, value)}


def _driver():
    """取本站仪表实例。

    真实接线与初始化依赖现场环境, 故留 {FILL_IN}。DriverRegistry.get_driver
    返回的是驱动**类**, 实例化与 connect 由站点代码负责。
    """
    # {FILL_IN}: 补上仪器句柄的获取(见 ate_platform.drivers.base.DriverRegistry)
    raise NotImplementedError(
        "{FILL_IN}: 未接线。DriverRegistry.get_driver({driver!r}) 返回驱动类, "
        "请在此实例化并 connect, 再返回实例。"
    )


{judge}
# ── 执行体在模块级, 不在函数内 ──────────────────────────────────────────
# executor 收集的是 exec() 的模块命名空间里的 result_* 变量。若把执行体放进
# 函数, 那些变量是函数局部变量, 永远不会出现在命名空间里, 于是每一步都会
# 返回空 outputs —— 不报错, 只是结果全丢。这一点只有真跑一遍才会发现。
_drv = _driver()
{body_call}
if __name__ == "__main__":
    pass
'''


def _judge_fn(limits: dict[str, Any], unit: str) -> str:
    return f'''
def _judge(measured):
    """按 LIMITS 判定。返回即通过, 不通过抛 AssertionError -> FAILED。

    global 声明不可省: result_* 必须在模块命名空间里, executor 从那里读
    输出。少了它, 判定结果就传不回上层。

    两侧皆空 -> 抛错而非放过: 规格书没给判据的测量, 放过等于每台都报通过。
    读数为 None 同样抛错: 仪表没读到东西不等于产品合格。
    """
    global result_measured_value, result_passed

    lo, hi = LIMITS.get("min"), LIMITS.get("max")
    if lo is None and hi is None:
        raise AssertionError(
            f"无条件判据, 无法判定: {{ORIGIN}} / {{LIMITS}}。"
            f" 该测量被排入序列但没有上下限 —— 请补判据或改为定性确认, "
            f"不得默认通过。"
        )
    if measured is None:
        raise AssertionError(f"仪表未返回读数, 无法判定: {{ORIGIN}}")

    out_of_range = (lo is not None and measured < lo) or (hi is not None and measured > hi)
    if out_of_range:
        raise AssertionError(
            f"实测 {{measured}}{{{unit!r}}} 超出 [{{lo}}, {{hi}}]: {{ORIGIN}}"
        )

    result_measured_value = measured
    result_passed = True
    return True
'''


def _describe_limits(limits: dict[str, Any]) -> str:
    if not limits:
        return ""
    lo, hi = limits.get("min"), limits.get("max")
    if lo is not None and hi is not None:
        return f"{lo} ~ {hi}"
    if hi is not None:
        return f"<= {hi} (只有上限)"
    if lo is not None:
        return f">= {lo} (只有下限)"
    if "typ" in limits:
        return f"典型值 {limits['typ']} (无上下限)"
    return ""
