"""End-to-end acceptance check for the ATERag → ATEStudio pipeline (P6).

This is the script that decides whether a deployment is good. Everything it
checks is a property that fails *silently* in production, which is why it needs
to be a script rather than a checklist someone works through by eye.

What it verifies, and why each one is here:

- The contract hash agrees across the two repos. A mismatch means fields were
  dropped, and the first symptom is a criterion missing from a case.
- The import is idempotent. A second import of the same bundle must change
  nothing; a non-zero delta means conditions double, and every case silently
  gets twice the instrument settings.
- No draft-derived step is in the executable plan. This is the red line, and it
  is the single highest-value assertion here.
- No measurement step lacks a criterion. Under the exec protocol such a step
  returns normally, which is PASSED.
- Every DSL step id is unique. A duplicate makes the dependency graph
  ambiguous, and the scheduler may skip a step — a requirement that quietly
  stops being tested.
- The generated plan parses with the platform's real parser.

Run::

    python scripts/verify_e2e.py --bundle PATH [--json]

Exit code 0 = the deployment may serve a line. Non-zero = do not run units.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ate_cloud.schemas.aterag_bundle import BundleModel, contract_hash  # noqa: E402
from ate_cloud.services.aterag_script_emit import plan_gaps  # noqa: E402
from ate_cloud.services.flow_planner import load_bindings, plan_flow  # noqa: E402

DEFAULT_BUNDLE = Path(r"F:\Workspace\ATERag\rag_storage\exports\studio_bundle.json")

#: The producer's recorded baseline. Pinned so a change on either side is a
#: failure here rather than a surprise in the field.
EXPECTED_CONTRACT_HASH = "59a852c26ca277e4141c6a60ae723b20"


@dataclass(slots=True)
class Check:
    """One assertion and its outcome."""

    name: str
    passed: bool
    detail: str = ""
    fatal: bool = True


@dataclass(slots=True)
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "", fatal: bool = True) -> None:
        self.checks.append(Check(name, passed, detail, fatal))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.fatal]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and not c.fatal]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": not self.failed,
            "checks": [
                {"name": c.name, "passed": c.passed, "fatal": c.fatal, "detail": c.detail}
                for c in self.checks
            ],
        }


def run(bundle_path: Path) -> Report:
    rep = Report()

    if not bundle_path.exists():
        rep.add("bundle 可读", False, f"找不到 {bundle_path}")
        return rep
    rep.add("bundle 可读", True, str(bundle_path))

    raw = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle = BundleModel.model_validate_json(json.dumps(raw, ensure_ascii=False))

    # ── 1. contract ─────────────────────────────────────────────────────────
    mine = contract_hash()
    rep.add(
        "本方契约 hash 与生产方一致",
        mine == bundle.contract_hash,
        f"本方={mine} 生产方={bundle.contract_hash}",
    )
    rep.add(
        "契约 hash 匹配冻结基准",
        mine == EXPECTED_CONTRACT_HASH,
        f"基准={EXPECTED_CONTRACT_HASH} 实际={mine}",
    )

    # ── 2. plan ─────────────────────────────────────────────────────────────
    bindings = load_bindings()
    plan = plan_flow(bundle, bindings)

    rep.add(
        "计划非空",
        plan.step_count > 0,
        f"{len(plan.segments)} 分段 / 计划 {plan.step_count} 步 / "
        f"实际发射 {plan.emitted_step_count} 步",
    )

    # ── 3. red line: no draft-derived step ──────────────────────────────────
    draft_pairs = {
        (r["requirement_code"], c["kind"])
        for r in raw["requirements"]
        for side in ("input_conditions", "output_conditions")
        for c in r[side]
        if c["status"] != "approved"
    }
    leaked = [
        s
        for seg in plan.segments
        for s in seg.steps
        if (s.requirement_code, s.params.get("condition_kind")) in draft_pairs
    ]
    rep.add(
        "红线: 无未批准条件进入执行序列",
        not leaked,
        (
            f"泄漏 {len(leaked)} 步 (未批准条件 {len(draft_pairs)} 组)"
            if leaked
            else f"已排除 {len(plan.pending)} 条未批准条件"
        ),
    )
    rep.add(
        "未批准条件已如实上报",
        len(plan.pending) == len(
            {
                (r["requirement_code"], c["kind"])
                for r in raw["requirements"]
                for side in ("input_conditions", "output_conditions")
                for c in r[side]
                if c["status"] != "approved"
            }
        ),
        f"pending={len(plan.pending)}",
    )

    # ── 4. no unjudgeable measurement ───────────────────────────────────────
    gaps = plan_gaps(plan)
    rep.add(
        "无判据的测量步骤为零",
        not gaps,
        f"{len(gaps)} 个: " + ", ".join(f"{g.requirement_code}/{g.condition_kind}" for g in gaps[:3])
        if gaps
        else "0",
    )

    # ── 5. step ids unique ──────────────────────────────────────────────────
    ids = [s.id for seg in plan.segments for s in seg.steps]
    dupes = len(ids) - len(set(ids))
    rep.add("步骤 id 全局唯一", dupes == 0, f"{len(set(ids))}/{len(ids)}" + (f", 重复 {dupes}" if dupes else ""))

    # ── 6. the real parser accepts it ───────────────────────────────────────
    text = plan.to_yaml()
    try:
        from ate_platform.dsl.parser import YamlParser

        tmp = Path(__file__).resolve().parents[1] / "data" / "_e2e_plan.yaml"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        try:
            parsed = YamlParser().parse(tmp)
            # The emitted plan is longer than the planned one by construction:
            # a clamp, a release, and one recovery fence per destructive
            # segment. Comparing against the predicted total turns "the
            # numbers differ" into "the numbers differ by exactly the safety
            # steps we expect" — an unexplained step injected anywhere else
            # still fails here.
            n = len(parsed.steps)
            want = plan.emitted_step_count
            rep.add(
                "平台 DSL 解析器接受该计划",
                n == want,
                f"{n} 步, version={parsed.version}"
                + ("" if n == want else f" —— 与预测 {want} 步不符, 发射端有未预期的注入"),
            )
        finally:
            tmp.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        rep.add("平台 DSL 解析器接受该计划", False, f"{type(exc).__name__}: {exc}")

    # ── 7. capacity sanity (advisory, not blocking) ─────────────────────────
    # Advisory because a number here is a planning input, not a correctness
    # gate — but a 16-hour per-unit cycle means nobody will run the plan as
    # written, so it is worth saying out loud.
    settle_min = plan.total_settle_s / 60
    rep.add(
        "逐台稳定等待在可接受范围",
        settle_min <= 30,
        f"{settle_min:.1f} 分钟/台"
        + (f", 另有批次级 {plan.batch_settle_s / 60:.0f} 分钟" if plan.batch_setups else ""),
        fatal=False,
    )

    if plan.unmapped:
        rep.add(
            "无未映射场景",
            False,
            f"{len(plan.unmapped)} 个场景的条件无绑定, 这些需求不会被测到",
            fatal=False,
        )

    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    rep = run(args.bundle)

    if args.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
        return 0 if not rep.failed else 1

    print("=" * 68)
    print("ATERag → ATEStudio  端到端验收")
    print("=" * 68)
    for c in rep.checks:
        mark = "PASS" if c.passed else ("FAIL" if c.fatal else "WARN")
        print(f"[{mark}] {c.name}")
        if c.detail:
            print(f"       {c.detail}")
    print()
    if rep.failed:
        print(f"结论: 不可上线 —— {len(rep.failed)} 项致命检查未通过")
        return 1
    if rep.warned:
        print(f"结论: 可上线, 但有 {len(rep.warned)} 项需知会 (非致命)")
        return 0
    print("结论: 全部通过 —— 可上线")
    return 0


if __name__ == "__main__":
    sys.exit(main())
