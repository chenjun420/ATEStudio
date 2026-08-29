"""Auth-enforcement tests for all cloud API routers (T17 v41-gap-analysis).

For EVERY router mounted via ``_PROTECTED_ROUTERS`` in
``src/ate_cloud/api/v1/router.py``:

- anonymous request (no Authorization header) -> 401
- request with a valid bearer token           -> 200 / 4xx-not-401

Plus regression guards:
- auth login/register remain anonymous (token acquisition must work)
- health endpoints stay open (Docker healthcheck depends on them)
- dev-mode bypass still works (ATE_DEV_MODE=true)
- invalid/expired tokens are rejected with 401

Service-backed endpoints (workers/faults/diagnose/workflows/scripts_generate)
use FastAPI ``dependency_overrides`` with lightweight fakes so the with-token
assertion stays deterministic (no NATS/Neo4j/LLM required).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ate_cloud.auth.password import hash_password
from ate_cloud.config import settings
from ate_cloud.models.user import User

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def rsa_keypair() -> rsa.RSAPrivateKey:
    """Generate a 2048-bit RSA keypair for RS256 JWT signing (session-scoped)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwt_secret(rsa_keypair: rsa.RSAPrivateKey) -> str:
    """PEM-encoded RSA private key used as Settings.jwt_secret."""
    pem = rsa_keypair.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode()


@pytest.fixture(autouse=True)
def _auth_mode(jwt_secret: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable dev mode and configure the JWT secret.

    Overrides the cloud conftest's session-scoped dev_mode=True bypass so
    these tests exercise real token verification.
    """
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "jwt_secret", jwt_secret)


async def _create_admin_user(db_session: Any) -> User:
    """Insert an active admin user into the test database."""
    user = User(
        id=str(uuid.uuid4()),
        username=f"admin_{uuid.uuid4().hex[:8]}",
        password_hash=hash_password("secret123"),
        role="admin",
        scopes=None,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _login_token(client: Any, db_session: Any) -> str:
    """Create an admin user and obtain a valid access token via POST /auth/login."""
    user = await _create_admin_user(db_session)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": user.username, "password": "secret123"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Uniform matrix: one entry per protected router with a deterministic
# with-token expectation (DB-backed list -> 200; unknown-id lookup -> 404).
# ---------------------------------------------------------------------------

ROUTER_MATRIX = [
    # (router label, method, path, expected status WITH valid token)
    ("node_templates", "GET", "/api/v1/node-templates", 200),
    ("scripts", "GET", "/api/v1/scripts", 200),
    ("sequences", "GET", "/api/v1/sequences", 200),
    ("executions", "GET", "/api/v1/executions", 200),
    ("debug", "GET", "/api/v1/debug/breakpoints", 200),
    ("changeover", "GET", "/api/v1/changeover/products", 200),
    ("dashboard", "GET", "/api/v1/dashboard/summary", 200),
    ("resources", "GET", "/api/v1/resources/humans", 200),
    ("reports", "GET", "/api/v1/reports/atml/nonexistent-id", 404),
    ("node_flow_bindings", "GET", "/api/v1/node-flow-bindings", 200),
    ("calibrations", "GET", "/api/v1/calibrations", 200),
    ("fixtures", "GET", "/api/v1/fixtures", 200),
    ("limits", "GET", "/api/v1/limits", 200),
    # offline status: valid token passes auth; provider unconfigured in test
    # app -> honest 503 (non-401 proves the mount-level JWT gate passed).
    ("offline", "GET", "/api/v1/offline/status", 503),
    ("operator_checkpoints", "GET", "/api/v1/executions/nope/checkpoint/pending", 404),
    ("products", "GET", "/api/v1/products", 200),
    ("recordings", "GET", "/api/v1/executions/nope/recording", 404),
    ("spc", "GET", "/api/v1/spc/alerts", 200),
    ("trace", "GET", "/api/v1/trace/UNKNOWN-SERIAL", 404),
]


class TestAnonymousRequestsRejected:
    """Every protected router must reject unauthenticated requests with 401."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label,method,path,_expected", ROUTER_MATRIX)
    async def test_anonymous_gets_401(
        self, client, label: str, method: str, path: str, _expected: int
    ) -> None:
        response = await client.request(method, path)
        assert response.status_code == 401, (
            f"{label}: {method} {path} returned {response.status_code} "
            "for an anonymous request"
        )

    @pytest.mark.asyncio
    async def test_workers_anonymous_401(self, client) -> None:
        response = await client.get("/api/v1/workers")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_faults_anonymous_401(self, client) -> None:
        response = await client.post("/api/v1/faults/seed")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_diagnose_anonymous_401(self, client) -> None:
        response = await client.post("/api/v1/diagnose/x/feedback", json={})
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_workflows_anonymous_401(self, client) -> None:
        response = await client.get("/api/v1/workflows/some-id")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_scripts_generate_anonymous_401(self, client) -> None:
        response = await client.post("/api/v1/scripts/generate", json={})
        assert response.status_code == 401


class TestValidTokenAccepted:
    """With a valid bearer token every protected router answers non-401."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label,method,path,expected", ROUTER_MATRIX)
    async def test_valid_token_non_401(
        self, client, db_session, label: str, method: str, path: str, expected: int
    ) -> None:
        token = await _login_token(client, db_session)
        response = await client.request(
            method, path, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code != 401, (
            f"{label}: valid token was rejected: {response.text}"
        )
        assert response.status_code == expected, (
            f"{label}: expected {expected}, got {response.status_code}: "
            f"{response.text[:300]}"
        )

    @pytest.mark.asyncio
    async def test_workers_with_token_200(self, client, db_session) -> None:
        """Workers list returns 200 with a fake registry service injected."""
        from ate_cloud.api.v1.workers import _get_worker_service

        class _FakeService:
            async def list_workers(self) -> list[dict[str, Any]]:
                return []

        client.app.dependency_overrides[_get_worker_service] = lambda: _FakeService()
        token = await _login_token(client, db_session)

        response = await client.get(
            "/api/v1/workers", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200
        assert response.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_faults_seed_with_token_200(self, client, db_session) -> None:
        """Fault seeding returns 200 with a fake KG seeder injected."""
        from ate_cloud.api.v1.faults import _get_kg_seeder

        class _FakeSeeder:
            async def seed_all(self) -> dict[str, int]:
                return {"nodes_created": 0, "relationships_created": 0}

        client.app.dependency_overrides[_get_kg_seeder] = lambda: _FakeSeeder()
        token = await _login_token(client, db_session)

        response = await client.post(
            "/api/v1/faults/seed", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200
        assert response.json()["nodes_created"] == 0

    @pytest.mark.asyncio
    async def test_diagnose_feedback_with_token_422(self, client, db_session) -> None:
        """Diagnose feedback with valid token but empty body -> 422 (auth passed)."""
        from ate_cloud.api.v1.diagnose import _get_diagnosis_service

        class _FakeService:
            pass  # never reached: body validation fails first

        client.app.dependency_overrides[_get_diagnosis_service] = lambda: _FakeService()
        token = await _login_token(client, db_session)

        response = await client.post(
            "/api/v1/diagnose/x/feedback",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_workflows_with_token_404(self, client, db_session) -> None:
        """Workflow lookup with valid token and fake orchestrator -> 404."""
        from ate_cloud.api.v1.workflows import _get_orchestrator

        class _FakeOrchestrator:
            async def connect(self) -> None:
                return None

            async def disconnect(self) -> None:
                return None

            async def get_workflow(self, workflow_id: str) -> None:
                return None

        client.app.dependency_overrides[_get_orchestrator] = lambda: _FakeOrchestrator()
        token = await _login_token(client, db_session)

        response = await client.get(
            "/api/v1/workflows/missing-id",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_scripts_generate_with_token_422(self, client, db_session) -> None:
        """Script generation with valid token but empty body -> 422 (auth passed)."""
        from ate_cloud.api.v1.scripts_generate import _get_script_generator

        class _FakeGenerator:
            pass  # never reached: body validation fails first

        client.app.dependency_overrides[_get_script_generator] = (
            lambda: _FakeGenerator()
        )
        token = await _login_token(client, db_session)

        response = await client.post(
            "/api/v1/scripts/generate",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Regression guards: exemptions and bypass semantics
# ---------------------------------------------------------------------------


class TestExemptionsStillWork:
    """Token-acquisition and health endpoints must stay reachable anonymously."""

    @pytest.mark.asyncio
    async def test_login_remains_anonymous(self, client, db_session) -> None:
        """POST /auth/login works without any Authorization header."""
        user = await _create_admin_user(db_session)

        response = await client.post(
            "/api/v1/auth/login",
            json={"username": user.username, "password": "secret123"},
        )

        assert response.status_code == 200
        assert "access_token" in response.json()

    @pytest.mark.asyncio
    async def test_register_remains_anonymous(self, client) -> None:
        """POST /auth/register works without any Authorization header."""
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "username": f"newuser_{uuid.uuid4().hex[:8]}",
                "password": "pass12345",
            },
        )

        assert response.status_code == 201
        assert "access_token" in response.json()

    @pytest.mark.asyncio
    async def test_health_db_stays_open(self, client) -> None:
        """GET /health/db is never 401 (Docker healthcheck has no credentials)."""
        response = await client.get("/api/v1/health/db")
        assert response.status_code != 401

    @pytest.mark.asyncio
    async def test_health_nats_stays_open(self, client) -> None:
        """GET /health/nats is never 401 (503 expected without NATS)."""
        response = await client.get("/api/v1/health/nats")
        assert response.status_code != 401


class TestTokenValidationSemantics:
    """Bad tokens are rejected; dev-mode bypass still functions."""

    @pytest.mark.asyncio
    async def test_garbage_token_rejected(self, client) -> None:
        response = await client.get(
            "/api/v1/executions",
            headers={"Authorization": "Bearer not.a.real.token"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, client, db_session, rsa_keypair) -> None:
        """An RS256-signed but expired access token gets 401."""
        user = await _create_admin_user(db_session)
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
        expired = jwt.encode(payload, rsa_keypair, algorithm="RS256")

        response = await client.get(
            "/api/v1/executions",
            headers={"Authorization": f"Bearer {expired}"},
        )

        assert response.status_code == 401
        assert "expired" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_auth_me_requires_token(self, client) -> None:
        """GET /auth/me keeps its endpoint-level protection."""
        response = await client.get("/api/v1/auth/me")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_dev_mode_bypass_still_works(self, client, monkeypatch) -> None:
        """ATE_DEV_MODE=true still grants anonymous access (documented bypass)."""
        monkeypatch.setattr(settings, "dev_mode", True)

        response = await client.get("/api/v1/executions")

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Mount-level sanity: the central mechanism actually wired on the app
# ---------------------------------------------------------------------------


def test_all_protected_mounts_carry_security_dependency() -> None:
    """Every mount in _PROTECTED_ROUTERS carries the auth dependency at include time.

    FastAPI >=0.139 keeps per-include dependencies on
    ``_IncludedRouter.include_context`` rather than flattening routes eagerly.
    """
    from ate_cloud.api.v1 import router as router_module

    def _dep_names(entry: Any) -> set[str]:
        ctx = getattr(entry, "include_context", None)
        return {
            getattr(d, "dependency", None).__name__ or ""
            for d in (getattr(ctx, "dependencies", None) or [])
        }

    def _mounted(entry: Any) -> Any:
        return entry.include_context.included_router

    mounts = list(router_module.api_router.routes)
    protected = [m for m in mounts if "get_current_user" in _dep_names(m)]
    anonymous = [m for m in mounts if not _dep_names(m)]

    # 25 protected mounts vs 5 exempt/already-protected mounts
    # (health, auth, users, rbac, apps). The 25th is the RH-6 checkpoint-id
    # ack alias router (POST /checkpoints/{checkpoint_id}/ack), mounted with
    # the same get_current_user guard; its anonymous-401 is also covered in
    # test_checkpoint_id_ack.py.
    assert len(protected) == 25
    assert len(anonymous) == 5

    anonymous_routers = [_mounted(m) for m in anonymous]
    assert router_module.health_router in anonymous_routers
    assert router_module.auth_router in anonymous_routers
    assert router_module.users_router in anonymous_routers
    assert router_module.rbac_router in anonymous_routers
    assert router_module.apps_router in anonymous_routers
