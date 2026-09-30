"""End-to-end check of the ATERag review wizard **as a user experiences it**.

Why this exists separately from ``verify_e2e.py``
------------------------------------------------
``verify_e2e.py`` calls the planner and the script emitter in-process. It never
opens the UI, never logs in, and never speaks HTTP to the running service. That
gap is not theoretical — three defects shipped and passed every existing check:

  1. The deploy script built the frontend and recorded ``frontend_built: true``,
     but nothing ever served ``frontend/dist``. ``/`` returned 404 while the
     process was active, the health endpoint answered, and the stamp agreed.
  2. ``require_scopes("aterag:import")`` guarded the wizard's import and plan
     steps, and no role — admin included — held that scope. Steps 1, 4 and 5
     returned 403 for every possible account.
  3. The plan was computed from the posted bundle, so a signature moved a
     database column and nothing else: pending stayed 82 and steps stayed 530
     after signing. The wizard's own warning promised the opposite.

All three are invisible to an in-process test and obvious to one walk through
the flow over HTTP. That is the whole reason this file is HTTP-only.

What it does
------------
Drives the five wizard steps in order against a running service, with a real
token, asserting what a reviewer would see on screen — including the two
properties that matter most and that no status code can express:

  * **the red line** — unsigned conditions are excluded from the execution
    sequence, and the exclusion is reported rather than silently dropped;
  * **a signature is worth something** — signing a condition that was excluded
    must move it into the plan. If it does not, the review meeting changed
    nothing and the product is decorative.

Two subtleties this script exists to get right, both of which produced a
confident wrong answer during development:

  * the target requirement is chosen from ``plan.pending``. Signing an input
    condition that carries no measurable data changes neither the pending count
    nor the step count — correctly — and a naive A/B reports that as a defect;
  * the sign → re-plan A/B runs **before** the real import. The importer resets
    condition status from the bundle, so an A/B taken after it measures the
    import, not the signature.

Usage
-----
    python scripts/verify_flow_http.py \\
        --base http://127.0.0.1:8000 \\
        --bundle /tmp/studio_bundle.json \\
        --username e2e-operator --password ...

Credentials come from ``--username``/``--password``, or from
``VERIFY_USER``/``VERIFY_PASSWORD``. The account must hold ``aterag:import``
(see ``scripts/bootstrap_admin.py``).

``--skip-ui`` omits the page-shell assertions, for environments that serve the
API without a frontend build. It does not skip the flow: the 403s had nothing
to do with the frontend.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# The contract hash is pinned on both sides; a change means the bundle contract
# drifted and the two services no longer agree on what a bundle means.
EXPECTED_CONTRACT = "59a852c26ca277e4141c6a60ae723b20"


class Report:
    """Accumulates results so a failure does not abort the walk.

    Stopping at the first failure would hide everything after it, and the most
    useful failures here are several at once — that is the normal shape of "the
    flow is blocked at step 1".
    """

    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}")
        if detail:
            print(f"       {detail}")
        (self.passed if ok else self.failed).append(name if ok else (name, detail))  # type: ignore[arg-type]
        return ok

    def fatal(self, name: str, detail: str) -> None:
        self.check(name, False, detail)
        self.finish()

    def finish(self) -> int:
        total = len(self.passed) + len(self.failed)
        print("\n" + "=" * 72)
        print(f"通过 {len(self.passed)} / {total}")
        if self.failed:
            print("\n未通过:")
            for name, detail in self.failed:
                print(f"  - {name}")
                if detail:
                    print(f"      {detail}")
            return 1
        print("结论: 界面可开, 五步向导全程可走通, 红线与签字效力均已验证")
        return 0


def request(
    base: str, method: str, path: str, *, body: Any = None, token: str | None = None
) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            status, raw = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode("utf-8", "replace")
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def get_text(base: str, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def scopes_of(token: str) -> list[str]:
    part = token.split(".")[1]
    pad = "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part + pad)).get("scopes", [])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", default=os.environ.get("VERIFY_BASE", "http://127.0.0.1:8000"))
    p.add_argument("--bundle", default=os.environ.get("VERIFY_BUNDLE", ""))
    p.add_argument("--username", default=os.environ.get("VERIFY_USER", ""))
    p.add_argument("--password", default=os.environ.get("VERIFY_PASSWORD", ""))
    p.add_argument("--signer", default="流程验证", help="署名; 无署名的批准无法审计")
    p.add_argument("--skip-ui", action="store_true", help="只验流程, 不验页面壳")
    args = p.parse_args()

    if not args.bundle:
        print("需要 --bundle 指向 ATERag 导出的 studio_bundle.json", file=sys.stderr)
        return 2
    if not (args.username and args.password):
        print("需要 --username/--password (或 VERIFY_USER/VERIFY_PASSWORD)", file=sys.stderr)
        return 2

    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    base = args.base.rstrip("/")
    r = Report()
    print(f"验证 {base}  bundle={args.bundle}\n")

    # ── the page shell ──────────────────────────────────────────────────────
    if not args.skip_ui:
        st, html = get_text(base, "/")
        r.check("界面: / 返回构建产物", st == 200 and 'id="app"' in html, f"HTTP {st}, {len(html)} 字节")

        m = re.search(r'src="(/assets/[^"]+\.js)"', html)
        r.check("界面引用了真实 bundle 资源", bool(m), m.group(1) if m else "找不到 /assets/*.js")
        if m:
            ast, js = get_text(base, m.group(1))
            r.check("bundle 资源可下载", ast == 200 and len(js) > 100_000, f"HTTP {ast}, {len(js) // 1024} KB")

        st, _ = get_text(base, "/aterag-review")
        r.check("深链回退到页面壳而非 404", st == 200, f"HTTP {st}")

        st, _ = get_text(base, "/api/v1/definitely-not-a-route")
        r.check("未知 API 路径不被页面壳吞掉", st == 404, f"HTTP {st}")

    # ── login ───────────────────────────────────────────────────────────────
    st, body = request(base, "POST", "/api/v1/auth/login",
                       body={"username": args.username, "password": args.password})
    if not r.check("登录", st == 200, f"HTTP {st} {str(body)[:160]}"):
        return r.finish()
    token = body["access_token"]

    # Checked up front so a 403 here reads as "the flow is blocked at step 1"
    # rather than surfacing three times later as unrelated failures.
    r.check("账号持有 aterag:import (向导导入步骤的先决条件)",
            "aterag:import" in scopes_of(token), f"scopes={scopes_of(token)}")

    # ── step 1: import the extraction result ────────────────────────────────
    st, dry = request(base, "POST", "/api/v1/imports/aterag", body=bundle, token=token)
    if not r.check("第 1 步 dry-run 接受 bundle", st == 200, f"HTTP {st} {str(dry)[:200]}"):
        return r.finish()
    r.check("契约 hash 与硬钉基准一致", dry.get("contract_hash") == EXPECTED_CONTRACT,
            str(dry.get("contract_hash")))
    r.check("标记为 dry-run", dry.get("dry_run") is True, f"dry_run={dry.get('dry_run')}")
    r.check("无冲突需要人工处置", not dry.get("conflicts"), f"conflicts={len(dry.get('conflicts', []))}")

    # ── step 4: plan, and read the red line off it ──────────────────────────
    st, plan = request(base, "POST", "/api/v1/imports/aterag/plan", body=bundle, token=token)
    if not r.check("流程计划可生成", st == 200, f"HTTP {st}"):
        return r.finish()

    warn = " ".join(plan.get("warnings", []))
    r.check("红线: 计划明说排除了未签字条件", "未经人审" in warn or "draft" in warn, warn[:140])
    r.check("红线: 有条件被排除在执行序列外", len(plan.get("pending", [])) > 0,
            f"pending {len(plan.get('pending', []))} 条")
    r.check("无数值判据的步骤被逐条报告 (而非静默)",
            all(g.get("requirement_code") and g.get("reason") for g in plan.get("gaps", [])),
            f"gaps {len(plan.get('gaps', []))} 个: "
            + ", ".join(f"{g.get('requirement_code')}/{g.get('condition_kind')}" for g in plan.get("gaps", [])))

    # ── step 3: review ──────────────────────────────────────────────────────
    st, summary = request(base, "GET", "/api/v1/knowledge/conditions/summary", token=token)
    if not r.check("待签工作列表可取", st == 200, f"HTTP {st}"):
        return r.finish()
    pending_total = summary.get("pending_total", 0)
    r.check("存在待签字条件", pending_total > 0, f"pending_total={pending_total}")

    # Pick a requirement that is actually excluded from the plan. Signing an
    # input condition with no measurable data changes neither the pending count
    # nor the step count — correctly — and an A/B over one reports a defect that
    # is not there.
    pending_codes = {p["requirement_code"] for p in plan.get("pending", [])}
    target = next((x for x in summary.get("items", [])
                   if x.get("draft", 0) > 0 and x["requirement_code"] in pending_codes), None)
    if not r.check("找到在计划中被排除的待签需求", target is not None,
                   f"pending 覆盖 {len(pending_codes)} 个需求"):
        return r.finish()
    code = target["requirement_code"]

    st, page = request(base, "GET",
                       f"/api/v1/knowledge/conditions?requirement_id={target['requirement_id']}&limit=100",
                       token=token)
    items = page.get("items", [])
    r.check("该需求的条件可列出", st == 200 and bool(items), f"HTTP {st}, {len(items)} 条")
    if items:
        first = items[0]
        for field, label in (("requirement_code", "需求编号"),
                             ("section_path", "条款号"),
                             ("requirement_description", "需求描述(签字依据)"),
                             ("requirement_flags", "抽取信号"),
                             ("requirement_assessment", "充分性评估"),
                             ("cond_fingerprint", "条件指纹")):
            r.check(f"条件行带 {label} ({field})", field in first, f"= {str(first.get(field))[:70]}")

    r.check("红线: 未签字条件仍为 draft", "draft" in {i.get("status") for i in items},
            f"状态取值 {sorted({i.get('status') for i in items})}")

    draft_ids = [i["id"] for i in items if i.get("status") == "draft"]
    st, res = request(base, "POST", "/api/v1/knowledge/conditions/approve",
                      body={"requirement_id": target["requirement_id"], "by": args.signer,
                            "condition_ids": draft_ids},
                      token=token)
    r.check("逐条签字成功", st == 200, f"HTTP {st} {str(res)[:120]}")
    if st == 200:
        r.check("签字条数与请求一致", res.get("approved") == len(draft_ids),
                f"approved={res.get('approved')}, 请求 {len(draft_ids)}")
        r.check("签字人已记录 (无署名等于没签)", res.get("by") == args.signer, f"by={res.get('by')}")

    st, after = request(base, "GET", "/api/v1/knowledge/conditions/summary", token=token)
    r.check("待签总数下降", after.get("pending_total", 0) < pending_total,
            f"{pending_total} -> {after.get('pending_total')}")

    # ── the point of the whole exercise ────────────────────────────────────
    # Runs BEFORE the real import on purpose: the importer resets condition
    # status from the bundle, so an A/B taken after it measures the import
    # rather than the signature.
    st, plan2 = request(base, "POST", "/api/v1/imports/aterag/plan", body=bundle, token=token)
    if r.check("签字后可重新规划", st == 200, f"HTTP {st}"):
        p0, p1 = len(plan.get("pending", [])), len(plan2.get("pending", []))
        r.check("签字后被排除的条件变少 (签字对产测生效)", p1 < p0, f"pending {p0} -> {p1}")
        r.check("步数没有倒退", plan2.get("steps", 0) >= plan.get("steps", 0),
                f"步数 {plan.get('steps')} -> {plan2.get('steps')}")
        still = [x for x in plan2.get("pending", []) if x.get("requirement_code") == code]
        r.check("刚签的需求已不在 pending 中", not still, f"{code} 仍有 {len(still)} 条待排除")

    # ── step 5: commit ──────────────────────────────────────────────────────
    # dry_run is a query parameter, as the UI sends it. In the body it is
    # silently ignored, the endpoint falls back to its default of true, and the
    # "commit" is a rehearsal that reports success.
    st, real = request(base, "POST", "/api/v1/imports/aterag?dry_run=false", body=bundle, token=token)
    if r.check("第 5 步 真导入可执行", st == 200, f"HTTP {st}"):
        r.check("真导入标记 dry_run=false", real.get("dry_run") is False, f"dry_run={real.get('dry_run')}")
        r.check("真导入确实写了东西", real.get("changed") is True, f"changed={real.get('changed')}")
        r.check("落库后仍无冲突", not real.get("conflicts"),
                f"conflicts={len(real.get('conflicts', []))}, unmapped={len(real.get('unmapped', []))}")

    st, again = request(base, "POST", "/api/v1/imports/aterag?dry_run=false", body=bundle, token=token)
    if st == 200:
        created = sum(again.get(k, {}).get("created", 0)
                      for k in ("requirements", "conditions", "cases", "limits"))
        r.check("重放未变更的 bundle 不新建任何行 (幂等)", created == 0, f"新建 {created} 条")

    return r.finish()


if __name__ == "__main__":
    raise SystemExit(main())
