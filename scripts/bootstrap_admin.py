"""Create or promote a user to a given role. Run once, at deploy time.

Why this exists
---------------
``/auth/register`` hardcodes ``role="read"``. Nothing else in the application
can change a role except ``UserManagement.vue``, which needs the ``admin``
scope to load. So a fresh deployment has no way to obtain an admin: the first
account registered is read-only, and the screen that would fix that is behind a
permission that account does not have.

Combined with ``ROLE_SCOPES`` omitting ``aterag:import`` (fixed separately),
that made the ATERag review wizard impossible to complete for any user — the
wizard's import, plan and commit steps all returned 403.

Why a script and not a change to ``register``
---------------------------------------------
Auto-promoting "the first user to register" is a well-known way to hand an
admin account to whoever reaches an exposed port first, and this service is
reachable on the LAN. A deploy-time command is explicit, auditable (it is a
committed file plus a shell history entry), and leaves no permanent backdoor in
the request path: once the admin exists, this script is not needed again.

Usage
-----
    python scripts/bootstrap_admin.py --username alice --role admin
    python scripts/bootstrap_admin.py --username alice --promote   # no password needed

``--promote`` is for the case where the account already exists — the common
one, because operators typically register through the login page first and only
then discover they cannot import.

The password is read from ``--password`` or ``ATE_BOOTSTRAP_PASSWORD``. Both
are accepted so a generated secret need not appear in shell history.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import uuid
from pathlib import Path

# Run from a checkout as well as from an installed venv: the deploy unit sets
# PYTHONPATH, but running this by hand from /opt/atestudio should not require
# remembering that.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from ate_cloud.auth.password import hash_password  # noqa: E402
from ate_cloud.auth.rbac import ROLE_SCOPES  # noqa: E402
from ate_cloud.config import settings  # noqa: E402
from ate_cloud.db.session import async_session_factory  # noqa: E402
from ate_cloud.models.user import User  # noqa: E402


def _resolve_password(args: argparse.Namespace) -> str:
    """Password from the flag, then the environment, then an interactive prompt.

    The environment variable exists so an operator can pass a generated secret
    without it landing in shell history.
    """
    if args.password:
        return args.password
    env = os.environ.get("ATE_BOOTSTRAP_PASSWORD")
    if env:
        return env
    if not sys.stdin.isatty():
        raise SystemExit(
            "no password given: pass --password, set ATE_BOOTSTRAP_PASSWORD, "
            "or run on a terminal to be prompted"
        )
    first = getpass.getpass("password for the account: ")
    if first != getpass.getpass("confirm: "):
        raise SystemExit("passwords did not match")
    return first


async def _apply(
    db: AsyncSession,
    username: str,
    role: str,
    password: str | None,
    promote: bool,
) -> int:
    """Create or update one account on ``db``.

    The session is a parameter rather than opened here. It used to be reached
    through the module-level ``async_session_factory``, which made the most
    safety-critical branch untestable: a test calling this wrote to the real
    database, so the tests could not be written at all — and the branch they
    most needed to cover, the one that silently did nothing while printing
    success, stayed unverified until it was run against a real deployment.
    """
    existing = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()

    if existing is not None:
        if existing.role == role:
            # An explicit password must still be honoured here. This branch used
            # to return immediately with "nothing to do", which made
            # `--username u --password newpw` against an account that already
            # held the role a **silent no-op**: it printed a success-looking
            # line, changed nothing, and the next login 401'd. Resetting the
            # password of an account that already has the role is the most
            # common reason to run this script twice.
            if password:
                existing.password_hash = hash_password(password)
                existing.is_active = True
                await db.commit()
                print(f"{username} already has role={role}; password reset")
            else:
                print(f"{username} already has role={role}; nothing to do")
            return 0
        old = existing.role
        existing.role = role
        # A promoted account must be able to log in, or the promotion is
        # invisible: the next sign-in attempt would 401 and look like the script
        # did nothing.
        existing.is_active = True
        if password:
            existing.password_hash = hash_password(password)
        await db.commit()
        print(f"promoted {username}: role {old} -> {role}")
        if password:
            print("  password reset")
        return 0

    if promote:
        raise SystemExit(f"--promote given but {username!r} does not exist yet")

    if not password:
        raise SystemExit("creating an account needs a password")

    db.add(
        User(
            id=str(uuid.uuid4()),
            username=username,
            password_hash=hash_password(password),
            role=role,
            scopes=None,
            is_active=True,
        )
    )
    await db.commit()
    print(f"created {username} with role={role}")
    print(f"  effective scopes: {sorted(ROLE_SCOPES.get(role, []))}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=(
            "examples:\n"
            "  python scripts/bootstrap_admin.py --username alice --role admin\n"
            "  python scripts/bootstrap_admin.py --username alice --promote\n"
            "\n"
            "--promote is for the common case where the account already exists,\n"
            "because operators typically register through the login page first\n"
            "and only then discover they cannot import."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--username", required=True)
    p.add_argument("--role", default="admin", choices=sorted(ROLE_SCOPES))
    p.add_argument("--password", default="")
    p.add_argument(
        "--promote",
        action="store_true",
        help="change the role of an existing account (no password required)",
    )
    args = p.parse_args()

    password = None if args.promote else _resolve_password(args)
    if args.promote and (args.password or os.environ.get("ATE_BOOTSTRAP_PASSWORD")):
        password = _resolve_password(args)

    # The host is printed without the credentials, which are the part of a
    # connection string nobody should see echoed into a deploy log.
    print(f"database host: {settings.get_database_url().split('@')[-1]}")

    async def _run() -> int:
        async with async_session_factory() as db:
            return await _apply(db, args.username, args.role, password, args.promote)

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
