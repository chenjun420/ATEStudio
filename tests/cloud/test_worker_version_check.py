"""Tests for the worker version-check endpoint and WorkerVersionCheckResponse.

Covers T25 of the v41 gap-analysis plan:
- ``WorkerVersionCheckResponse`` validates a known payload (the shape
  ``ScriptVersioningService.check_worker_version`` produces).
- Malformed internal payloads fail model validation.
- ``GET /api/v1/workers/{worker_id}/version-check`` returns the documented
  schema (happy path, empty-diff path, NATS-unavailable path).
- The OpenAPI document includes the ``WorkerVersionCheckResponse`` model.
"""

from __future__ import annotations

from typing import Any

import pytest

from ate_cloud.schemas.script import WorkerVersionCheckResponse, WorkerVersionDiff

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

KNOWN_DIFFS: list[dict[str, str | bool]] = [
    {
        "script_path": "scripts/a.py",
        "tagged_hash": "aaa111",
        "current_hash": "aaa111",
        "needs_update": False,
    },
    {
        "script_path": "scripts/b.py",
        "tagged_hash": "bbb222",
        "current_hash": "ccc333",
        "needs_update": True,
    },
]


class _FakeVersioning:
    """Stand-in for ScriptVersioningService returning canned diffs."""

    def __init__(self, diffs: list[dict[str, str | bool]]) -> None:
        self._diffs = diffs

    async def check_worker_version(
        self, worker_id: str, js: Any
    ) -> list[dict[str, str | bool]]:
        return self._diffs


def _override_versioning(client: Any, diffs: list[dict[str, str | bool]]) -> None:
    from ate_cloud.api.v1.workers import _get_versioning_service

    client.app.dependency_overrides[_get_versioning_service] = lambda: _FakeVersioning(
        diffs
    )


@pytest.fixture
def fake_nc(client: Any) -> Any:
    """Attach a minimal NATS client stub to app.state (jetstream() only)."""
    client.app.state.nc = type("NC", (), {"jetstream": staticmethod(lambda: object())})()
    return client.app.state.nc


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestWorkerVersionCheckResponseSchema:
    """Schema-level tests for WorkerVersionCheckResponse."""

    def test_schema_validates_known_payload(self) -> None:
        """The service's diff shape validates into the response model."""
        resp = WorkerVersionCheckResponse.model_validate(
            {"worker_id": "w1", "scripts": KNOWN_DIFFS}
        )
        assert resp.worker_id == "w1"
        assert len(resp.scripts) == 2
        assert resp.scripts[0].script_path == "scripts/a.py"
        assert resp.scripts[0].needs_update is False
        assert resp.scripts[1].needs_update is True
        assert resp.scripts[1].current_hash == "ccc333"

    def test_schema_empty_scripts_allowed(self) -> None:
        """A worker with no tags yields an empty scripts list."""
        resp = WorkerVersionCheckResponse(worker_id="w2", scripts=[])
        assert resp.scripts == []

    def test_schema_rejects_invalid_payload(self) -> None:
        """Malformed internal payload fails validation (missing fields)."""
        with pytest.raises(Exception):  # noqa: B017, PT011 — pydantic ValidationError
            WorkerVersionCheckResponse.model_validate(
                {
                    "worker_id": "w1",
                    "scripts": [
                        {
                            "script_path": "a.py",
                            # missing tagged_hash / current_hash / needs_update
                        }
                    ],
                }
            )

    def test_diff_model_roundtrip(self) -> None:
        """WorkerVersionDiff round-trips through model_dump."""
        diff = WorkerVersionDiff(
            script_path="x.py",
            tagged_hash="h1",
            current_hash="h2",
            needs_update=True,
        )
        dumped = diff.model_dump()
        assert WorkerVersionDiff.model_validate(dumped) == diff


# ---------------------------------------------------------------------------
# Endpoint contract
# ---------------------------------------------------------------------------


class TestWorkerVersionCheckEndpoint:
    """HTTP contract for GET /api/v1/workers/{worker_id}/version-check."""

    @pytest.mark.asyncio
    async def test_endpoint_returns_documented_schema(self, client, fake_nc) -> None:
        """Happy path: endpoint returns the documented schema."""
        _override_versioning(client, KNOWN_DIFFS)

        resp = await client.get("/api/v1/workers/worker-001/version-check")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["worker_id"] == "worker-001"
        assert len(body["scripts"]) == 2
        assert body["scripts"][1]["needs_update"] is True
        # Response must validate against the schema it documents.
        WorkerVersionCheckResponse.model_validate(body)

    @pytest.mark.asyncio
    async def test_endpoint_empty_diffs(self, client, fake_nc) -> None:
        """No tags → 200 with an empty scripts list."""
        _override_versioning(client, [])

        resp = await client.get("/api/v1/workers/worker-001/version-check")

        assert resp.status_code == 200
        assert resp.json() == {"worker_id": "worker-001", "scripts": []}

    @pytest.mark.asyncio
    async def test_endpoint_nats_unavailable_503(self, client) -> None:
        """Missing NATS client on app.state → 503 (fail-fast convention)."""
        _override_versioning(client, KNOWN_DIFFS)
        if hasattr(client.app.state, "nc"):
            del client.app.state.nc

        resp = await client.get("/api/v1/workers/worker-001/version-check")

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_endpoint_with_jwt_token(
        self, client, db_session, fake_nc, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With real JWT semantics: valid token reaches the endpoint."""
        import uuid

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        from ate_cloud.auth.password import hash_password
        from ate_cloud.config import settings
        from ate_cloud.models.user import User

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        monkeypatch.setattr(settings, "jwt_secret", pem)

        user = User(
            id=str(uuid.uuid4()),
            username=f"vc_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("pw123456"),
            role="admin",
            scopes=None,
            is_active=True,
        )
        db_session.add(user)
        await db_session.flush()

        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": user.username, "password": "pw123456"},
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]

        _override_versioning(client, KNOWN_DIFFS)
        resp = await client.get(
            "/api/v1/workers/worker-001/version-check",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["worker_id"] == "worker-001"


# ---------------------------------------------------------------------------
# OpenAPI documentation
# ---------------------------------------------------------------------------


class TestOpenAPIDocumentation:
    """The version-check endpoint must be documented in OpenAPI."""

    def test_openapi_includes_model(self, app) -> None:
        """WorkerVersionCheckResponse appears in the OpenAPI schemas."""
        schema = app.openapi()["components"]["schemas"]["WorkerVersionCheckResponse"]
        props = schema["properties"]
        assert "worker_id" in props
        assert "scripts" in props
        assert props["scripts"]["items"]["$ref"].endswith("WorkerVersionDiff")

    def test_openapi_endpoint_references_response_model(self, app) -> None:
        """The version-check operation references the response model."""
        openapi = app.openapi()
        op = openapi["paths"]["/api/v1/workers/{worker_id}/version-check"]["get"]
        ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("WorkerVersionCheckResponse")
