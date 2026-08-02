"""JWT token creation and verification using PyJWT.

Access tokens are short-lived (Settings.jwt_expire_minutes, default 30min).
Refresh tokens are longer-lived (7 days) with rotation: each refresh
consumes the old refresh token and issues a new pair.

Token claims validated on decode: iss, aud, exp, nbf.

Supports both HS256 (shared HMAC secret) and RS256 (RSA PEM key pair):
  - HS256: Settings.jwt_secret is used directly as the HMAC key string.
  - RS256: Settings.jwt_secret must be a PEM-encoded RSA private key;
           the public key is derived from it for verification.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization

from ate_cloud.config import settings

# Token metadata — constant issuer and audience for ATE Cloud API.
_ISSUER = "ate-cloud"
_AUDIENCE = "ate-cloud-api"

# Refresh tokens are valid for 7 days regardless of access token expiry.
_REFRESH_TOKEN_DAYS = 7

# In-memory refresh token store for rotation.
# Production should use Redis or a database table; this is sufficient for
# the login/refresh/me scope (no full user management CRUD).
_refresh_store: dict[str, str] = {}  # jti -> user_id


class TokenError(Exception):
    """Raised when a JWT token is invalid, expired, or has wrong type."""


def _load_keys() -> tuple[Any, Any]:
    """Load the signing and verification keys from Settings.jwt_secret.

    For HS256, the secret string is used directly as both the signing
    and verification key. For RS256, jwt_secret must be a PEM-encoded
    RSA private key; the public key is derived from it.

    Returns:
        Tuple of (signing_key, verification_key). For HS256 both elements
        are the secret string; for RS256 they are cryptography key objects.

    Raises:
        TokenError: If jwt_secret is not configured.
    """
    if not settings.jwt_secret:
        raise TokenError("JWT_SECRET not configured")

    algorithm = settings.jwt_algorithm
    if algorithm == "HS256":
        return settings.jwt_secret, settings.jwt_secret

    private_key = serialization.load_pem_private_key(
        settings.jwt_secret.encode(),
        password=None,
    )
    return private_key, private_key.public_key()


def create_access_token(user_id: str, scopes: list[str]) -> str:
    """Create a short-lived JWT access token.

    Args:
        user_id: User identifier (subject claim).
        scopes: Permission scopes to embed in the token.

    Returns:
        Encoded JWT string.
    """
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=settings.jwt_expire_minutes)
    payload: dict[str, Any] = {
        "sub": user_id,
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "exp": expire,
        "nbf": now,
        "iat": now,
        "jti": str(uuid.uuid4()),
        "type": "access",
        "scopes": scopes,
    }
    private_key, _ = _load_keys()
    return jwt.encode(payload, private_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: str) -> str:
    """Create a longer-lived JWT refresh token.

    The refresh token's jti is stored in-memory for rotation tracking.

    Args:
        user_id: User identifier (subject claim).

    Returns:
        Encoded JWT string.
    """
    now = datetime.now(UTC)
    expire = now + timedelta(days=_REFRESH_TOKEN_DAYS)
    jti = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "sub": user_id,
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "exp": expire,
        "nbf": now,
        "iat": now,
        "jti": jti,
        "type": "refresh",
    }
    private_key, _ = _load_keys()
    token = jwt.encode(payload, private_key, algorithm=settings.jwt_algorithm)
    store_refresh_token(jti, user_id)
    return token


def verify_token(token: str, expected_type: str = "access") -> dict[str, Any]:
    """Verify and decode a JWT token.

    Verifies iss, aud, exp, and nbf claims. The algorithm is pinned to
    settings.jwt_algorithm to prevent algorithm confusion attacks.

    Args:
        token: Encoded JWT string.
        expected_type: Expected token type ("access" or "refresh").

    Returns:
        Decoded token payload as a dict.

    Raises:
        TokenError: If the token is invalid, expired, or has wrong type.
    """
    _, public_key = _load_keys()
    try:
        payload = jwt.decode(
            token,
            public_key,
            algorithms=[settings.jwt_algorithm],
            audience=_AUDIENCE,
            issuer=_ISSUER,
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenError("Token expired") from e
    except jwt.InvalidTokenError as e:
        raise TokenError(f"Invalid token: {e}") from e

    if payload.get("type") != expected_type:
        raise TokenError(f"Expected {expected_type} token, got {payload.get('type')}")

    return payload


def store_refresh_token(jti: str, user_id: str) -> None:
    """Store a refresh token jti for rotation tracking.

    Args:
        jti: Unique token identifier from the refresh token.
        user_id: User ID the token was issued to.
    """
    _refresh_store[jti] = user_id


def consume_refresh_token(jti: str) -> str | None:
    """Consume (revoke) a refresh token for rotation.

    Removing the jti from the store ensures it cannot be reused.
    A new refresh token is issued by the caller after consumption.

    Args:
        jti: Unique token identifier to consume.

    Returns:
        The user_id if the token was valid and consumed, None if not found.
    """
    return _refresh_store.pop(jti, None)
