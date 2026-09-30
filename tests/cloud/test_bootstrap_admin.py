"""``bootstrap_admin.py`` must never be a silent no-op.

The bug this covers
-------------------
``_apply`` returned early with ``"already has role=admin; nothing to do"``
whenever the account already held the requested role — *before* looking at the
password. So

    bootstrap_admin.py --username e2e-operator --role admin --password admin

printed a success-looking line, changed nothing, and the next login with the new
password 401'd. It surfaced only by running it against a real deployment: the
obvious test (create, then re-run, expect idempotence) passes, because without a
password the early return *is* the correct behaviour.

That is why the password cases are what gets asserted here. Silently doing
nothing while reporting success is worse than failing — the operator cannot tell
the difference except by trying to log in.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ate_cloud.auth.password import hash_password, verify_password
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


async def _seed(db: AsyncSession, username: str, password: str, role: str) -> User:
    row = User(
        id=str(uuid.uuid4()),
        username=username,
        password_hash=hash_password(password),
        role=role,
        scopes=None,
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()
    return row


async def _fetch(db: AsyncSession, username: str) -> User:
    """Read back by natural key.

    ``Session.get`` only accepts a primary key, so the username lookups these
    tests are about have to go through an explicit select.
    """
    row = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one()
    return row


@pytest.mark.asyncio
class TestPasswordResetIsNotSilentlySkipped:
    async def test_same_role_still_resets_the_password(self, db: AsyncSession) -> None:
        """The exact case that shipped broken: role matches, password supplied."""
        from scripts.bootstrap_admin import _apply

        seeded = await _seed(db, "op", "old-password", "admin")
        await _apply(db, "op", "admin", "new-password", promote=False)

        assert verify_password("new-password", seeded.password_hash)
        assert not verify_password("old-password", seeded.password_hash)

    async def test_promotion_also_resets_the_password(self, db: AsyncSession) -> None:
        from scripts.bootstrap_admin import _apply

        seeded = await _seed(db, "op", "old-password", "read")
        await _apply(db, "op", "admin", "new-password", promote=False)

        assert seeded.role == "admin"
        assert verify_password("new-password", seeded.password_hash)

    async def test_no_password_leaves_the_hash_untouched(self, db: AsyncSession) -> None:
        """Idempotence is still idempotence: a bare re-run must not clear it.

        The early return exists for this case. Blanking the password on every
        invocation would be a different silent no-op.
        """
        from scripts.bootstrap_admin import _apply

        seeded = await _seed(db, "op", "keep-me", "admin")
        await _apply(db, "op", "admin", None, promote=True)

        assert verify_password("keep-me", seeded.password_hash)

    async def test_reset_reactivates_a_disabled_account(self, db: AsyncSession) -> None:
        """A password reset that leaves the account disabled looks like success.

        Same reasoning as the promotion branch: the operator's next action is a
        login, and a 401 on a disabled account reads as "the password didn't
        take" rather than "the account is off".
        """
        from scripts.bootstrap_admin import _apply

        seeded = await _seed(db, "op", "old-password", "admin")
        seeded.is_active = False
        await db.commit()

        await _apply(db, "op", "admin", "new-password", promote=False)
        assert seeded.is_active is True


@pytest.mark.asyncio
class TestCreation:
    async def test_creates_with_the_requested_role(self, db: AsyncSession) -> None:
        from scripts.bootstrap_admin import _apply

        await _apply(db, "newbie", "write", "pw", promote=False)
        row = await _fetch(db, "newbie")
        assert row.role == "write"
        assert verify_password("pw", row.password_hash)

    async def test_promote_on_a_missing_account_fails_loudly(self, db: AsyncSession) -> None:
        """Refusing is the whole point of ``--promote``.

        Silently creating an account here would hand out credentials on a path
        the operator explicitly said was a role change.
        """
        from scripts.bootstrap_admin import _apply

        with pytest.raises(SystemExit, match="does not exist"):
            await _apply(db, "ghost", "admin", None, promote=True)

    async def test_creating_without_a_password_fails_loudly(self, db: AsyncSession) -> None:
        from scripts.bootstrap_admin import _apply

        with pytest.raises(SystemExit, match="needs a password"):
            await _apply(db, "nopass", "read", None, promote=False)
