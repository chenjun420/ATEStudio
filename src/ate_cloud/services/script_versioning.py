"""Git-based script versioning service.

Provides file content read/write with automatic Git commit tracking
for script version history. All Git operations use GitPython with
pathlib.Path for cross-platform compatibility (ARM64/x86_64, Linux/Windows).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import git
from fastapi import HTTPException, status

from ate_cloud.schemas.script import WorkerVersionTag


class ScriptVersioningService:
    """Manages script file content with Git-based version tracking.

    Attributes:
        scripts_root: Root directory containing script files.
        repo: GitPython Repo instance for the scripts_root directory.
    """

    def __init__(self, scripts_root: Path) -> None:
        """Initialize the versioning service.

        Args:
            scripts_root: Path to the root directory for script files.
                A Git repository will be initialized here if one does not exist.
        """
        self.scripts_root = scripts_root
        self.scripts_root.mkdir(parents=True, exist_ok=True)

        if self._is_git_repo():
            self.repo = git.Repo(self.scripts_root)
        else:
            self.repo = git.Repo.init(self.scripts_root)
            # Create initial commit so git log works on empty repos
            self._ensure_initial_commit()

    def _is_git_repo(self) -> bool:
        """Check if scripts_root contains a valid Git repository."""
        git_dir = self.scripts_root / ".git"
        return git_dir.is_dir()

    def _ensure_initial_commit(self) -> None:
        """Create an empty initial commit if the repo has no commits."""
        try:
            _ = self.repo.head.commit
        except ValueError:
            # No commits yet — create a placeholder
            readme_path = self.scripts_root / ".gitkeep"
            readme_path.write_text("", encoding="utf-8")
            self.repo.index.add([".gitkeep"])
            self.repo.index.commit(
                message="Initial commit",
                author=git.Actor("ATE Studio", "ate-studio@local"),
                committer=git.Actor("ATE Studio", "ate-studio@local"),
            )

    def _resolve_path(self, script_path: str) -> Path:
        """Resolve a relative script path to an absolute path within scripts_root.

        Args:
            script_path: Relative path to the script file.

        Returns:
            Resolved absolute Path.

        Raises:
            HTTPException: 404 if the path escapes scripts_root (traversal attack).
        """
        # Normalize to forward slashes for cross-platform consistency
        normalized = script_path.replace("\\", "/")
        resolved = (self.scripts_root / normalized).resolve()

        # Security: prevent path traversal outside scripts_root
        if not str(resolved).startswith(str(self.scripts_root.resolve())):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Script path not found",
            )

        return resolved

    def read_content(self, script_path: str) -> str:
        """Read the current content of a script file.

        Args:
            script_path: Relative path to the script file within scripts_root.

        Returns:
            The file content as a string.

        Raises:
            HTTPException: 404 if the file does not exist.
        """
        resolved = self._resolve_path(script_path)

        if not resolved.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Script file not found: {script_path}",
            )

        return resolved.read_text(encoding="utf-8")

    def write_content(
        self,
        script_path: str,
        content: str,
        commit_message: str | None = None,
    ) -> str:
        """Write content to a script file and create a Git commit.

        Args:
            script_path: Relative path to the script file within scripts_root.
            content: The content to write.
            commit_message: Optional commit message. Defaults to auto-generated.

        Returns:
            The commit hash of the new commit.

        Raises:
            HTTPException: 404 if the parent directory does not exist.
        """
        resolved = self._resolve_path(script_path)

        # Ensure parent directory exists
        resolved.parent.mkdir(parents=True, exist_ok=True)

        # Write content
        resolved.write_text(content, encoding="utf-8")

        # Stage and commit
        # Use POSIX path for git index (cross-platform)
        posix_path = script_path.replace("\\", "/")
        self.repo.index.add([posix_path])

        if commit_message is None:
            commit_message = f"Update {posix_path}"

        commit = self.repo.index.commit(
            message=commit_message,
            author=git.Actor("ATE Studio", "ate-studio@local"),
            committer=git.Actor("ATE Studio", "ate-studio@local"),
        )

        return str(commit.hexsha)

    def list_versions(self, script_path: str) -> list[dict[str, object]]:
        """List version history for a script file.

        Args:
            script_path: Relative path to the script file within scripts_root.

        Returns:
            List of version info dicts with keys: hash, message, author, timestamp.

        Raises:
            HTTPException: 404 if the file does not exist.
        """
        resolved = self._resolve_path(script_path)

        if not resolved.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Script file not found: {script_path}",
            )

        posix_path = script_path.replace("\\", "/")
        versions: list[dict[str, object]] = []

        try:
            commits = list(self.repo.iter_commits(paths=posix_path))
        except Exception:
            # No commits for this path
            return versions

        for commit in commits:
            # GitPython committed_date is a Unix timestamp (int)
            ts = datetime.fromtimestamp(commit.committed_date, tz=timezone.utc)
            versions.append({
                "hash": str(commit.hexsha),
                "message": str(commit.message).strip(),
                "author": str(commit.author),
                "timestamp": ts,
            })

        return versions

    def read_version(self, script_path: str, commit_hash: str) -> str:
        """Read script content at a specific Git commit.

        Args:
            script_path: Relative path to the script file within scripts_root.
            commit_hash: The commit hash to read the file at.

        Returns:
            The file content at the given commit.

        Raises:
            HTTPException: 404 if the commit or file at that commit does not exist.
        """
        posix_path = script_path.replace("\\", "/")

        try:
            commit = self.repo.commit(commit_hash)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Commit not found: {commit_hash}",
            ) from None

        try:
            blob = commit.tree / posix_path
            content = blob.data_stream.read().decode("utf-8")
        except (KeyError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Script file not found at commit {commit_hash}: {script_path}",
            ) from None

        return cast(str, content)

    def get_head_commit_hash(self, script_path: str) -> str | None:
        """Get the latest commit hash for a script file.

        Args:
            script_path: Relative path to the script file.

        Returns:
            The hexsha of the latest commit, or None if no commits exist.
        """
        posix_path = script_path.replace("\\", "/")
        try:
            commit = next(self.repo.iter_commits(paths=posix_path))
            return str(commit.hexsha)
        except StopIteration:
            return None

    def get_last_modified(self, script_path: str) -> datetime | None:
        """Get the last modified timestamp for a script file from Git history.

        Args:
            script_path: Relative path to the script file.

        Returns:
            The datetime of the latest commit, or None if no commits exist.
        """
        posix_path = script_path.replace("\\", "/")
        try:
            commit = next(self.repo.iter_commits(paths=posix_path))
            return datetime.fromtimestamp(commit.committed_date, tz=timezone.utc)
        except StopIteration:
            return None

    # ------------------------------------------------------------------
    # Worker script version tags (JetStream KV bucket "ate-scripts")
    # ------------------------------------------------------------------

    async def tag_worker(
        self,
        script_path: str,
        worker_id: str,
        commit_hash: str,
        js: Any,
    ) -> int:
        """Record a worker→script version tag in the ``ate-scripts`` KV bucket.

        Writes a ``WorkerVersionTag`` JSON payload under the key
        ``workers.{worker_id}.{script_path}`` (backslashes normalized to
        forward slashes for cross-platform consistency).

        Args:
            script_path: Relative script path.
            worker_id: Unique worker identifier.
            commit_hash: Git commit hash the worker should run.
            js: JetStream context exposing async ``key_value``/``put``.

        Returns:
            The KV revision returned by ``put()``.
        """
        normalized = script_path.replace("\\", "/")
        key = f"workers.{worker_id}.{normalized}"
        payload = WorkerVersionTag(
            worker_id=worker_id,
            script_path=normalized,
            commit_hash=commit_hash,
        )
        kv = await js.key_value("ate-scripts")
        return cast(int, await kv.put(key, payload.model_dump_json().encode("utf-8")))

    async def check_worker_version(
        self,
        worker_id: str,
        js: Any,
    ) -> list[dict[str, str | bool]]:
        """Compare a worker's tagged script hashes against current Git HEAD.

        Reads all ``workers.{worker_id}.*`` tags from the ``ate-scripts``
        KV bucket and reports which scripts need an update.

        Args:
            worker_id: Unique worker identifier.
            js: JetStream context exposing async ``key_value``/``keys``/``get``.

        Returns:
            List of diff dicts: ``{script_path, tagged_hash, current_hash,
            needs_update}``. Empty when no tags exist.
        """
        kv = await js.key_value("ate-scripts")
        prefix = f"workers.{worker_id}."
        try:
            keys = await kv.keys()
        except Exception:
            # No keys (NoKeysError) or KV unavailable — nothing to compare
            return []

        diffs: list[dict[str, str | bool]] = []
        for key in keys:
            if not key.startswith(prefix):
                continue
            entry = await kv.get(key)
            tag = json.loads(entry.value.decode("utf-8"))
            script_path = tag["script_path"]
            tagged_hash = tag["commit_hash"]
            head_hash = self.get_head_commit_hash(script_path)
            needs_update = head_hash is not None and head_hash != tagged_hash
            diffs.append({
                "script_path": script_path,
                "tagged_hash": tagged_hash,
                "current_hash": head_hash or tagged_hash,
                "needs_update": needs_update,
            })
        return diffs

    async def sync_worker(
        self,
        worker_id: str,
        js: Any,
    ) -> list[dict[str, str]]:
        """Re-tag every script whose Git HEAD has advanced past its tag.

        Args:
            worker_id: Unique worker identifier.
            js: JetStream context exposing async ``key_value``/``keys``/``get``.

        Returns:
            List of ``{script_path, commit_hash}`` for scripts that were
            re-tagged to the current HEAD. Scripts with no Git commits are
            skipped.
        """
        kv = await js.key_value("ate-scripts")
        prefix = f"workers.{worker_id}."
        try:
            keys = await kv.keys()
        except Exception:
            return []

        results: list[dict[str, str]] = []
        for key in keys:
            if not key.startswith(prefix):
                continue
            entry = await kv.get(key)
            tag = json.loads(entry.value.decode("utf-8"))
            script_path = tag["script_path"]
            head_hash = self.get_head_commit_hash(script_path)
            if head_hash is None:
                # No commits for this path — nothing to sync
                continue
            if head_hash != tag["commit_hash"]:
                await self.tag_worker(script_path, worker_id, head_hash, js)
                results.append({"script_path": script_path, "commit_hash": head_hash})
        return results
