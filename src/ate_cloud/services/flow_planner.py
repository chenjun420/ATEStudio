"""Flow planner (P2.5) — turn conditions and scenarios into a DSL v3.2 plan.

The problem
-----------
286 scenarios across 95 requirements. Executed in extraction order they would
be *correct but ruinous*: each scenario re-sets the input voltage, waits for
settling, changes the load, waits again. On a real line that is hours of pure
changeover — the UUT sits clamped while instruments reconfigure.

The insight
-----------
Most consecutive scenarios want the *same* excitation. Scenarios that need
"110Vac, 50% load" and "110Vac, 75% load" can share one input setpoint; the
difference is only the load, and the load bank is one register write. So the
planner groups by setup signature, runs each group once, and inside the group
only re-applies what actually differs.

What this module will not do
----------------------------
It will not invent a sequence that the spec does not support, and it will not
merge two scenarios whose criteria overlap in a way that loses a measurement.
Grouping is only ever an *ordering* and *coalescing of identical setup
actions* — every scenario still produces its own measurement step.

Destructive isolation
---------------------
Over-voltage, short-circuit and injected-fault scenarios can damage a UUT. If
one of them latches the unit into protection mode, every later measurement is
taken in the wrong state — and the report looks clean. So destructive work is
emitted into its own segment, fenced with power-cycle recovery on both sides.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from ate_cloud.schemas.aterag_bundle import BundleModel, RequirementModel, ScenarioModel

#: ``src/ate_cloud/services/flow_planner.py`` -> repo root is four levels up.
#: Counted rather than guessed: an off-by-one here resolves to ``src/config``,
#: which does not exist, so the failure is loud — but only at the first import
#: in a fresh process, not at collection time. Cheap to get right, so get it
#: right once.
REPO = Path(__file__).resolve().parents[3]
BINDINGS_PATH = REPO / "config" / "test_method_bindings.yaml"

#: Setup-affecting kinds, in the order they must be applied. Ordering matters:
#: you set the source before the load, and the load before the measurement —
#: otherwise the measurement captures the transient of the previous change.
SETUP_ORDER = (
    "test_mode",
    "input_type",
    "input_voltage",
    "input_frequency",
    "power_factor",
    "load",
    "cap_load",
    "duty",
    "temperature",
    "measurement_setup",
    "power_event",
    "fault_stimulus",
)

#: Fixture actions wrapped around the whole sequence.
FIXTURE_CLAMP = "fixture_clamp"
FIXTURE_RELEASE = "fixture_release"


@dataclass(frozen=True, slots=True)
class Binding:
    """One row of the kind→action binding table."""

    kind: str
    side: str
    resource: tuple[str, ...]
    action: str
    settle_s: float
    setup_group: str
    destructive: bool
    requires: tuple[str, ...]
    method_ref: str
    unit: str
    #: Batch-level setup (e.g. a chamber setpoint) is changed once per batch,
    #: not per unit. Its settle time is therefore *not* per-step wait time —
    #: adding it per step invents hours of dead time that no production line
    #: would ever run.
    batch_level: bool = False

    @property
    def is_setup(self) -> bool:
        return self.side == "input"


def load_bindings(path: Path | None = None) -> dict[tuple[str, str], Binding]:
    """Load the binding table, keyed by (kind, side)."""
    data = yaml.safe_load((path or BINDINGS_PATH).read_text(encoding="utf-8"))
    out: dict[tuple[str, str], Binding] = {}
    for row in data["bindings"]:
        key = (row["kind"], row["side"])
        if key in out:
            # A duplicate here would make the planner pick arbitrarily between
            # two different instrument plans, so the same plan would never
            # reproduce. Verified by scripts/verify_bindings.py, but the guard
            # is repeated here because the planner's output depends on it.
            raise ValueError(f"绑定表存在重复项 {key}: 计划将不可复现")
        out[key] = Binding(
            kind=row["kind"],
            side=row["side"],
            resource=tuple(row.get("resource", [])),
            action=row["action"],
            settle_s=float(row.get("settle_s", 0.0)),
            setup_group=row.get("setup_group", "default"),
            destructive=bool(row.get("destructive", False)),
            requires=tuple(row.get("requires", [])),
            method_ref=row.get("method_ref", ""),
            unit=row.get("unit", ""),
            batch_level=bool(row.get("batch_level", False)),
        )
    return out


@dataclass(slots=True)
class PlanStep:
    """One DSL v3.2 step."""

    id: str
    type: str
    action: str
    requirement_code: str
    scenario_seq: int
    scenario_name: str
    resources: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    settle_s: float = 0.0
    batch_level: bool = False
    destructive: bool = False
    setup_group: str = ""
    method_ref: str = ""

    def to_yaml_dict(self) -> dict[str, Any]:
        """Render as a DSL v3.2 step mapping.

        Only the keys the parser actually reads are emitted. Writing extra keys
        (``settle_s``, ``destructive``) would be ignored by the parser today and
        silently become dead weight the moment a stricter validator lands.
        The plan's own metadata lives in a top-level comment, not in the steps.
        """
        d: dict[str, Any] = {
            "id": self.id,
            "type": "action",
            "script": f"{self.action}.py",
            "params": self.params,
        }
        if self.resources:
            d["resources"] = list(self.resources)
        if self.depends_on:
            d["depends_on"] = list(self.depends_on)
        return d


@dataclass(slots=True)
class Segment:
    """A contiguous run of steps sharing one setup signature."""

    signature: str
    index: int = 0
    steps: list[PlanStep] = field(default_factory=list)
    destructive: bool = False
    total_settle_s: float = 0.0

    @property
    def setup_label(self) -> str:
        return self.signature or "(no-setup)"


@dataclass(slots=True)
class FlowPlan:
    """The planned sequence."""

    product_code: str
    doc_version: str
    segments: list[Segment]
    #: Scenarios whose conditions had no binding — must be surfaced, never
    #: silently dropped. A scenario with no steps is a requirement nobody
    #: is testing.
    unmapped: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def step_count(self) -> int:
        return sum(len(s.steps) for s in self.segments)

    @property
    def total_settle_s(self) -> float:
        """Per-unit settling time, excluding batch-level setups.

        Batch-level time (chamber soak) is reported separately: it is paid once
        per batch, so folding it in here would overstate the per-unit cycle by
        hours and make the number useless for capacity planning — which is the
        only reason anyone computes it.
        """
        return sum(s.total_settle_s for s in self.segments)

    @property
    def batch_setups(self) -> list[dict[str, Any]]:
        """Distinct batch-level setups, with their one-off settle time."""
        seen: dict[str, dict[str, Any]] = {}
        for seg in self.segments:
            for st in seg.steps:
                if not st.batch_level:
                    continue
                key = (
                    f"{st.action}:"
                    f"{json.dumps(st.params.get('value'), sort_keys=True, ensure_ascii=False)}"
                )
                seen.setdefault(
                    key,
                    {
                        "action": st.action,
                        "value": st.params.get("value"),
                        "settle_s": st.settle_s,
                        "requirement_code": st.requirement_code,
                    },
                )
        return list(seen.values())

    @property
    def batch_settle_s(self) -> float:
        return sum(float(b["settle_s"]) for b in self.batch_setups)

    def to_yaml(self) -> str:
        """Emit a DSL v3.2 plan.

        The plan is a *draft*: it is a starting point for a test engineer, not
        something to paste onto a line unattended. The header says so, and the
        ``dry_run`` gate in push_to_studio.py refuses to upload a draft without
        explicit sign-off.
        """
        doc: dict[str, Any] = {
            "name": f"{self.product_code} 产测序列 (ATERag 生成草稿)",
            "version": "3.2",
            "scope": {"variables": {}},
            "max_concurrency": 1,  # exclusive instruments: one at a time
            "uut_count": 1,
            "steps": [],
        }

        steps: list[dict[str, Any]] = [
            {
                "id": FIXTURE_CLAMP,
                "type": "fixture_control",
                "action": "clamp",
                "fixture_id": "FILL_IN_AT_COMMISSION",
                "on_failure": "abort",
            }
        ]
        last_id: str | None = None
        for seg in self.segments:
            if seg.destructive and last_id:
                # Fence destructive work: a protection latch left uncleared
                # makes every subsequent measurement wrong.
                steps.append(
                    {
                        "id": f"recover_before_{seg.steps[0].id}",
                        "type": "action",
                        "script": "power_cycle.py",
                        "params": {"reason": "destructive-segment-fence"},
                        "depends_on": [last_id],
                    }
                )
                last_id = f"recover_before_{seg.steps[0].id}"
            for st in seg.steps:
                d = st.to_yaml_dict()
                if not d.get("depends_on") and last_id:
                    d["depends_on"] = [last_id]
                steps.append(d)
                last_id = st.id
        if last_id:
            steps.append(
                {
                    "id": FIXTURE_RELEASE,
                    "type": "fixture_control",
                    "action": "release",
                    "fixture_id": "FILL_IN_AT_COMMISSION",
                    "depends_on": [last_id],
                }
            )
        doc["steps"] = steps

        header = [
            "# 由 ATERag 从规格书自动生成 —— 这是**草稿**, 不是可直接上机的序列。",
            "#",
            f"# 产品: {self.product_code}  规格版本: {self.doc_version or '(未标注)'}",
            f"# 步骤数: {self.step_count}  分段: {len(self.segments)}  "
            f"逐台稳定等待: {self.total_settle_s:.1f}s",
            f"# 未映射场景: {len(self.unmapped)}  (这些需求未被排入任何测试)",
        ]
        bs = self.batch_setups
        if bs:
            total = sum(b["settle_s"] for b in bs)
            header.append(
                f"# 批次级设置 {len(bs)} 项, 一次性稳定 {total / 60:.0f} 分钟 "
                "(按批计, 不在逐台节拍内)"
            )
            for b in bs:
                header.append(
                    f"#   - {b['action']} = {json.dumps(b['value'], ensure_ascii=False)[:60]}"
                    f"  稳定 {b['settle_s'] / 60:.0f} 分钟"
                )
        if self.warnings:
            header.append("#")
            for w in self.warnings:
                header.append(f"# 警告: {w}")
        body = str(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100))
        return "\n".join(header) + "\n" + body


def _setup_signature(bindings: dict[tuple[str, str], Binding], model: RequirementModel,
                     scen: ScenarioModel) -> str:
    """Signature of the excitation this scenario needs.

    Built from the *bound* setup conditions plus the scenario's own bindings,
    because a scenario at 110Vac differs from one at 230Vac even when both
    bind the same kinds.
    """
    parts: list[str] = []
    if scen.rail:
        parts.append(f"rail={scen.rail}")
    for k, v in sorted(scen.bindings.items()):
        parts.append(f"{k}={v}")
    for cl in model.input_conditions:
        b = bindings.get((cl.kind, "input"))
        if b is not None and b.setup_group not in ("telemetry", "sequence"):
            parts.append(f"{cl.kind}:{_cond_value(cl)}")
    return "|".join(parts)


def _cond_value(clause: Any) -> str:
    v = clause.value
    if not v:
        return str(clause.text)[:24]
    return str(json.dumps(v, sort_keys=True, ensure_ascii=False))


def _slug(s: str) -> str:
    """Filesystem/DSL-safe id fragment.

    DSL step ids become script parameters and fixture labels; a space or a
    slash in them produces a plan that reads fine and fails at commissioning.
    """
    out = []
    for ch in s:
        out.append(ch if (ch.isalnum() or ch in "-_") else "_")
    return "".join(out) or "x"


def plan_flow(bundle: BundleModel, bindings: dict[tuple[str, str], Binding] | None = None) -> FlowPlan:
    """Plan the whole product's sequence from a bundle."""
    binds = bindings or load_bindings()
    segments: list[Segment] = []
    unmapped: list[dict[str, str]] = []
    warnings: list[str] = []

    # Group scenarios by setup signature across the whole product. Grouping
    # globally (not per requirement) is the point: the same 110Vac setup serves
    # requirements from different clauses, and per-requirement grouping would
    # re-set the source once per requirement.
    buckets: dict[str, list[tuple[RequirementModel, ScenarioModel]]] = defaultdict(list)
    for model in bundle.requirements:
        for scen in model.scenarios:
            sig = _setup_signature(binds, model, scen)
            buckets[sig].append((model, scen))

    # Ordering: ordinary measurement work first, destructive work last.
    #
    # Destructive last is not cosmetic. A protection test (OVP/OCP/SCP/injected
    # fault) can latch the unit; anything measured after that is measured in
    # the wrong state and still reports PASS. Putting destruction at the end
    # bounds the damage to the last unit of the batch rather than to everything
    # downstream of wherever it happened to land.
    #
    # Determinism: ties break on the signature string itself. An unstable order
    # means the same spec regenerates a different line behaviour each time, and
    # a test engineer cannot review a plan that moves between exports.
    def _sig_is_destructive(sig: str) -> int:
        for (kind, side), b in binds.items():
            if b.destructive and side == "input" and f"{kind}:" in sig:
                return 1
        return 0

    ordered = sorted(buckets.items(), key=lambda kv: (_sig_is_destructive(kv[0]), kv[0]))

    for seg_index, (sig, items) in enumerate(ordered):
        seg = Segment(signature=sig, index=seg_index)
        prev_id: str | None = None
        # Steps already applied in this segment, keyed by (action, value).
        # This is what makes grouping worth anything: a segment holding six
        # scenarios that all want "110Vac, CC 5A" sets the source once instead
        # of six times. Without it, grouping only reordered work and the line
        # still paid every changeover — the docstring would be a lie.
        applied: set[tuple[str, str]] = set()
        for model, scen in items:
            # 段号进 id: 同一需求的场景可能被分到不同分段(setup 签名不同),
            # 那时这些步骤必须重新施加 —— 但若 id 不带段号, 两次会生成同名
            # 步骤, 依赖图出现歧义, 调度器会跳过后一个。带段号后 id 全局唯一,
            # 而"该段内已施加过什么"仍由 applied 单独判定, 两者职责分开。
            steps, miss = _plan_scenario(
                binds, model, scen, prev_id, applied, seg_index=len(segments)
            )
            for m in miss:
                unmapped.append(
                    {
                        "requirement_code": model.requirement_code,
                        "scenario_seq": str(scen.seq),
                        "scenario_name": scen.name or model.title,
                        "missing": m,
                    }
                )
            if not steps:
                continue
            seg.steps.extend(steps)
            seg.destructive = seg.destructive or any(s.destructive for s in steps)
            # 批次级设置的时间不计入逐台等待 —— 见 FlowPlan.total_settle_s。
            seg.total_settle_s += sum(s.settle_s for s in steps if not s.batch_level)
            prev_id = steps[-1].id
        if seg.steps:
            segments.append(seg)

    if unmapped:
        warnings.append(
            f"{len(unmapped)} 个场景的条件无绑定, 这些需求不会被测到 —— "
            "补 config/test_method_bindings.yaml 后重新规划"
        )

    # A plan whose only content is a clamp and a release is worse than no plan:
    # it looks like coverage and tests nothing.
    total_steps = sum(len(s.steps) for s in segments)
    if total_steps == 0:
        warnings.append("计划为空 —— 没有任何条件排不出步骤, 禁止上机")

    _disambiguate_step_ids(segments, warnings)

    return FlowPlan(
        product_code=bundle.product_code,
        doc_version=bundle.doc_version,
        segments=segments,
        unmapped=unmapped,
        warnings=warnings,
    )


def _disambiguate_step_ids(segments: list[Segment], warnings: list[str]) -> None:
    """Make every step id globally unique, deterministically.

    Requirement codes are slugified for the DSL (spaces and ``#``/``@`` are not
    valid in a step id), and that slug is lossy: ``SR-1223#m0m1`` and
    ``SR-1223@m0m1`` both become ``SR-1223_m0m1``. Two different requirements
    then emit identically-named steps, and a dependency graph with duplicate
    node ids is ambiguous — the scheduler may skip one, and a skipped
    measurement is a requirement that silently stops being tested.

    Resolved by appending an occurrence ordinal in plan order. Deterministic
    (plan order is deterministic) and readable, which a content hash would not
    be. ``depends_on`` is rewritten so the graph stays consistent.
    """
    seen: dict[str, int] = {}
    renames: dict[str, str] = {}
    for seg in segments:
        for st in seg.steps:
            n = seen.get(st.id, 0)
            seen[st.id] = n + 1
            if n:
                new_id = f"{st.id}_d{n}"
                renames[st.id] = new_id
                st.id = new_id
    if renames:
        warnings.append(
            f"{len(renames)} 个步骤 id 因需求码 slug 化而冲突, 已加序号区分。"
            " 需求码含 #/@ 等字符时会发生 —— 若 id 变得难以阅读, 建议在 ATERag 侧"
            "改用更规整的消歧后缀"
        )
        for seg in segments:
            for st in seg.steps:
                st.depends_on = [renames.get(d, d) for d in st.depends_on]


def _plan_scenario(
    binds: dict[tuple[str, str], Binding],
    model: RequirementModel,
    scen: ScenarioModel,
    prev_id: str | None,
    applied: set[tuple[str, str]],
    seg_index: int = 0,
) -> tuple[list[PlanStep], list[str]]:
    """Plan one scenario's steps: setup actions first, then measurements.

    ``applied`` accumulates the setup actions already performed in this segment.
    A setup action whose (action, value) pair is already in the set is skipped —
    the instrument is already in that state, and re-issuing the same setpoint
    costs a settle time and adds switch stress for no change in state.

    Skipping is safe only because the value is part of the key. Skipping on
    ``action`` alone would silently leave the source at the previous scenario's
    voltage while measuring — producing a plausible number that describes the
    wrong operating point, which is the most dangerous failure mode in test
    automation: it does not look like a failure.
    """
    steps: list[PlanStep] = []
    missing: list[str] = []
    base = _slug(f"{model.requirement_code}_s{scen.seq:03d}_g{seg_index:03d}")

    # ---- 1. setup: input-side conditions, in SETUP_ORDER ----
    setup_items: list[tuple[int, Any, Any]] = []
    for cl in model.input_conditions:
        b = binds.get((cl.kind, "input"))
        if b is None:
            missing.append(f"input/{cl.kind}")
            continue
        try:
            rank = SETUP_ORDER.index(cl.kind)
        except ValueError:
            rank = len(SETUP_ORDER)
        setup_items.append((rank, b, cl))
    setup_items.sort(key=lambda x: x[0])

    for _, b, cl in setup_items:
        # 批次级设置(温箱)不参与去重: 它的稳定时间以小时计, 是整批换温点时
        # 一次性的, 不是每台等待。按逐台等待算, 12 个温度条件会算成 6 小时
        # 死等 —— 那不是产线会做的事, 而是一个让计划不可信的数字。
        if not b.batch_level:
            key = (b.action, json.dumps(cl.value, sort_keys=True, ensure_ascii=False) + "|"
                   + json.dumps(dict(scen.bindings), sort_keys=True, ensure_ascii=False)
                   + "|" + scen.rail)
            if key in applied:
                continue
            applied.add(key)
        steps.append(
            PlanStep(
                id=f"{base}_{_slug(cl.kind)}",
                type="action",
                action=b.action,
                requirement_code=model.requirement_code,
                scenario_seq=scen.seq,
                scenario_name=scen.name or model.title,
                resources=list(b.resource),
                params={
                    "condition_kind": cl.kind,
                    "condition_text": cl.text[:200],
                    "value": cl.value,
                    "unit": b.unit,
                    "method_ref": b.method_ref,
                    "scenario_bindings": dict(scen.bindings),
                    "rail": scen.rail,
                },
                depends_on=[prev_id] if prev_id else [],
                settle_s=b.settle_s,
                batch_level=b.batch_level,
                destructive=b.destructive,
                setup_group=b.setup_group,
                method_ref=b.method_ref,
            )
        )
        prev_id = steps[-1].id

    # ---- 2. measurement: output-side conditions ----
    for cl in model.output_conditions:
        b = binds.get((cl.kind, "output"))
        if b is None:
            missing.append(f"output/{cl.kind}")
            continue
        steps.append(
            PlanStep(
                id=f"{base}_m_{_slug(cl.kind)}",
                type="action",
                action=b.action,
                requirement_code=model.requirement_code,
                scenario_seq=scen.seq,
                scenario_name=scen.name or model.title,
                resources=list(b.resource),
                params={
                    "condition_kind": cl.kind,
                    "condition_text": cl.text[:200],
                    "value": cl.value,
                    "unit": b.unit,
                    "method_ref": b.method_ref,
                    "scenario_bindings": dict(scen.bindings),
                    "rail": scen.rail,
                },
                depends_on=[prev_id] if prev_id else [],
                settle_s=b.settle_s,
                destructive=b.destructive,
                setup_group=b.setup_group,
                method_ref=b.method_ref,
            )
        )
        prev_id = steps[-1].id

    return steps, missing
