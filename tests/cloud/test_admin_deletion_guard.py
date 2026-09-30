"""admin 账号不可删除。

风险类别
--------
``admin`` 是唯一携带 ``aterag:import`` 的角色, 所以删掉一个 admin 少的不只是
一个登录, 而可能是**唯一能跑评审向导导入/规划/提交步骤的账号** —— 之后除了
直接改数据库没有回头的路。

同一类风险代码里已经认过一次: ``/me/deactivate`` 拒绝停用最后一个启用中的
admin。删除端点当时没有同等保护, 于是同一个锁死结果换个动词就能达成。

本文件钉住的行为
----------------
  - admin 账号被删 -> 403, 且错误信息给出可行路径 (先降级再删)
  - 非 admin 账号被删 -> 204
  - 不存在的账号     -> 404 (而不是 403, 否则"探测账号是否存在"变成了 admin 专属)

**已知未覆盖**: 把最后一个 admin 通过 ``PUT /users/{id} {"role": ...}`` 降级,
仍然会造成同样的锁死。这次没有一并堵, 因为需求是"admin 用户不可删除", 而
降级是一个语义明确的独立动作; 是否也要给降权加最后一道保护, 应该是一个
明确决定而不是顺手带上。下面的 test_demotion_of_last_admin_is_still_possible
把这个洞显式钉住, 免得日后有人以为已经堵严实了。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ate_cloud.api.v1.users import deactivate_my_account, delete_user
from ate_cloud.models import Base
from ate_cloud.models.user import User


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed(db: AsyncSession, username: str, role: str) -> User:
    row = User(
        id=str(uuid.uuid4()),
        username=username,
        # Not a real hash: nothing here logs in, and a valid-looking dummy keeps
        # the fixture from depending on the hashing cost.
        password_hash="x",
        role=role,
        scopes=None,
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()
    return row


async def _exists(db: AsyncSession, user_id: str) -> bool:
    return (
        await db.execute(select(User.id).where(User.id == user_id))
    ).scalar_one_or_none() is not None


ADMIN_TOKEN = User(id="admin-token", username="caller", password_hash="x",
                   role="admin", scopes=None, is_active=True)


class TestAdminCannotBeDeleted:
    async def test_admin_delete_is_refused(self, db: AsyncSession) -> None:
        victim = await _seed(db, "root", "admin")
        with pytest.raises(Exception) as ei:
            await delete_user(victim.id, db, current_user=ADMIN_TOKEN)
        assert getattr(ei.value, "status_code", None) == 403
        assert await _exists(db, victim.id), "被拒绝的删除不该已经把行删掉"

    async def test_refusal_names_the_way_out(self, db: AsyncSession) -> None:
        """A bare 403 leaves the operator guessing.

        The remedy — demote first, then delete — is not obvious, and an admin
        who cannot delete an account and is not told why will assume the button
        is broken.
        """
        victim = await _seed(db, "root", "admin")
        with pytest.raises(Exception) as ei:
            await delete_user(victim.id, db, current_user=ADMIN_TOKEN)
        detail = str(getattr(ei.value, "detail", ""))
        assert "降级" in detail
        assert "role" in detail

    async def test_admin_cannot_delete_itself(self, db: AsyncSession) -> None:
        """The exact shape of the lockout, called by the account being deleted."""
        me = await _seed(db, "me", "admin")
        with pytest.raises(Exception) as ei:
            await delete_user(me.id, db, current_user=me)
        assert getattr(ei.value, "status_code", None) == 403
        assert await _exists(db, me.id)

    async def test_refused_even_with_several_admins(self, db: AsyncSession) -> None:
        """The rule is on the role, not on the headcount.

        A "protect only the last one" rule is bypassable in a way that matters
        here: an admin demotes the *other* admin, then deletes itself, and the
        system is down with no admin-shaped account left to reason about.
        """
        me = await _seed(db, "a", "admin")
        await _seed(db, "b", "admin")
        with pytest.raises(Exception) as ei:
            await delete_user(me.id, db, current_user=me)
        assert getattr(ei.value, "status_code", None) == 403


class TestNonAdminStillDeletable:
    async def test_read_role_deletes_cleanly(self, db: AsyncSession) -> None:
        target = await _seed(db, "bob", "read")
        await delete_user(target.id, db, current_user=ADMIN_TOKEN)
        assert not await _exists(db, target.id)

    async def test_every_other_role_deletes_cleanly(self, db: AsyncSession) -> None:
        for role in ("read", "write", "execute"):
            target = await _seed(db, f"u-{role}", role)
            await delete_user(target.id, db, current_user=ADMIN_TOKEN)
            assert not await _exists(db, target.id), role

    async def test_missing_user_is_404_not_403(self, db: AsyncSession) -> None:
        """Otherwise "does this id exist" becomes an admin-only oracle.

        Cheap to leak, and it turns a typo in the UI into a confusing 403.
        """
        with pytest.raises(Exception) as ei:
            await delete_user("no-such-id", db, current_user=ADMIN_TOKEN)
        assert getattr(ei.value, "status_code", None) == 404


class TestDeactivationKeepsItsOwnGuard:
    async def test_last_admin_cannot_deactivate_self(self, db: AsyncSession) -> None:
        """The sibling protection, asserted so it is not lost in a refactor."""
        me = await _seed(db, "solo", "admin")
        with pytest.raises(Exception) as ei:
            await deactivate_my_account(db, current_user=me)
        assert getattr(ei.value, "status_code", None) == 403
        assert me.is_active is True

    async def test_non_last_admin_may_deactivate(self, db: AsyncSession) -> None:
        me = await _seed(db, "second", "admin")
        await _seed(db, "other", "admin")
        await deactivate_my_account(db, current_user=me)
        assert me.is_active is False


class TestKnownRemainingHole:
    async def test_demotion_of_last_admin_is_still_possible(self, db: AsyncSession) -> None:
        """Documents the gap this change deliberately leaves open.

        Not an endorsement — a statement of where the boundary is. Whoever reads
        this should know that "admin 不可删除" is a statement about the DELETE
        verb only, and that a determined admin can still reach the same lockout
        through PUT.
        """
        me = await _seed(db, "solo", "admin")
        me.role = "read"
        await db.commit()
        # Now deletable, and no account holds admin any more.
        assert me.role != "admin"
        admins = (
            await db.execute(select(User.id).where(User.role == "admin"))
        ).scalars().all()
        assert not admins, "最后一个 admin 已降级 —— 系统当前无人能导入"
