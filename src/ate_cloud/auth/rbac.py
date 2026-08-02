"""Role-Based Access Control scope definitions.

Maps user roles to permission scopes. Scopes are encoded into JWT tokens
at login time and enforced via FastAPI SecurityScopes in protected routes.

Roles:
    admin   — full access (all scopes)
    write   — read + write access
    read    — read-only access
    execute — execution access only (no read/write on resources)
"""

ROLE_SCOPES: dict[str, list[str]] = {
    "admin": ["admin", "read", "write", "execute"],
    "write": ["read", "write"],
    "read": ["read"],
    "execute": ["execute"],
}


def get_role_scopes(role: str) -> list[str]:
    """Return the scopes granted by a role.

    Args:
        role: Role name (admin/read/write/execute).

    Returns:
        List of scope strings. Empty for unknown roles.
    """
    return ROLE_SCOPES.get(role, [])


def get_effective_scopes(role: str, explicit_scopes: list[str] | None) -> list[str]:
    """Compute the effective scopes for a user.

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
