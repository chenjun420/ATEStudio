"""迁移必须被真正执行过 —— 而不只是模型能建表。

为什么要有这个测试
------------------
``test_station_models.py`` 走 ``Base.metadata.create_all``, 它按 ORM 的声明建表,
**完全不碰 Alembic**。于是一个把 ``op.create_foreign_key`` 参数写反的迁移可以
一路绿灯通过本地全部测试, 直到部署到板子上才炸:

    ProgrammingError: 在外键约束中的关联字段 "plant_id" 不存在

Alembic 的签名是 ``create_foreign_key(name, source_table, referent_table, ...)``,
约束挂在**第二个**表上。反过来写时 Postgres 报的是「被引用列不存在」, 读起来
像少建了一列, 而实际是调用方向反了 —— 这个歧义只靠真实数据库才暴露得出来。

这与本项目已有的一类问题同形: 单元测试全绿、构建成功、进程 active, 而能力
从未运行过一次。所以本文件存在的意义是**让迁移在声称可用之前先被跑过一遍**。

需要什么才能跑
--------------
真实 Postgres。SQLite 不行: 它不校验被引用表的存在, 也不执行
``ON DELETE`` 行为, 而这两样正是下面几条断言的内容。

    TEST_POSTGRES_URL=postgresql://user:pw@host:5432/postgres pytest tests/cloud/test_migrations.py

未设置或连不上时整个模块跳过, 并在跳过理由里写明缺什么 —— 不静默通过。
板卡上有 Postgres, 验收时用它跑。
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONS_DIR = REPO_ROOT / "alembic" / "versions"

NO_POSTGRES = (
    "需要真实 Postgres 才能验证迁移 —— 设 TEST_POSTGRES_URL。"
    "SQLite 的 create_all 不经过 Alembic, 也无法暴露 FK 方向与 ON DELETE 错误"
)


def _admin_url() -> str | None:
    return os.environ.get("TEST_POSTGRES_URL") or None


# No module-level skip: the migration-chain test below needs no database, and it
# is the one assertion that can run on a developer machine at all.


# ── helpers ────────────────────────────────────────────────────────────────


def _dsn_to_admin(base: str, dbname: str) -> str:
    """Swap the database name, keeping driver/host/credentials intact."""
    head, _, _ = base.rpartition("/")
    tail = base.split("/")[-1]
    query = ""
    if "?" in tail:
        query = "?" + tail.split("?", 1)[1]
    return f"{head}/{dbname}{query}"


def _run_sql(url: str, sql: str) -> None:
    """Execute one statement via asyncpg, synchronously from the test's view."""
    import asyncio

    async def go() -> None:
        import asyncpg

        conn = await asyncpg.connect(_dsn_to_admin(url, url.split("/")[-1].split("?")[0]))
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(go())


def _query(url: str, sql: str) -> list[tuple]:
    import asyncio

    async def go() -> list[tuple]:
        import asyncpg

        conn = await asyncpg.connect(_dsn_to_admin(url, url.split("/")[-1].split("?")[0]))
        try:
            return await conn.fetch(sql)
        finally:
            await conn.close()

    return asyncio.run(go())


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke Alembic exactly the way the deploy script does.

    The deploy script runs ``PYTHONPATH="$REPO_DIR/src" python -m alembic ...``,
    and ``alembic/env.py`` imports ``src.ate_cloud.config`` rather than
    ``ate_cloud.config``. Reproducing the deploy invocation keeps this test from
    validating a code path the deployment never takes.

    Subprocess rather than in-process ``command.upgrade``: ``env.py`` resolves
    the URL from its own imported ``settings`` object, so patching settings
    in-process would have no effect on the module Alembic actually reads.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["ATE_CLOUD_DATABASE_URL"] = url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.fixture
def migrated_db() -> str:
    """A scratch database migrated to head, dropped afterwards.

    Scratch rather than the real one: ``downgrade`` runs against it, and a
    migration whose downgrade is broken should be discovered here rather than
    during a rollback on a board holding production data.
    """
    base = _admin_url()
    if not base:
        pytest.skip(NO_POSTGRES)
    name = f"mig_test_{uuid.uuid4().hex[:12]}"

    try:
        _run_sql(base, f'CREATE DATABASE "{name}"')
    except Exception as exc:  # noqa: BLE001 — unreachable host must skip, not fail
        pytest.skip(f"无法连接 Postgres 以建临时库({type(exc).__name__})")

    url = _dsn_to_admin(base, name)
    try:
        result = _alembic(url, "upgrade", "head")
        if result.returncode != 0:
            pytest.fail(
                "alembic upgrade head 失败 —— 迁移从未真正跑通过:\n"
                f"{result.stdout}\n{result.stderr}"
            )
        yield url
    finally:
        try:
            _run_sql(base, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        except Exception:  # noqa: BLE001,S110 — cleanup must not mask a real failure
            pass


def _fk_map(url: str, table: str) -> dict[str, str]:
    """column -> fully qualified referenced table, from information_schema."""
    rows = _query(
        url,
        f"""
        SELECT kcu.column_name,
               ccu.table_schema || '.' || ccu.table_name AS ref_table
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_name = '{table}'
        """,
    )
    return {r["column_name"]: r["ref_table"] for r in rows}


# ── tests ──────────────────────────────────────────────────────────────────


class TestTheMigrationRuns:
    def test_upgrade_head_succeeds(self, migrated_db: str) -> None:
        """The bare minimum, stated as its own test.

        If this is the only assertion anyone reads, it should be the one that
        would have caught the reversed ``create_foreign_key`` arguments.
        """
        rows = _query(
            migrated_db,
            """
            SELECT version_num FROM alembic_version
            """,
        )
        assert rows, "upgrade head 声称成功但 alembic_version 是空的"


class TestForeignKeysPointWhereTheyWereMeantTo:
    """Direction is the thing that reads wrong when it is wrong.

    A reversed call produces "referenced column does not exist", which reads as
    a missing column instead of a backwards argument list.
    """

    def test_station_belongs_to_plant(self, migrated_db: str) -> None:
        fks = _fk_map(migrated_db, "stations")
        assert fks.get("plant_id") == "public.plants"

    def test_fault_case_belongs_to_station(self, migrated_db: str) -> None:
        fks = _fk_map(migrated_db, "station_fault_cases")
        assert fks.get("station_id") == "public.stations"

    def test_fault_case_evidence_link_survives_diagnosis_pruning(self, migrated_db: str) -> None:
        """``diagnoses`` is prunable evidence; the case that outlives it is not.

        A CASCADE here would delete fault history because someone cleaned up a
        diagnosis, which is the same failure as cascading from plant to station.
        """
        fks = _fk_map(migrated_db, "station_fault_cases")
        assert fks.get("source_diagnosis_id") == "public.diagnoses"


class TestDeleteBehaviourIsWhatTheDesignSays:
    def _delete_rule(self, url: str, table: str, column: str) -> str:
        rows = _query(
            url,
            """
            SELECT rc.delete_rule AS rule
            FROM information_schema.referential_constraints rc
            JOIN information_schema.key_column_usage kcu
              ON rc.constraint_name = kcu.constraint_name
             AND rc.constraint_schema = kcu.constraint_schema
            WHERE kcu.table_name = '{table}' AND kcu.column_name = '{column}'
              AND rc.constraint_schema = 'public'
            """,
        )
        assert rows, f"{table}.{column} 上没有外键"
        return rows[0]["rule"]

    def test_deleting_a_station_removes_its_cases(self, migrated_db: str) -> None:
        """A case has no meaning without its station — CASCADE is correct here."""
        assert self._delete_rule(migrated_db, "station_fault_cases", "station_id") == "CASCADE"

    def test_pruning_a_diagnosis_keeps_the_fault_case(self, migrated_db: str) -> None:
        """The opposite, and the one that protects history."""
        rule = self._delete_rule(migrated_db, "station_fault_cases", "source_diagnosis_id")
        assert rule == "SET NULL"


class TestColumnsCarryTheConstraintsTheModelClaims:
    def _is_nullable(self, url: str, table: str, column: str) -> str:
        rows = _query(
            url,
            f"""
            SELECT is_nullable FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = '{table}'
              AND column_name = '{column}'
            """,
        )
        assert rows, f"{table}.{column} 不存在"
        return rows[0]["is_nullable"]

    def test_every_station_has_a_plant(self, migrated_db: str) -> None:
        """NOT NULL is what makes plant-with-stations undeletable.

        Without it the FK's SET NULL would orphan stations into rows the
        context selector can never display.
        """
        assert self._is_nullable(migrated_db, "stations", "plant_id") == "NO"

    def test_an_unverified_fix_cannot_be_stored_as_null(self, migrated_db: str) -> None:
        """``fix_verified`` NULL would break "only quote verified remedies".

        A NULL reads as neither verified nor unverified, and a filter of
        ``fix_verified IS TRUE`` would quietly drop the row instead of treating
        it as unverified.
        """
        assert self._is_nullable(migrated_db, "station_fault_cases", "fix_verified") == "NO"

    def test_a_case_cannot_exist_without_an_observation(self, migrated_db: str) -> None:
        assert self._is_nullable(migrated_db, "station_fault_cases", "symptom") == "NO"


class TestIndexesServeTheQueriesPhaseOneExistsToAnswer:
    def test_station_codes_unique_within_a_plant(self, migrated_db: str) -> None:
        """Global uniqueness would make the second line un-modellable.

        Two lines routinely call their first station W1. A duplicate inside one
        plant, though, splits one station's fault history across two rows — and
        "no history here" is indistinguishable from "nothing has failed here".
        """
        rows = _query(
            migrated_db,
            """
            SELECT indexdef FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = 'stations'
              AND indexdef LIKE '%UNIQUE%'
            """,
        )
        defs = " ".join(r["indexdef"] for r in rows)
        assert "plant_id" in defs and "code" in defs

    def test_rpn_and_verification_are_reachable_per_station(self, migrated_db: str) -> None:
        for index in (
            "ix_fault_cases_station_rpn",
            "ix_fault_cases_station_verified",
            "ix_fault_cases_station_occurred",
        ):
            rows = _query(
                migrated_db,
                f"SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname='{index}'",
            )
            assert rows, f"缺索引 {index}"


class TestTheChainIsReversible:
    """Downgrade runs against a scratch DB, never against a board's data."""

    def test_downgrade_then_upgrade_again(self, migrated_db: str) -> None:
        down = _alembic(migrated_db, "downgrade", "-1")
        assert down.returncode == 0, f"downgrade 失败:\n{down.stdout}\n{down.stderr}"

        rows = _query(
            migrated_db,
            """
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public' AND tablename = 'station_fault_cases'
            """,
        )
        assert not rows, "downgrade 后表还在"

        up = _alembic(migrated_db, "upgrade", "head")
        assert up.returncode == 0, f"回滚后无法重新升级:\n{up.stdout}\n{up.stderr}"


class TestEveryVersionIsOnOneChain:
    """A second head is discovered only when someone tries to upgrade.

    ``alembic upgrade head`` on two heads applies neither and says nothing, so
    the merge has to be asserted where it is cheap to assert.
    """

    def test_single_head(self) -> None:
        files = sorted(VERSIONS_DIR.glob("*.py"))
        if not files:
            pytest.skip("还没有迁移")

        import re

        revisions: dict[str, str | None] = {}
        for f in files:
            text = f.read_text(encoding="utf-8")
            rev = re.search(r'^revision[^=]*=\s*["\']([^"\']+)', text, re.M)
            down = re.search(r'^down_revision[^=]*=\s*["\']([^"\']+)', text, re.M)
            if rev:
                revisions[rev.group(1)] = down.group(1) if down else None

        referenced = {v for v in revisions.values() if v}
        missing = referenced - set(revisions)
        assert not missing, f"down_revision 指向不存在的 revision: {missing}"

        heads = [r for r in revisions if r not in referenced]
        assert len(heads) == 1, f"迁移链有 {len(heads)} 个头 {heads} —— upgrade head 会两个都不执行"
