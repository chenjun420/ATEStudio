"""Authentication and authorization package.

Provides JWT token management (RS256), password hashing (Argon2 via pwdlib),
RBAC scope enforcement, and FastAPI dependencies for protected routes.
"""

from ate_cloud.auth.dependencies import get_current_user, require_scopes
from ate_cloud.auth.jwt import create_access_token, create_refresh_token, verify_token
from ate_cloud.auth.password import hash_password, verify_password
from ate_cloud.auth.rbac import ROLE_SCOPES, get_effective_scopes

__all__ = [
    "ROLE_SCOPES",
    "create_access_token",
    "create_refresh_token",
    "get_current_user",
    "get_effective_scopes",
    "hash_password",
    "require_scopes",
    "verify_password",
    "verify_token",
]
