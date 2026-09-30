"""Push an ATERag-derived plan + scripts into ATEStudio (P3).

The gate is the point of this script
------------------------------------
Everything upstream — extraction, import, planning, script emission — can
succeed while producing something that must not reach a production line. This
refuses to upload in that case. Two independent reasons:

1. **Unjudgeable measurements.** Under the exec protocol a step with no
   criterion returns normally, which is PASSED. Uploading such a plan means
   units with no measured criterion are recorded as passing, forever.

2. **Unwired drafts.** Every generated script carries ``FILL_IN`` until the
   site supplies instrument wiring. A plan referencing a stub raises at the
   first step — but only on the line, mid-batch, with boards waiting.

``--dry-run`` is the default for the same reason imports are two-phase: a
wrong upload is expensive to unwind, and the cost of looking first is zero.

What is uploaded
----------------
The DSL plan and the generated scripts, registered as ATEStudio ``Script``
rows. Requirements / conditions / limits are **not** re-uploaded here — they
came in through ``POST /api/v1/imports/aterag``, which owns their conflict
policy. Two upload paths for one entity would mean two sets of rules about who
wins.

Usage::

    python scripts/push_to_studio.py --bundle PATH --out DIR --plan-name NAME
    python scripts/push_to_studio.py ... --apply          # actually upload
    python scripts/push_to_studio.py ... --allow-gaps     # sign off on gaps
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ate_cloud.schemas.aterag_bundle import BundleModel  # noqa: E402
from ate_cloud.services.aterag_script_emit import (  # noqa: E402
    FILL_IN,
    PlanGap,
    generate_all,
    plan_gaps,
)
from ate_cloud.services.flow_planner import load_bindings, plan_flow  # noqa: E402


@dataclass(slots=True)
class PushPlan:
    """What a push would upload, and what is blocking it."""

    product_code: str
    plan_yaml: str
    scripts: dict[str, str]
    gaps: list[PlanGap]
    step_count: int
    segment_count: int
    settle_s: float
    pending: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def unwired(self) -> list[str]:
        return sorted(n for n, b in self.scripts.items() if FILL_IN in b)

    def blocking_reasons(self, *, allow_gaps: bool) -> list[str]:
        """Everything standing between this plan and the line."""
        reasons: list[str] = []
        if self.pending:
            reasons.append(
                f"{len(self.pending)} 条条件未经人审(status=draft), 已排除在执行序列之外。"
                " 按红线未批准的条件不得作为产测判据 —— 需先在评审中签字, "
                "再重新规划纳入。"
            )
        if self.gaps and not allow_gaps:
            reasons.append(
                f"{len(self.gaps)} 个测量步骤无数值判据 (exec 协议下会静默报通过)。"
                " 补判据, 或用 --allow-gaps 显式签字承担。"
            )
        if self.unwired:
            reasons.append(
                f"{len(self.unwired)}/{len(self.scripts)} 个脚本仍含 {FILL_IN}, "
                "未补本站仪表接线。"
            )
        reasons.extend(self.warnings)
        return reasons


def build(bundle_path: Path) -> PushPlan:
    """Build the upload payload from a bundle (no side effects)."""
    bundle = BundleModel.model_validate_json(bundle_path.read_text(encoding="utf-8"))
    plan = plan_flow(bundle, load_bindings())
    scripts = {s.name: s.body for s in generate_all(plan)}
    return PushPlan(
        product_code=bundle.product_code,
        plan_yaml=plan.to_yaml(),
        scripts=scripts,
        gaps=plan_gaps(plan),
        step_count=plan.step_count,
        segment_count=len(plan.segments),
        settle_s=plan.total_settle_s,
        pending=list(plan.pending),
        warnings=list(plan.warnings),
    )


def report(pp: PushPlan, *, allow_gaps: bool, apply: bool) -> bool:
    """Print the human-readable plan summary. Returns whether it may proceed."""
    blocking = pp.blocking_reasons(allow_gaps=allow_gaps)

    print(f"产品        : {pp.product_code}")
    print(f"计划        : {pp.segment_count} 分段 / {pp.step_count} 步")
    print(f"逐台稳定等待: {pp.settle_s:.1f}s ({pp.settle_s / 60:.1f} 分钟)")
    print(f"脚本        : {len(pp.scripts)} 个, 其中 {len(pp.unwired)} 个未接线")
    print(f"无判据测量  : {len(pp.gaps)} 个")
    print(f"未批准条件  : {len(pp.pending)} 条 (status=draft, 已排除出执行序列)")
    print()

    if pp.pending:
        print("=== 未批准条件 (红线: 不得作为产测判据) ===")
        by_kind: dict[str, int] = {}
        for c in pp.pending:
            by_kind[c["kind"]] = by_kind.get(c["kind"], 0) + 1
        for k, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            print(f"  {k:22} {n:3} 条")
        print("  -> 评审签字后重新规划即可纳入")
        print()

    if pp.gaps:
        print("=== 无判据的测量步骤 (exec 协议下会报通过) ===")
        for g in pp.gaps[:10]:
            print(f"  {g.requirement_code[:44]:44} {g.condition_kind}")
        if len(pp.gaps) > 10:
            print(f"  ... 另 {len(pp.gaps) - 10} 条")
        print()

    for w in pp.warnings:
        print(f"警告: {w}")
    if pp.warnings:
        print()

    if not blocking:
        print("=== 可以上传 ===" if apply else "=== 校验通过 (加 --apply 实际上传) ===")
        return True

    print("=== 阻止上传 ===")
    for r in blocking:
        print(f"  - {r}")
    return False


def upload(pp: PushPlan, base_url: str, token: str, plan_name: str, timeout: int) -> int:
    """Upload the plan and scripts via the ATEStudio API."""
    import urllib.error
    import urllib.request

    def _post(path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        _post(
            "/api/v1/scripts/register",
            {
                "name": plan_name,
                "description": f"ATERag 生成草稿: {pp.step_count} 步 / {len(pp.scripts)} 脚本",
                "script_path": f"generated/{plan_name}.yaml",
                "tags": ["aterag", "draft"],
            },
        )
    except urllib.error.HTTPError as e:
        print(f"上传失败: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}")
        return 1
    except urllib.error.URLError as e:
        print(f"无法连接 {base_url}: {e.reason}")
        return 1

    print(f"已注册脚本 {plan_name} (草稿)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", type=Path, required=True)
    ap.add_argument("--out", type=Path, help="把计划和脚本写到本地目录")
    ap.add_argument("--plan-name", default="")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--token", default="")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--apply", action="store_true", help="真正上传 (默认只校验)")
    ap.add_argument(
        "--allow-gaps",
        action="store_true",
        help="对无判据的测量步骤显式签字放行 (需人工承担后果)",
    )
    args = ap.parse_args()

    if not args.bundle.exists():
        print(f"找不到 bundle: {args.bundle}")
        return 2

    pp = build(args.bundle)
    ok = report(pp, allow_gaps=args.allow_gaps, apply=args.apply)

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "plan.yaml").write_text(pp.plan_yaml, encoding="utf-8")
        for name, body in pp.scripts.items():
            (args.out / name).write_text(body, encoding="utf-8")
        (args.out / "_gaps.json").write_text(
            json.dumps([g.to_dict() for g in pp.gaps], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n已写出 {len(pp.scripts) + 2} 个文件到 {args.out}")

    if not ok:
        print("\n未上传。修掉上面的问题, 或用 --allow-gaps 签字放行。")
        return 1
    if not args.apply:
        return 0

    name = args.plan_name or f"{pp.product_code}_aterag_draft"
    return upload(pp, args.base_url, args.token, name, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
