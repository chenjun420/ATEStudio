"""Verify the condition-kind binding table against real ATERag output.

Why this exists
---------------
A missing binding is the worst kind of gap: it does not raise. The planner
silently omits the steps for that condition, the report shows "all passed",
and an untested unit ships. So the table must be checked against the kinds
that *actually appear in real data*, not against a hand-written list that
drifts the moment someone adds a kind upstream.

The check is bidirectional:
- kind in data but not in table  -> that condition can never become a step
- kind in table but not in data  -> a binding that will never fire; it is
  usually a typo, or a kind upstream stopped emitting

Run::

    python scripts/verify_bindings.py [--bundle PATH]

Exit code 0 = table covers reality. Non-zero = do not run a production plan.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = Path(r"F:\Workspace\ATERag\rag_storage\exports\studio_bundle.json")
BINDINGS = REPO / "config" / "test_method_bindings.yaml"

#: Actions that consume a resource exclusively and therefore cannot overlap
#: with another step holding the same resource. Kept here rather than in the
#: YAML because it is a property of the *platform's* resource model, not of
#: any one test domain — a change to the DSL's resources contract should
#: change this list, not the test bindings.
EXCLUSIVE_RESOURCES = {"PSU_1", "ELOAD_1", "CHAMBER_1", "RELAY_MATRIX"}


def load_bindings() -> list[dict]:
    data = yaml.safe_load(BINDINGS.read_text(encoding="utf-8"))
    return data["bindings"]


def collect_kinds(bundle_path: Path) -> tuple[Counter, Counter]:
    """Count (kind, side) pairs in a real bundle."""
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    counts: Counter = Counter()
    sides: Counter = Counter()
    for req in bundle.get("requirements", []):
        for clause in req.get("input_conditions", []):
            counts[clause["kind"]] += 1
            sides[("input", clause["kind"])] += 1
        for clause in req.get("output_conditions", []):
            counts[clause["kind"]] += 1
            sides[("output", clause["kind"])] += 1
    return counts, sides


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    args = ap.parse_args()

    if not args.bundle.exists():
        print(f"找不到 bundle: {args.bundle}")
        print("先在 ATERag 侧跑 scripts/export_studio.py 导出")
        return 2

    bindings = load_bindings()
    table = {(b["kind"], b["side"]) for b in bindings}
    counts, sides = collect_kinds(args.bundle)

    failures: list[str] = []
    print(f"绑定表 {len(bindings)} 条, 真实数据 {len(sides)} 个 kind+side 组合")
    print(f"真实条件总数: {sum(counts.values())}")
    print()

    # ---- 1. 真实数据里的每个 (kind, side) 都必须有绑定 ----
    print("=== 覆盖检查 (真实数据 -> 绑定表) ===")
    uncovered = 0
    for (side, kind), n in sorted(sides.items(), key=lambda kv: -kv[1]):
        if (kind, side) in table:
            print(f"  OK   {side:6} {kind:22} {n:4} 条")
        else:
            uncovered += n
            failures.append(f"未绑定: {side}/{kind} — {n} 条真实条件排不出步骤")
            print(f"  FAIL {side:6} {kind:22} {n:4} 条  <== 未绑定")
    print()

    # ---- 2. 绑定表里的每条都应在真实数据里出现过 ----
    print("=== 冗余检查 (绑定表 -> 真实数据) ===")
    for kind, side in sorted(table):
        if (side, kind) in sides:
            continue
        failures.append(f"空绑定: {side}/{kind} — 表里有, 真实数据里没出现")
        print(f"  WARN {side:6} {kind:22} 真实数据中未出现 (可能是冗余或拼写错误)")
    print()

    # ---- 3. 同一 (kind, side) 不得有多条绑定 ----
    dup = Counter((b["kind"], b["side"]) for b in bindings)
    print("=== 唯一性检查 ===")
    for key, n in dup.items():
        if n > 1:
            failures.append(f"重复绑定: {key} 有 {n} 条 — 规划器将任意选一条, 计划不可复现")
            print(f"  FAIL {key} 出现 {n} 次")
    if not any(n > 1 for n in dup.values()):
        print("  OK   无重复")
    print()

    # ---- 4. requires 指向的 kind 必须自己也有绑定 ----
    print("=== 依赖检查 ===")
    kinds_with_binding = {k for k, _ in table}
    for b in bindings:
        for dep in b.get("requires", []):
            if dep not in kinds_with_binding:
                failures.append(
                    f"悬空依赖: {b['kind']}/{b['side']} 依赖 {dep}, 但 {dep} 无绑定"
                )
                print(f"  FAIL {b['kind']}/{b['side']} -> {dep} 无绑定")
    if not any("悬空依赖" in f for f in failures):
        print("  OK   无悬空依赖")
    print()

    # ---- 5. 排他资源不得并发 (平台约束的自检) ----
    print("=== 排他资源检查 ===")
    excl = [(b["kind"], b["side"], r) for b in bindings for r in b.get("resource", []) if r in EXCLUSIVE_RESOURCES]
    print(f"  {len(excl)} 条绑定占用排他资源 {sorted(EXCLUSIVE_RESOURCES)}")
    print("  规划器据此串行化: 同一时刻只有一条步骤可持有某台仪器")
    print()

    # ---- 结论 ----
    if failures:
        print(f"=== 失败 {len(failures)} 项 ===")
        for f in failures:
            print(f"  - {f}")
        if uncovered:
            print()
            print(f"其中 {uncovered} 条真实条件将排不出任何步骤。")
            print("产测会漏测这些项, 而报告上一切正常 —— 流出的是不合格品。")
        return 1

    print("=== 全部通过 ===")
    print(f"真实数据 {sum(counts.values())} 条条件全部有绑定, 计划可生成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
