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
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

ROLE_SCOPES: dict[str, list[str]] = {
    "admin": ["admin", "read", "write", "execute"],
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
