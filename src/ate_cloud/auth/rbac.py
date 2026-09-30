"""Role-Based Access Control scope definitions.

Maps user roles to permission scopes. Scopes are encoded into JWT tokens
at login time and enforced via FastAPI SecurityScopes in protected routes.

Roles:
    admin   — full access (all scopes)
    write   — read + write access
    read    — read-only access
    execute — execution access only (no read/write on resources)

When the database is seeded with Role/Permission records, the async
functions query the DB for up-to-date scopes. If the DB is not seeded
(or the role is not found), they fall back to the hardcoded ROLE_SCOPES
defaults for backward compatibility.

The fallback is the one that actually runs
-----------------------------------------
``get_current_user`` compares the required scope against the *token's* scopes,
and those are baked at login by :func:`get_effective_scopes` — which consults
only this dict plus the ``User.scopes`` column. ``get_db_role_scopes`` exists
but is not on the login path, so seeding the Role table does not change who can
call what. Anything a route requires must therefore appear here, or the route is
unreachable.

That is not hypothetical: ``require_scopes("aterag:import")`` guards the ATERag
bundle import and the flow planner, and ``aterag:import`` was in no role's list.
Every account, admin included, got 403 — so the review wizard's steps 1, 4 and 5
could not be completed by anyone. The token check being the *only* check is what
hid it: the endpoints registered fine, the tests mocked the dependency, and the
deploy passed.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: Scope gating ingestion of an ATERag bundle. Kept separate from ``write`` on
#: purpose: an import overwrites authoritative spec-derived rows and can
#: invalidate already-reviewed conditions, which is not the same act as editing
#: one record. The import module's own note is "operator roles cannot ingest", so
#: only ``admin`` carries it.
SCOPE_ATERAG_IMPORT = "aterag:import"

#: The scope that means "no permission check applies".
#:
#: It exists because permission checks in this codebase come in two flavours and
#: only one of them consults :data:`ROLE_SCOPES`. Endpoint guards say
#: ``require_scopes("x")``; data-driven guards say "this row declares
#: ``required_permissions``". The second kind compares against strings that live
#: in the *data* — ``app_menus.required_permissions`` holds values like
#: ``system:read`` and ``node:read``, which no role in :data:`ROLE_SCOPES` grants.
#:
#: Left unhandled, that makes the whole product unreachable: every seeded menu
#: was filtered out for every account, admin included, and the main screen came
#: up empty after a successful login. An admin who satisfies no permission check
#: is not an admin.
SCOPE_ADMIN = "admin"


def is_superuser(scopes: set[str] | frozenset[str] | list[str] | None) -> bool:
    """Whether these scopes bypass permission-gated filtering entirely.

    For guards whose permission vocabulary is data-driven and therefore cannot be
    enumerated in :data:`ROLE_SCOPES`. Where a guard names a scope explicitly,
    prefer listing that scope on the role: an explicit grant is auditable, a
    wildcard is not, and overusing this turns it into a way to silence checks
    rather than satisfy them.
    """
    return bool(scopes) and SCOPE_ADMIN in scopes


ROLE_SCOPES: dict[str, list[str]] = {
    "admin": ["admin", "read", "write", "execute", SCOPE_ATERAG_IMPORT],
    "write": ["read", "write"],
    "read": ["read"],
    "execute": ["execute"],
}


def get_role_scopes(role: str) -> list[str]:
    """Return the scopes granted by a role (hardcoded fallback).

    Args:
        role: Role name (admin/read/write/execute).

    Returns:
        List of scope strings. Empty for unknown roles.
    """
    return ROLE_SCOPES.get(role, [])


def get_effective_scopes(role: str, explicit_scopes: list[str] | None) -> list[str]:
    """Compute the effective scopes for a user (hardcoded fallback).

    Combines role-based scopes with any explicitly granted scopes.

    Args:
        role: User's role.
        explicit_scopes: Optional additional scopes from the User.scopes field.

    Returns:
        Sorted list of unique scope strings.
    """
    scopes = set(get_role_scopes(role))
    if explicit_scopes:
        scopes.update(explicit_scopes)
    return sorted(scopes)


async def get_db_role_scopes(role: str, db: AsyncSession) -> list[str]:
    """Return the scopes granted by a role, querying the database.

    Queries the Role table for an active role matching the given name.
    If found, returns the role's permission codes. If not found (e.g.
    the DB is not seeded), falls back to the hardcoded ROLE_SCOPES.

    Args:
        role: Role name (admin/read/write/execute or custom).
        db: Async database session.

    Returns:
        List of scope/permission strings. Empty for unknown roles.
    """
    # Import here to avoid circular import at module load time.
    from ate_cloud.models.rbac import Role

    result = await db.execute(
        select(Role).where(Role.name == role, Role.is_active.is_(True))
    )
    db_role = result.scalar_one_or_none()

    if db_role is not None and db_role.permissions:
        return list(db_role.permissions)

    return get_role_scopes(role)


async def get_db_effective_scopes(
    role: str, explicit_scopes: list[str] | None, db: AsyncSession
) -> list[str]:
    """Compute the effective scopes for a user using DB-driven role data.

    Combines DB-queried role scopes with any explicitly granted scopes.
    Falls back to hardcoded defaults if the role is not in the database.

    Args:
        role: User's role.
        explicit_scopes: Optional additional scopes from the User.scopes field.
        db: Async database session.

    Returns:
        Sorted list of unique scope strings.
    """
    scopes = set(await get_db_role_scopes(role, db))
    if explicit_scopes:
        scopes.update(explicit_scopes)
    return sorted(scopes)
