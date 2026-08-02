"""Tests for JWT authentication and RBAC authorization.

Tests:
    - Login success (valid credentials → tokens returned)
    - Login failure (wrong password → 401)
    - Token verification (valid token → 200 on protected route)
    - No token (missing Authorization header → 401)
    - Token expired (expired access token → 401)
    - RBAC scope denied (user without "read" scope → 403)
    - Dev mode bypass (dev_mode=True → 200 without token)
    - Refresh token rotation (old refresh token revoked after use)
    - GET /auth/me returns current user
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ate_cloud.auth.password import hash_password
from ate_cloud.config import settings
from ate_cloud.models.user import User

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def rsa_keypair() -> rsa.RSAPrivateKey:
    """Generate a 2048-bit RSA keypair for RS256 JWT signing (session-scoped)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwt_secret(rsa_keypair: rsa.RSAPrivateKey) -> str:
    """Return the PEM-encoded RSA private key for Settings.jwt_secret."""
    pem = rsa_keypair.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode()


@pytest.fixture(autouse=True)
def _auth_mode(jwt_secret: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable dev mode and configure JWT secret for auth tests.

    Overrides the cloud conftest's dev_mode bypass (which sets dev_mode=True).
    """
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "jwt_secret", jwt_secret)


async def _create_user(
    db_session,
    username: str,
    password: str,
    role: str,
    scopes: list[str] | None = None,
) -> User:
    """Insert a user into the test database and return it."""
    user = User(
        id=str(uuid.uuid4()),
        username=username,
        password_hash=hash_password(password),
        role=role,
        scopes=scopes,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    return user


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLogin:
    """Tests for POST /api/v1/auth/login."""

    @pytest.mark.asyncio
    async def test_login_success(self, client, db_session):
        """Valid credentials return access and refresh tokens."""
        await _create_user(db_session, "admin_user", "secret123", "admin")

        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin_user", "password": "secret123"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "Bearer"
        assert data["expires_in"] == settings.jwt_expire_minutes * 60

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, client, db_session):
        """Wrong password returns 401."""
        await _create_user(db_session, "admin_user", "secret123", "admin")

        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin_user", "password": "wrong"},
        )

        assert response.status_code == 401
        assert "Invalid" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_login_nonexistent_user(self, client):
        """Login with unknown user returns 401."""
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "ghost", "password": "anything"},
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_login_inactive_user(self, client, db_session):
        """Inactive user cannot login."""
        user = await _create_user(db_session, "inactive", "pass", "read")
        user.is_active = False
        await db_session.flush()

        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "inactive", "password": "pass"},
        )

        assert response.status_code == 401


class TestTokenVerification:
    """Tests for protected route access with/without tokens."""

    @pytest.mark.asyncio
    async def test_protected_route_no_token(self, client):
        """Request without Authorization header returns 401."""
        response = await client.get("/api/v1/sequences")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_protected_route_valid_token(self, client, db_session):
        """Request with valid token returns 200."""
        await _create_user(db_session, "admin_user", "secret123", "admin")

        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin_user", "password": "secret123"},
        )
        token = login_resp.json()["access_token"]

        response = await client.get(
            "/api/v1/sequences",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_protected_route_invalid_token(self, client):
        """Request with garbage token returns 401."""
        response = await client.get(
            "/api/v1/sequences",
            headers={"Authorization": "Bearer not.a.real.token"},
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_token_expired(self, client, db_session, rsa_keypair):
        """Expired access token returns 401."""
        user = await _create_user(db_session, "admin_user", "secret123", "admin")

        now = datetime.now(UTC)
        payload = {
            "sub": user.id,
            "iss": "ate-cloud",
            "aud": "ate-cloud-api",
            "exp": now - timedelta(minutes=5),
            "nbf": now - timedelta(minutes=35),
            "iat": now - timedelta(minutes=35),
            "jti": str(uuid.uuid4()),
            "type": "access",
            "scopes": ["admin", "read", "write", "execute"],
        }
        expired_token = jwt.encode(payload, rsa_keypair, algorithm="RS256")

        response = await client.get(
            "/api/v1/sequences",
            headers={"Authorization": f"Bearer {expired_token}"},
        )

        assert response.status_code == 401
        assert "expired" in response.json()["detail"].lower()


class TestRBAC:
    """Tests for RBAC scope enforcement."""

    @pytest.mark.asyncio
    async def test_scope_denied(self, client, db_session):
        """User without 'read' scope gets 403 on protected route."""
        # "execute" role only has ["execute"] scope, not "read".
        await _create_user(db_session, "operator", "pass123", "execute")

        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "operator", "password": "pass123"},
        )
        token = login_resp.json()["access_token"]

        response = await client.get(
            "/api/v1/sequences",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403
        assert "Insufficient" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_read_role_can_access(self, client, db_session):
        """User with 'read' role can access protected route."""
        await _create_user(db_session, "reader", "pass123", "read")

        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "reader", "password": "pass123"},
        )
        token = login_resp.json()["access_token"]

        response = await client.get(
            "/api/v1/sequences",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200


class TestDevModeBypass:
    """Tests for ATE_DEV_MODE auth bypass."""

    @pytest.mark.asyncio
    async def test_dev_mode_bypasses_auth(self, client, monkeypatch):
        """With dev_mode=True, protected routes return 200 without token."""
        monkeypatch.setattr(settings, "dev_mode", True)

        response = await client.get("/api/v1/sequences")

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_dev_mode_bypasses_health(self, client, monkeypatch):
        """Health endpoints remain accessible in dev mode (and always)."""
        monkeypatch.setattr(settings, "dev_mode", True)

        response = await client.get("/api/v1/health/db")

        # 503 is acceptable (DB might not be connected in test), but NOT 401.
        assert response.status_code != 401


class TestRefreshToken:
    """Tests for refresh token rotation."""

    @pytest.mark.asyncio
    async def test_refresh_returns_new_tokens(self, client, db_session):
        """Refresh endpoint returns new access + refresh tokens."""
        await _create_user(db_session, "admin_user", "secret123", "admin")

        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin_user", "password": "secret123"},
        )
        refresh_token = login_resp.json()["refresh_token"]

        refresh_resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )

        assert refresh_resp.status_code == 200
        data = refresh_resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["refresh_token"] != refresh_token

    @pytest.mark.asyncio
    async def test_refresh_rotation_revokes_old_token(self, client, db_session):
        """Old refresh token is revoked after use (rotation)."""
        await _create_user(db_session, "admin_user", "secret123", "admin")

        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin_user", "password": "secret123"},
        )
        old_refresh = login_resp.json()["refresh_token"]

        # First refresh succeeds.
        resp1 = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": old_refresh},
        )
        assert resp1.status_code == 200

        # Second use of the same (now consumed) refresh token fails.
        resp2 = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": old_refresh},
        )
        assert resp2.status_code == 401


class TestMeEndpoint:
    """Tests for GET /api/v1/auth/me."""

    @pytest.mark.asyncio
    async def test_me_returns_user_info(self, client, db_session):
        """GET /auth/me returns the authenticated user's info."""
        await _create_user(db_session, "admin_user", "secret123", "admin")

        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin_user", "password": "secret123"},
        )
        token = login_resp.json()["access_token"]

        response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "admin_user"
        assert data["role"] == "admin"
        assert data["is_active"] is True

    @pytest.mark.asyncio
    async def test_me_no_token(self, client):
        """GET /auth/me without token returns 401."""
        response = await client.get("/api/v1/auth/me")
        assert response.status_code == 401
