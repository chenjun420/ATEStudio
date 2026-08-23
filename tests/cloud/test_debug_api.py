"""Tests for debug breakpoint CRUD API endpoints.

Tests cover:
- POST /api/v1/debug/breakpoints - create returns 201, dev_mode required
- GET /api/v1/debug/breakpoints - list returns 200 with items and total
- GET /api/v1/debug/breakpoints/{bp_id} - get returns 200, nonexistent 404
- PUT /api/v1/debug/breakpoints/{bp_id} - update modifies fields, 404 handling
- DELETE /api/v1/debug/breakpoints/{bp_id} - delete returns 204, 404 for nonexistent
- Schema validation - invalid data returns 422
- DB persistence - data is actually stored and retrievable
- Dev mode enforcement - POST/PUT/DELETE return 403 when dev_mode=False
"""

import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ate_cloud.auth.password import hash_password
from ate_cloud.config import settings
from ate_cloud.models.user import User


@pytest.fixture(scope="session")
def rsa_keypair() -> rsa.RSAPrivateKey:
    """RSA keypair for RS256 JWT signing (session-scoped)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _jwt_secret(rsa_keypair: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure Settings.jwt_secret so login can issue tokens."""
    pem = rsa_keypair.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    monkeypatch.setattr(settings, "jwt_secret", pem.decode())


@pytest.fixture
async def auth_headers(client, db_session):
    """Bearer-token headers for an authenticated admin user.

    Since T17 all routers (debug included) require JWT authentication at
    mount level; tests that exercise the dev_mode 403 gate must present a
    valid token to reach the endpoint logic.
    """
    user = User(
        id=str(uuid.uuid4()),
        username=f"dbg_{uuid.uuid4().hex[:8]}",
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
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _sample_bp_data(
    session_id: str = "debug-sess-1",
    step_id: str = "step-1",
    node_id: str = "node-1",
) -> dict[str, object]:
    """Return a sample breakpoint dict for testing."""
    return {
        "session_id": session_id,
        "step_id": step_id,
        "node_id": node_id,
        "line_number": 15,
        "condition": None,
        "enabled": True,
        "node_data": {"id": node_id, "shape": "rect"},
    }


class TestCreateBreakpoint:
    """Tests for POST /api/v1/debug/breakpoints."""

    @pytest.mark.asyncio
    async def test_create_breakpoint(self, client):
        """Test creating a new breakpoint returns 201."""
        response = await client.post(
            "/api/v1/debug/breakpoints", json=_sample_bp_data()
        )

        assert response.status_code == 201
        data = response.json()
        assert data["session_id"] == "debug-sess-1"
        assert data["step_id"] == "step-1"
        assert data["node_id"] == "node-1"
        assert data["line_number"] == 15
        assert data["enabled"] is True
        assert data["node_data"] == {"id": "node-1", "shape": "rect"}
        assert "id" in data
        assert "created_at" in data
        assert "updated_at" in data

    @pytest.mark.asyncio
    async def test_create_breakpoint_minimal(self, client):
        """Test creating a breakpoint with only required fields."""
        bp_data = {
            "session_id": "sess-1",
            "step_id": "step-1",
            "node_id": "node-1",
        }
        response = await client.post("/api/v1/debug/breakpoints", json=bp_data)

        assert response.status_code == 201
        data = response.json()
        assert data["line_number"] == 0  # default
        assert data["enabled"] is True  # default
        assert data["condition"] is None
        assert data["node_data"] is None

    @pytest.mark.asyncio
    async def test_create_breakpoint_invalid(self, client):
        """Test creating a breakpoint with invalid data returns 422."""
        bp_data = {
            "session_id": "",  # Empty is invalid
            "step_id": "step-1",
            "node_id": "node-1",
        }
        response = await client.post("/api/v1/debug/breakpoints", json=bp_data)

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_breakpoint_negative_line(self, client):
        """Test creating a breakpoint with negative line_number returns 422."""
        bp_data = {
            "session_id": "sess-1",
            "step_id": "step-1",
            "node_id": "node-1",
            "line_number": -1,
        }
        response = await client.post("/api/v1/debug/breakpoints", json=bp_data)

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_breakpoint_dev_mode_required(self, client, monkeypatch, auth_headers):
        """Test creating a breakpoint returns 403 when dev_mode=False."""
        monkeypatch.setattr(settings, "dev_mode", False)

        response = await client.post(
            "/api/v1/debug/breakpoints",
            json=_sample_bp_data(),
            headers=auth_headers,
        )

        assert response.status_code == 403


class TestListBreakpoints:
    """Tests for GET /api/v1/debug/breakpoints."""

    @pytest.mark.asyncio
    async def test_list_empty(self, client):
        """Test listing breakpoints when none exist."""
        response = await client.get("/api/v1/debug/breakpoints")

        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_after_create(self, client):
        """Test listing breakpoints after creating some."""
        await client.post(
            "/api/v1/debug/breakpoints",
            json=_sample_bp_data(session_id="sess-1"),
        )
        await client.post(
            "/api/v1/debug/breakpoints",
            json=_sample_bp_data(session_id="sess-2", step_id="step-2"),
        )

        response = await client.get("/api/v1/debug/breakpoints")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    @pytest.mark.asyncio
    async def test_list_filter_by_session(self, client):
        """Test listing breakpoints filtered by session_id."""
        await client.post(
            "/api/v1/debug/breakpoints",
            json=_sample_bp_data(session_id="sess-1"),
        )
        await client.post(
            "/api/v1/debug/breakpoints",
            json=_sample_bp_data(session_id="sess-2"),
        )

        response = await client.get(
            "/api/v1/debug/breakpoints", params={"session_id": "sess-1"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["items"][0]["session_id"] == "sess-1"

    @pytest.mark.asyncio
    async def test_list_works_without_dev_mode(self, client, monkeypatch, auth_headers):
        """Test listing breakpoints works even when dev_mode=False (read-only)."""
        # First create in dev mode
        await client.post(
            "/api/v1/debug/breakpoints", json=_sample_bp_data()
        )

        # Then disable dev mode and list (authenticated: T17 mount-level JWT)
        monkeypatch.setattr(settings, "dev_mode", False)
        response = await client.get(
            "/api/v1/debug/breakpoints", headers=auth_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1


class TestGetBreakpoint:
    """Tests for GET /api/v1/debug/breakpoints/{bp_id}."""

    @pytest.mark.asyncio
    async def test_get_existing(self, client):
        """Test getting an existing breakpoint."""
        create_resp = await client.post(
            "/api/v1/debug/breakpoints", json=_sample_bp_data()
        )
        bp_id = create_resp.json()["id"]

        response = await client.get(f"/api/v1/debug/breakpoints/{bp_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == bp_id
        assert data["session_id"] == "debug-sess-1"

    @pytest.mark.asyncio
    async def test_get_not_found(self, client):
        """Test getting a nonexistent breakpoint returns 404."""
        response = await client.get(
            "/api/v1/debug/breakpoints/nonexistent-id"
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_works_without_dev_mode(self, client, monkeypatch, auth_headers):
        """Test getting a breakpoint works when dev_mode=False (read-only)."""
        create_resp = await client.post(
            "/api/v1/debug/breakpoints", json=_sample_bp_data()
        )
        bp_id = create_resp.json()["id"]

        monkeypatch.setattr(settings, "dev_mode", False)
        response = await client.get(
            f"/api/v1/debug/breakpoints/{bp_id}", headers=auth_headers
        )

        assert response.status_code == 200


class TestUpdateBreakpoint:
    """Tests for PUT /api/v1/debug/breakpoints/{bp_id}."""

    @pytest.mark.asyncio
    async def test_update_fields(self, client):
        """Test updating breakpoint fields."""
        create_resp = await client.post(
            "/api/v1/debug/breakpoints", json=_sample_bp_data()
        )
        bp_id = create_resp.json()["id"]

        update_data = {
            "line_number": 25,
            "condition": "x > 5",
            "enabled": False,
        }
        response = await client.put(
            f"/api/v1/debug/breakpoints/{bp_id}", json=update_data
        )

        assert response.status_code == 200
        data = response.json()
        assert data["line_number"] == 25
        assert data["condition"] == "x > 5"
        assert data["enabled"] is False

    @pytest.mark.asyncio
    async def test_update_partial(self, client):
        """Test partial update (only enabled field)."""
        create_resp = await client.post(
            "/api/v1/debug/breakpoints", json=_sample_bp_data()
        )
        bp_id = create_resp.json()["id"]
        original = create_resp.json()

        response = await client.put(
            f"/api/v1/debug/breakpoints/{bp_id}", json={"enabled": False}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False
        # Other fields should be unchanged
        assert data["line_number"] == original["line_number"]
        assert data["step_id"] == original["step_id"]

    @pytest.mark.asyncio
    async def test_update_not_found(self, client):
        """Test updating a nonexistent breakpoint returns 404."""
        response = await client.put(
            "/api/v1/debug/breakpoints/nonexistent-id",
            json={"enabled": False},
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_dev_mode_required(self, client, monkeypatch, auth_headers):
        """Test updating a breakpoint returns 403 when dev_mode=False."""
        create_resp = await client.post(
            "/api/v1/debug/breakpoints", json=_sample_bp_data()
        )
        bp_id = create_resp.json()["id"]

        monkeypatch.setattr(settings, "dev_mode", False)
        response = await client.put(
            f"/api/v1/debug/breakpoints/{bp_id}",
            json={"enabled": False},
            headers=auth_headers,
        )

        assert response.status_code == 403


class TestDeleteBreakpoint:
    """Tests for DELETE /api/v1/debug/breakpoints/{bp_id}."""

    @pytest.mark.asyncio
    async def test_delete_existing(self, client):
        """Test deleting an existing breakpoint returns 204."""
        create_resp = await client.post(
            "/api/v1/debug/breakpoints", json=_sample_bp_data()
        )
        bp_id = create_resp.json()["id"]

        response = await client.delete(f"/api/v1/debug/breakpoints/{bp_id}")

        assert response.status_code == 204

        # Verify it's gone
        get_resp = await client.get(f"/api/v1/debug/breakpoints/{bp_id}")
        assert get_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_not_found(self, client):
        """Test deleting a nonexistent breakpoint returns 404."""
        response = await client.delete(
            "/api/v1/debug/breakpoints/nonexistent-id"
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_dev_mode_required(self, client, monkeypatch, auth_headers):
        """Test deleting a breakpoint returns 403 when dev_mode=False."""
        create_resp = await client.post(
            "/api/v1/debug/breakpoints", json=_sample_bp_data()
        )
        bp_id = create_resp.json()["id"]

        monkeypatch.setattr(settings, "dev_mode", False)
        response = await client.delete(
            f"/api/v1/debug/breakpoints/{bp_id}", headers=auth_headers
        )

        assert response.status_code == 403


class TestDBPersistence:
    """Tests for database persistence."""

    @pytest.mark.asyncio
    async def test_create_persists_to_db(self, client, db_session):
        """Test that created breakpoints are persisted to the database."""
        from sqlalchemy import select

        from ate_cloud.models.breakpoint import Breakpoint

        await client.post(
            "/api/v1/debug/breakpoints", json=_sample_bp_data()
        )

        result = await db_session.execute(select(Breakpoint))
        breakpoints = result.scalars().all()

        assert len(breakpoints) == 1
        assert breakpoints[0].session_id == "debug-sess-1"
        assert breakpoints[0].step_id == "step-1"

    @pytest.mark.asyncio
    async def test_update_persists_to_db(self, client, db_session):
        """Test that updates are persisted to the database."""
        from sqlalchemy import select

        from ate_cloud.models.breakpoint import Breakpoint

        create_resp = await client.post(
            "/api/v1/debug/breakpoints", json=_sample_bp_data()
        )
        bp_id = create_resp.json()["id"]

        await client.put(
            f"/api/v1/debug/breakpoints/{bp_id}",
            json={"line_number": 99, "enabled": False},
        )

        result = await db_session.execute(
            select(Breakpoint).where(Breakpoint.id == bp_id)
        )
        bp = result.scalar_one()

        assert bp.line_number == 99
        assert bp.enabled is False

    @pytest.mark.asyncio
    async def test_delete_removes_from_db(self, client, db_session):
        """Test that deletion removes the row from the database."""
        from sqlalchemy import select

        from ate_cloud.models.breakpoint import Breakpoint

        create_resp = await client.post(
            "/api/v1/debug/breakpoints", json=_sample_bp_data()
        )
        bp_id = create_resp.json()["id"]

        await client.delete(f"/api/v1/debug/breakpoints/{bp_id}")

        result = await db_session.execute(
            select(Breakpoint).where(Breakpoint.id == bp_id)
        )
        bp = result.scalar_one_or_none()

        assert bp is None
