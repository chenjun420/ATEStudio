"""Every scope a route requires must be obtainable by some account.

Why this test exists
--------------------
``require_scopes("aterag:import")`` guarded the ATERag bundle import and the
flow planner. ``aterag:import`` appeared in no role's list in ``ROLE_SCOPES``,
and the scope check in ``get_current_user`` compares against the *token's*
scopes only — which are baked at login from ``ROLE_SCOPES`` plus the
``User.scopes`` column. ``get_db_role_scopes`` is not on that path.

So every account, admin included, got 403 on the endpoints that drive the
review wizard's import, plan and commit steps. Nobody could complete the flow.
It went unnoticed because:

  * the routes registered without error,
  * the route tests mock ``require_scopes`` and so never exercise it,
  * the deploy script's end-to-end checks call a *script*, not the HTTP API,
  * the DB has no users, so login is never exercised either.

This file closes that by deriving the required scopes from the routes
themselves rather than restating them, so a newly guarded endpoint fails here
until somebody grants the scope on purpose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ate_cloud.auth.rbac import ROLE_SCOPES, SCOPE_ATERAG_IMPORT, get_effective_scopes

API_DIR = Path(__file__).resolve().parents[2] / "src" / "ate_cloud" / "api" / "v1"

#: ``require_scopes("a", "b")`` — all listed scopes are required together.
_CALL = re.compile(r'require_scopes\(([^)]*)\)')
_STRING = re.compile(r'"([^"]+)"')


def _required_scopes() -> set[str]:
    """Every scope named by a ``require_scopes`` call, read from the source."""
    found: set[str] = set()
    for path in sorted(API_DIR.glob("*.py")):
        for call in _CALL.findall(path.read_text(encoding="utf-8")):
            found.update(_STRING.findall(call))
    return found


class TestNoRouteAsksForAnUngrantableScope:
    def test_every_required_scope_is_granted_to_some_role(self) -> None:
        required = _required_scopes()
        # Guard the guard: if this breaks, the test below would pass vacuously.
        assert required, "no require_scopes() found — the parser or the routes moved"

        grantable = {s for scopes in ROLE_SCOPES.values() for s in scopes}
        unreachable = required - grantable
        assert not unreachable, (
            f"这些 scope 被端点要求却没有任何角色拥有, 端点对所有人都是 403: "
            f"{sorted(unreachable)}"
        )

    def test_the_scan_actually_finds_the_import_scope(self) -> None:
        """Pins the specific regression, not just the general property.

        If the parser silently matched nothing, the test above would go green
        on an empty set. This asserts the scan sees the scope that was missing.
        """
        assert SCOPE_ATERAG_IMPORT in _required_scopes()

    def test_aterag_import_is_not_granted_to_operators(self) -> None:
        """An import overwrites authoritative spec-derived rows.

        That is not the same act as editing one record, so it must not ride in
        on the generic ``write`` scope. Only admin carries it.
        """
        assert SCOPE_ATERAG_IMPORT in ROLE_SCOPES["admin"]
        for operator_role in ("write", "read", "execute"):
            assert SCOPE_ATERAG_IMPORT not in ROLE_SCOPES[operator_role], operator_role


class TestScopesReachTheToken:
    """The dict is only half the story — it has to survive into the token."""

    def test_admin_token_carries_the_import_scope(self) -> None:
        # This is exactly what a login on the board produced before the fix:
        # ['admin', 'execute', 'read', 'write'] and a 403 on import.
        scopes = get_effective_scopes("admin", None)
        assert SCOPE_ATERAG_IMPORT in scopes

    def test_register_default_role_cannot_import(self) -> None:
        """``/auth/register`` hardcodes role="read"; the wizard is then blocked.

        Not a bug in the scope table — bootstrapping an admin is a separate,
        deliberate step (``scripts/bootstrap_admin.py``). Asserted so the
        interaction stays visible: fixing the table alone does not make the
        flow reachable for a self-registered account.
        """
        assert SCOPE_ATERAG_IMPORT not in get_effective_scopes("read", None)

    def test_explicit_scope_column_still_wins(self) -> None:
        """A hand-granted scope must not be dropped by the role lookup."""
        scopes = get_effective_scopes("read", [SCOPE_ATERAG_IMPORT])
        assert SCOPE_ATERAG_IMPORT in scopes

    @pytest.mark.parametrize("role", sorted(ROLE_SCOPES))
    def test_every_role_is_self_consistent(self, role: str) -> None:
        """A role's scopes must not shrink when combined with itself.

        Catches a refactor that rebuilds the list instead of extending it.
        """
        assert set(ROLE_SCOPES[role]) <= set(get_effective_scopes(role, None))
