"""Tests for script content and versioning API endpoints.

Uses httpx AsyncClient with ASGITransport to test the FastAPI endpoints.
Each test creates a temporary Git repository for script file isolation.
All tests use SQLite in-memory database configured in conftest.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ate_cloud.db import get_db
from ate_cloud.main import create_app
from ate_cloud.models import Base
from ate_cloud.nats.sse_bridge import SSEBridge
from ate_cloud.services.script_versioning import ScriptVersioningService

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def test_engine() -> AsyncEngine:
    """Create a test database engine with SQLite in-memory database."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncSession:
    """Create a test database session."""
    factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
def script_versioning(tmp_path: Path) -> ScriptVersioningService:
    """Create a ScriptVersioningService with a temporary Git repo."""
    return ScriptVersioningService(scripts_root=tmp_path)


@pytest.fixture
def app(db_session: AsyncSession, script_versioning: ScriptVersioningService) -> FastAPI:
    """Create a FastAPI app with test database and versioning service."""
    app = create_app()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.state.sse_bridge = SSEBridge(nc=None)
    app.state.script_versioning = script_versioning

    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:
    """Create an async HTTP client for testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def script_with_file(client: AsyncClient, script_versioning: ScriptVersioningService) -> dict:
    """Create a script in the DB and a corresponding file in the Git repo.

    Returns the script metadata dict from the API response.
    """
    script_path = "test_script.py"
    script_versioning.write_content(script_path, 'print("hello")', commit_message="Initial version")

    create_data = {
        "name": "Test Script With Content",
        "script_path": script_path,
    }
    response = await client.post("/api/v1/scripts", json=create_data)
    assert response.status_code == 201
    return response.json()


class TestReadContent:
    """Tests for GET /api/v1/scripts/{id}/content endpoint."""

    @pytest.mark.asyncio
    async def test_read_content(self, client: AsyncClient, script_with_file: dict) -> None:
        """Test reading script content returns file content and version hash."""
        script_id = script_with_file["id"]
        response = await client.get(f"/api/v1/scripts/{script_id}/content")

        assert response.status_code == 200
        data = response.json()
        assert data["content"] == 'print("hello")'
        assert len(data["version"]) == 40  # Git commit hash is 40 hex chars
        assert data["last_modified"] is not None

    @pytest.mark.asyncio
    async def test_read_content_nonexistent_script(self, client: AsyncClient) -> None:
        """Test reading content for a script that doesn't exist in DB."""
        response = await client.get("/api/v1/scripts/nonexistent-id/content")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_read_content_missing_file(self, client: AsyncClient) -> None:
        """Test reading content when the DB script exists but file is missing."""
        create_data = {
            "name": "Missing File Script",
            "script_path": "nonexistent_file.py",
        }
        create_response = await client.post("/api/v1/scripts", json=create_data)
        script_id = create_response.json()["id"]

        response = await client.get(f"/api/v1/scripts/{script_id}/content")
        assert response.status_code == 404


class TestWriteContent:
    """Tests for PUT /api/v1/scripts/{id}/content endpoint."""

    @pytest.mark.asyncio
    async def test_write_content(self, client: AsyncClient, script_with_file: dict) -> None:
        """Test writing content creates a Git commit and returns new version."""
        script_id = script_with_file["id"]
        update_data = {
            "content": 'print("updated")',
            "commit_message": "Update script",
        }
        response = await client.put(f"/api/v1/scripts/{script_id}/content", json=update_data)

        assert response.status_code == 200
        data = response.json()
        assert data["content"] == 'print("updated")'
        assert len(data["version"]) == 40
        assert data["version"] != script_with_file.get("version", "")

    @pytest.mark.asyncio
    async def test_write_content_auto_commit_message(
        self, client: AsyncClient, script_with_file: dict
    ) -> None:
        """Test writing content without commit_message uses auto-generated message."""
        script_id = script_with_file["id"]
        update_data = {"content": 'print("auto msg")'}
        response = await client.put(f"/api/v1/scripts/{script_id}/content", json=update_data)

        assert response.status_code == 200
        data = response.json()
        assert data["content"] == 'print("auto msg")'

    @pytest.mark.asyncio
    async def test_write_content_nonexistent_script(self, client: AsyncClient) -> None:
        """Test writing content for a script that doesn't exist in DB."""
        update_data = {"content": "some content"}
        response = await client.put("/api/v1/scripts/nonexistent-id/content", json=update_data)
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_write_content_empty_string_rejected(
        self, client: AsyncClient, script_with_file: dict
    ) -> None:
        """Test writing empty content is rejected by Pydantic validation."""
        script_id = script_with_file["id"]
        update_data = {"content": ""}
        response = await client.put(f"/api/v1/scripts/{script_id}/content", json=update_data)
        assert response.status_code == 422


class TestListVersions:
    """Tests for GET /api/v1/scripts/{id}/versions endpoint."""

    @pytest.mark.asyncio
    async def test_list_versions(self, client: AsyncClient, script_with_file: dict) -> None:
        """Test listing versions returns commit history."""
        script_id = script_with_file["id"]

        # Write another version
        update_data = {"content": 'print("v2")', "commit_message": "Second version"}
        await client.put(f"/api/v1/scripts/{script_id}/content", json=update_data)

        response = await client.get(f"/api/v1/scripts/{script_id}/versions")
        assert response.status_code == 200

        data = response.json()
        versions = data["versions"]
        # At least 2 versions: initial + update (may have .gitkeep initial commit too)
        assert len(versions) >= 2
        # Newest first
        assert versions[0]["message"] == "Second version"
        assert len(versions[0]["hash"]) == 40
        assert versions[0]["author"] is not None
        assert versions[0]["timestamp"] is not None

    @pytest.mark.asyncio
    async def test_list_versions_nonexistent_script(self, client: AsyncClient) -> None:
        """Test listing versions for a script that doesn't exist in DB."""
        response = await client.get("/api/v1/scripts/nonexistent-id/versions")
        assert response.status_code == 404


class TestReadVersion:
    """Tests for GET /api/v1/scripts/{id}/versions/{commit_hash} endpoint."""

    @pytest.mark.asyncio
    async def test_read_version(
        self, client: AsyncClient, script_with_file: dict
    ) -> None:
        """Test reading script content at a specific commit hash."""
        script_id = script_with_file["id"]

        # Get the initial version hash
        content_response = await client.get(f"/api/v1/scripts/{script_id}/content")
        initial_hash = content_response.json()["version"]

        # Write a new version
        update_data = {"content": 'print("new version")', "commit_message": "New version"}
        await client.put(f"/api/v1/scripts/{script_id}/content", json=update_data)

        # Read the old version
        response = await client.get(f"/api/v1/scripts/{script_id}/versions/{initial_hash}")
        assert response.status_code == 200
        data = response.json()
        assert data["content"] == 'print("hello")'
        assert data["version"] == initial_hash

    @pytest.mark.asyncio
    async def test_read_version_invalid_hash(
        self, client: AsyncClient, script_with_file: dict
    ) -> None:
        """Test reading at an invalid commit hash returns 404."""
        script_id = script_with_file["id"]
        response = await client.get(f"/api/v1/scripts/{script_id}/versions/deadbeef")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_read_version_nonexistent_script(self, client: AsyncClient) -> None:
        """Test reading version for a script that doesn't exist in DB."""
        response = await client.get("/api/v1/scripts/nonexistent-id/versions/abc123")
        assert response.status_code == 404


class TestScriptVersioningService:
    """Unit tests for ScriptVersioningService (no HTTP layer)."""

    def test_init_creates_git_repo(self, tmp_path: Path) -> None:
        """Test that initialization creates a Git repo if none exists."""
        ScriptVersioningService(scripts_root=tmp_path)
        assert (tmp_path / ".git").is_dir()

    def test_init_existing_repo(self, tmp_path: Path) -> None:
        """Test that initialization works with an existing Git repo."""
        ScriptVersioningService(scripts_root=tmp_path)
        # Second init should not fail
        ScriptVersioningService(scripts_root=tmp_path)
        assert (tmp_path / ".git").is_dir()

    def test_read_content(self, tmp_path: Path) -> None:
        """Test reading content from a tracked file."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        svc.write_content("test.py", "hello world")
        content = svc.read_content("test.py")
        assert content == "hello world"

    def test_read_content_missing_file(self, tmp_path: Path) -> None:
        """Test reading a missing file raises HTTPException 404."""
        from fastapi import HTTPException

        svc = ScriptVersioningService(scripts_root=tmp_path)
        with pytest.raises(HTTPException) as exc_info:
            svc.read_content("missing.py")
        assert exc_info.value.status_code == 404

    def test_write_content_returns_hash(self, tmp_path: Path) -> None:
        """Test that write_content returns a valid commit hash."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        commit_hash = svc.write_content("test.py", "content", commit_message="test commit")
        assert len(commit_hash) == 40
        assert all(c in "0123456789abcdef" for c in commit_hash)

    def test_list_versions(self, tmp_path: Path) -> None:
        """Test listing versions returns commit history for a file."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        svc.write_content("test.py", "v1", commit_message="First")
        svc.write_content("test.py", "v2", commit_message="Second")

        versions = svc.list_versions("test.py")
        assert len(versions) == 2
        assert versions[0]["message"] == "Second"  # newest first
        assert versions[1]["message"] == "First"

    def test_read_version(self, tmp_path: Path) -> None:
        """Test reading file content at a specific commit."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        hash1 = svc.write_content("test.py", "version1", commit_message="V1")
        svc.write_content("test.py", "version2", commit_message="V2")

        content = svc.read_version("test.py", hash1)
        assert content == "version1"

    def test_read_version_invalid_hash(self, tmp_path: Path) -> None:
        """Test reading at an invalid hash raises HTTPException 404."""
        from fastapi import HTTPException

        svc = ScriptVersioningService(scripts_root=tmp_path)
        svc.write_content("test.py", "content")

        with pytest.raises(HTTPException) as exc_info:
            svc.read_version("test.py", "invalid_hash")
        assert exc_info.value.status_code == 404

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        """Test that path traversal attempts are blocked."""
        from fastapi import HTTPException

        svc = ScriptVersioningService(scripts_root=tmp_path)
        with pytest.raises(HTTPException) as exc_info:
            svc.read_content("../../../etc/passwd")
        assert exc_info.value.status_code == 404

    def test_get_head_commit_hash(self, tmp_path: Path) -> None:
        """Test getting the head commit hash for a file."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        hash1 = svc.write_content("test.py", "v1")
        result = svc.get_head_commit_hash("test.py")
        assert result == hash1

    def test_get_head_commit_hash_no_commits(self, tmp_path: Path) -> None:
        """Test getting head commit hash for a file with no commits returns None."""
        svc = ScriptVersioningService(scripts_root=tmp_path)
        # Create a file outside of git tracking
        (tmp_path / "untracked.py").write_text("untracked", encoding="utf-8")
        result = svc.get_head_commit_hash("untracked.py")
        assert result is None
