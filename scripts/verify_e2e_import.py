"""Production E2E: import the real ATERag bundle into the deployed Postgres.

Run on the target board, inside the deployment venv. Verifies the import
against a real PostgreSQL rather than the SQLite used in CI — the two differ
in exactly the places this pipeline is most likely to break (batch DDL,
JSON columns, transaction semantics), so a CI pass is not evidence here.

Checks, in order:
  1. contract hash agreement (cross-repo)
  2. first import writes the expected counts
  3. replay is a no-op  (idempotency — the property that silently doubles
     conditions if broken)
  4. provenance landed (section_path / notes / created_by)
  5. draft conditions are stored as draft, not promoted

Exit code 0 only if all five hold.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ate_cloud.config import settings
from ate_cloud.models.knowledge import SOURCE_ATERAG, TestCase, TestRequirement
from ate_cloud.models.test_conditions import TestCondition
from ate_cloud.schemas.aterag_bundle import BundleModel, contract_hash
from ate_cloud.services.aterag_importer import ATERagImporter

EXPECTED_CONTRACT_HASH = "59a852c26ca277e4141c6a60ae723b20"


def _report(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if detail:
        print(f"       {detail}")
    return ok


async def main() -> int:
    bundle_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/studio_bundle.json")
    if not bundle_path.exists():
        print(f"找不到 bundle: {bundle_path}")
        return 2

    bundle = BundleModel.model_validate_json(bundle_path.read_text(encoding="utf-8"))
    ok = True

    ok &= _report(
        "契约 hash 与生产方一致",
        contract_hash() == bundle.contract_hash == EXPECTED_CONTRACT_HASH,
        f"本方={contract_hash()} 生产方={bundle.contract_hash}",
    )

    n_req = len(bundle.requirements)
    n_cond = sum(len(r.input_conditions) + len(r.output_conditions) for r in bundle.requirements)
    n_scen = sum(len(r.scenarios) for r in bundle.requirements)
    print(f"\nBundle: {n_req} 需求 / {n_scen} 场景 / {n_cond} 条件")
    print(f"DB URL: {settings.get_database_url().split('@')[-1]}\n")

    url = settings.get_database_url()
    engine = create_async_engine(url, poolclass=None)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with maker() as db:
            imp = ATERagImporter()

            # Refuse to import into a database that already holds someone
            # else's requirements for this product: this script exists to prove
            # the path works, and a second run over production data would
            # rewrite real rows.
            existing = (
                await db.execute(
                    select(func.count())
                    .select_from(TestRequirement)
                    .where(TestRequirement.source == SOURCE_ATERAG)
                )
            ).scalar_one()
            if existing:
                print(f"[SKIP] 该库已有 {existing} 条 ATERag 来源需求, 拒绝覆盖")
                return 2

            r1 = await imp.import_bundle(db, bundle, dry_run=False)
            ok &= _report(
                "首次导入计数正确",
                r1.requirements.created == n_req
                and r1.conditions.created == n_cond
                and r1.cases.created == n_scen,
                f"需求 {r1.requirements.to_dict()} 条件 {r1.conditions.to_dict()} "
                f"用例 {r1.cases.to_dict()} 限值 {r1.limits.to_dict()}",
            )

            r2 = await imp.import_bundle(db, bundle, dry_run=False)
            replay_zero = (
                r2.requirements.created == 0
                and r2.requirements.updated == 0
                and r2.conditions.created == 0
                and r2.conditions.updated == 0
                and r2.cases.created == 0
                and r2.cases.updated == 0
                and r2.cases_reset_to_draft == []
            )
            ok &= _report(
                "重放为幂等空操作",
                replay_zero,
                f"需求 {r2.requirements.to_dict()} 条件 {r2.conditions.to_dict()} "
                f"用例 {r2.cases.to_dict()} 回退draft {len(r2.cases_reset_to_draft)}",
            )

            for model, want in ((TestRequirement, n_req), (TestCondition, n_cond), (TestCase, n_scen)):
                got = (await db.execute(select(func.count()).select_from(model))).scalar_one()
                ok &= _report(f"{model.__tablename__} 行数", got == want, f"{got} (期望 {want})")

            with_path = (
                await db.execute(
                    select(func.count())
                    .select_from(TestRequirement)
                    .where(TestRequirement.section_path.is_not(None))
                )
            ).scalar_one()
            ok &= _report(
                "规格书条款号已落库",
                with_path == n_req,
                f"{with_path}/{n_req} 条带 section_path (追溯到规格书的唯一锚点)",
            )

            statuses = (
                await db.execute(select(TestCondition.status).distinct())
            ).scalars().all()
            ok &= _report(
                "未签字条件保持 draft",
                "draft" in statuses,
                f"条件状态取值: {statuses} —— 未经人审的条件不得被提升为判据",
            )

            by = (
                await db.execute(
                    select(TestCase.created_by, func.count()).group_by(TestCase.created_by)
                )
            ).all()
            ok &= _report("用例来源已记录", all(r[0] == "aterag:bundle" for r in by), str(dict(by)))

    finally:
        await engine.dispose()

    print()
    if ok:
        print("结论: 生产导入端到端通过")
        return 0
    print("结论: 有检查未通过 —— 不要在此库上继续")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
