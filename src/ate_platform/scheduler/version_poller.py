"""Version poller — detects script version changes for hot-reload.

Runs on the worker side. Every ``poll_interval`` seconds (default 60s),
reads the worker's script version tags from the ``ate-scripts`` JetStream
KV bucket and compares them with the locally known versions. On mismatch,
invokes the ``on_version_update`` callback so the caller can pull the new
content and hot-reload.

Lifecycle::

    poller = VersionPoller(worker_id="abc-123", nats_url="nats://localhost:4222")
    poller.on_version_update = my_reload_handler
    await poller.start()
    ...
    await poller.stop()

The first ``check_once()`` call establishes the baseline (records all
tagged versions without signalling). Subsequent calls detect changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import nats
from nats.aio.client import Client as NatsClient
from nats.js.errors import NoKeysError

logger = logging.getLogger(__name__)

_SCRIPTS_KV_BUCKET = "ate-scripts"
_DEFAULT_POLL_INTERVAL: float = 60.0

# Callback signature: (script_path, new_hash) -> coroutine
VersionUpdateCallback = Callable[[str, str], Awaitable[None]]


@dataclass
class VersionDiff:
    """A detected version change for a tagged script.

    Attributes:
        script_path: Relative path to the script file.
        new_hash: The newly tagged commit hash from KV.
        old_hash: The previously known commit hash (None if first sighting
            after baseline).
    """

    script_path: str
    new_hash: str
    old_hash: str | None


class VersionPoller:
    """Polls script version tags from JetStream KV for hot-reload signalling.

    Every ``poll_interval`` seconds, reads all ``workers.{worker_id}.*``
    keys from the ``ate-scripts`` KV bucket. On the first poll, records
    the baseline. On subsequent polls, detects version changes and invokes
    the ``on_version_update`` callback for each changed script.

    Attributes:
        worker_id: Unique worker identifier.
        nats_url: NATS server URL.
        poll_interval: Seconds between polls (default 60).
        on_version_update: Async callback invoked on version mismatch.
    """

    def __init__(
        self,
        worker_id: str,
        nats_url: str = "nats://localhost:4222",
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        on_version_update: VersionUpdateCallback | None = None,
    ) -> None:
        self._worker_id = worker_id
        self._nats_url = nats_url
        self._poll_interval = poll_interval
        self._on_version_update = on_version_update

        self._nc: NatsClient | None = None
        self._js: Any = None
        self._poll_task: asyncio.Task[None] | None = None
        self._running = False

        # Locally known versions: script_path -> commit_hash.
        # Populated by the first poll (baseline); updated on each change.
        self._known_versions: dict[str, str] = {}
        self._initialized = False

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def start(self, nc: NatsClient | None = None) -> None:
        """Connect to NATS and start the polling loop.

        Args:
            nc: Optional pre-connected NATS client. If None, connects to
                ``nats_url``.
        """
        if nc is not None:
            self._nc = nc
        else:
            self._nc = await nats.connect(self._nats_url)
        self._js = self._nc.jetstream()
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("VersionPoller started for worker '%s'", self._worker_id)

    async def stop(self) -> None:
        """Stop polling and close the NATS connection."""
        self._running = False

        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        if self._nc is not None:
            await self._nc.close()
            self._nc = None

        logger.info("VersionPoller stopped for worker '%s'", self._worker_id)

    async def check_once(self) -> list[VersionDiff]:
        """Perform a single poll and return version diffs.

        On the first call, establishes the baseline (records all tagged
        versions) and returns an empty list. On subsequent calls, compares
        KV tags with the baseline and returns diffs for changed scripts.

        Returns:
            List of VersionDiff for scripts whose tagged hash changed.

        Raises:
            RuntimeError: If not started (no JetStream context).
        """
        if self._js is None:
            raise RuntimeError("VersionPoller not started — call start() first")

        kv = await self._js.key_value(_SCRIPTS_KV_BUCKET)
        prefix = f"workers.{self._worker_id}."

        try:
            keys = await kv.keys()
        except NoKeysError:
            keys = []

        current_tags: dict[str, str] = {}
        for key in keys:
            if not key.startswith(prefix):
                continue
            entry = await kv.get(key)
            tag = json.loads(entry.value.decode("utf-8"))
            script_path = tag["script_path"]
            tagged_hash = tag["commit_hash"]
            current_tags[script_path] = tagged_hash

        diffs: list[VersionDiff] = []
        if not self._initialized:
            # First poll: establish baseline without signalling.
            self._known_versions = current_tags
            self._initialized = True
            logger.info(
                "VersionPoller baseline established: %d scripts tracked",
                len(current_tags),
            )
            return diffs

        for script_path, new_hash in current_tags.items():
            old_hash = self._known_versions.get(script_path)
            if old_hash != new_hash:
                diffs.append(VersionDiff(
                    script_path=script_path,
                    new_hash=new_hash,
                    old_hash=old_hash,
                ))

        self._known_versions = current_tags
        return diffs

    async def _poll_loop(self) -> None:
        """Main polling loop — runs until ``stop()`` is called."""
        while self._running:
            try:
                diffs = await self.check_once()
                for diff in diffs:
                    logger.info(
                        "Version change detected: '%s' %s → %s",
                        diff.script_path,
                        (diff.old_hash or "none")[:8],
                        diff.new_hash[:8],
                    )
                    if self._on_version_update is not None:
                        await self._on_version_update(
                            diff.script_path, diff.new_hash,
                        )
            except Exception as e:
                logger.warning("Version poll failed: %s", e)
            await asyncio.sleep(self._poll_interval)
