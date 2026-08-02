"""Station orchestrator - multi-station test workflow coordination via NATS KV.

Coordinates test execution across multiple physical test stations (edge
workers) using a NATS JetStream KV handshake. Each station runs a portion
of a test sequence; when a station finishes, it writes a handoff record to
KV. Downstream stations watch for their upstream's handoff before starting.

KV layout (per AGENTS.md NATS naming conventions):
    - Bucket: ``ate-handoffs`` (lower-kebab)
    - Workflow definition key: ``workflow.{workflow_id}``
    - Handoff key: ``session.{session_id}.station.{station_id}.done``

Per AGENTS.md section 7: if NATS or the KV bucket is unavailable, operations
raise ``RuntimeError`` - no silent degradation, no local fallback.

This module lives in ``ate_platform`` (edge side). The cloud side interacts
via the API endpoints in ``ate_cloud.api.v1.workflows`` which call through
to the same KV bucket.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import nats
from nats.aio.client import Client as NatsClient
from nats.js.errors import KeyNotFoundError, NotFoundError

from shared.multi_station import (
    HandoffStatus,
    StationHandoff,
    StationWorkflow,
    handoff_from_dict,
    handoff_to_dict,
    workflow_from_dict,
    workflow_to_dict,
)

logger = logging.getLogger(__name__)

# KV bucket for multi-station handoffs (persistent, no TTL - handoffs must
# survive until explicitly read by the downstream station or aged out by
# NATS bucket max_age if configured at creation).
HANDOFF_KV_BUCKET: str = "ate-handoffs"

# Key prefixes (lower.dot convention).
_WORKFLOW_KEY_PREFIX: str = "workflow."
_HANDOFF_KEY_PREFIX: str = "session."


def _workflow_key(workflow_id: str) -> str:
    """Build the KV key for a workflow definition."""
    return f"{_WORKFLOW_KEY_PREFIX}{workflow_id}"


def _handoff_key(session_id: str, station_id: str) -> str:
    """Build the KV key for a station completion handoff.

    Key pattern: ``session.{session_id}.station.{station_id}.done``
    """
    return f"{_HANDOFF_KEY_PREFIX}{session_id}.station.{station_id}.done"


class StationOrchestrator:
    """Coordinates multi-station test workflows via NATS KV handoffs.

    Each edge worker instantiates a StationOrchestrator bound to its
    ``station_id``. The orchestrator connects to NATS, accesses the
    ``ate-handoffs`` KV bucket, and provides methods to:

    - Register a workflow definition (stored in KV for all stations to read).
    - Signal completion (``notify_done``) by writing a handoff record.
    - Wait for an upstream station's handoff (``wait_for_upstream``) by
      watching the KV key, with a timeout.
    - Read a handoff record (``get_handoff``) for inspection.

    The orchestrator does NOT execute test sequences itself - that is the
    ScannerScheduler's job. It only handles the inter-station coordination.

    Per AGENTS.md section 7, if NATS is unreachable or the KV bucket cannot
    be accessed/created, methods raise ``RuntimeError``. There is no
    fallback to local state.
    """

    def __init__(
        self,
        nats_url: str = "nats://localhost:4222",
        station_id: str = "",
    ) -> None:
        """Initialize the station orchestrator.

        Args:
            nats_url: NATS server URL (default ``nats://localhost:4222``).
            station_id: Identifier of the local station. May be empty if the
                orchestrator is used purely for workflow registration/lookup
                (e.g., from the cloud API), but must be set before calling
                ``wait_for_upstream`` or ``notify_done`` for the local station.
        """
        self._nats_url = nats_url
        self._station_id = station_id
        self._nc: NatsClient | None = None
        self._js: Any = None
        self._kv: Any = None
        self._owns_connection: bool = False

    @property
    def station_id(self) -> str:
        """The station identifier this orchestrator is bound to."""
        return self._station_id

    async def connect(self, nc: NatsClient | None = None) -> None:
        """Connect to NATS and acquire the ``ate-handoffs`` KV bucket handle.

        If the bucket does not exist, it is created (persistent, no TTL).
        This follows the same pattern as ConfigDistributionService.

        Args:
            nc: Optional pre-connected NATS client. If ``None``, a new
                connection is made to ``nats_url`` and closed on
                :meth:`disconnect`.

        Raises:
            RuntimeError: If connection fails or the KV bucket cannot be
                accessed or created.
        """
        if self._nc is not None:
            return  # Already connected.
        if nc is not None:
            self._nc = nc
            self._owns_connection = False
        else:
            try:
                self._nc = await nats.connect(self._nats_url)
            except Exception as e:
                raise RuntimeError(
                    f"Failed to connect to NATS at {self._nats_url}: {e}"
                ) from e
            self._owns_connection = True

        self._js = self._nc.jetstream()
        try:
            self._kv = await self._js.key_value(HANDOFF_KV_BUCKET)
        except NotFoundError:
            try:
                self._kv = await self._js.create_key_value(bucket=HANDOFF_KV_BUCKET)
                logger.info(
                    "Created KV bucket '%s' (persistent, no TTL)", HANDOFF_KV_BUCKET
                )
            except Exception as e:
                raise RuntimeError(
                    f"Failed to create KV bucket '{HANDOFF_KV_BUCKET}': {e}"
                ) from e
        except Exception as e:
            raise RuntimeError(
                f"Failed to access KV bucket '{HANDOFF_KV_BUCKET}': {e}"
            ) from e
        logger.info(
            "StationOrchestrator connected (station_id='%s')", self._station_id
        )

    async def disconnect(self) -> None:
        """Close the NATS connection if this orchestrator owns it.

        Safe to call multiple times. If a pre-connected client was passed
        to :meth:`connect`, it is NOT closed (caller owns its lifecycle).
        """
        if self._owns_connection and self._nc is not None:
            try:
                await self._nc.close()
            except Exception:
                logger.debug("Error closing NATS connection (ignored)", exc_info=True)
        self._nc = None
        self._js = None
        self._kv = None
        self._owns_connection = False

    async def _ensure_connected(self) -> None:
        """Ensure the orchestrator is connected; raise if not."""
        if self._kv is None:
            raise RuntimeError(
                "StationOrchestrator not connected - call connect() first"
            )

    async def register_workflow(self, workflow: StationWorkflow) -> int:
        """Register a multi-station workflow definition in KV.

        Stores the serialized workflow at key ``workflow.{workflow_id}``
        so all stations can discover the station ordering and dependencies.

        Args:
            workflow: The workflow definition to register.

        Returns:
            The KV revision number of the put operation.

        Raises:
            RuntimeError: If not connected or the KV put fails.
        """
        await self._ensure_connected()
        key = _workflow_key(workflow.workflow_id)
        payload = json.dumps(workflow_to_dict(workflow)).encode("utf-8")
        try:
            revision = int(await self._kv.put(key, payload))
        except Exception as e:
            raise RuntimeError(
                f"Failed to register workflow '{workflow.workflow_id}': {e}"
            ) from e
        logger.info(
            "Registered workflow '%s' (%d stations, key=%s, rev=%s)",
            workflow.workflow_id,
            len(workflow.stations),
            key,
            revision,
        )
        return revision

    async def get_workflow(self, workflow_id: str) -> StationWorkflow | None:
        """Retrieve a registered workflow definition from KV.

        Args:
            workflow_id: The workflow identifier.

        Returns:
            The StationWorkflow if found, ``None`` if the key doesn't exist.

        Raises:
            RuntimeError: If not connected or the KV read fails for a reason
                other than key-not-found.
        """
        await self._ensure_connected()
        key = _workflow_key(workflow_id)
        try:
            entry = await self._kv.get(key)
        except KeyNotFoundError:
            return None
        except Exception as e:
            raise RuntimeError(
                f"Failed to get workflow '{workflow_id}': {e}"
            ) from e
        if entry.value is None:
            return None
        data = json.loads(entry.value.decode("utf-8"))
        return workflow_from_dict(data)

    async def notify_done(
        self,
        session_id: str,
        station_id: str,
        handoff: StationHandoff,
    ) -> int:
        """Signal that a station has completed by writing a handoff record.

        Writes the serialized handoff to KV key
        ``session.{session_id}.station.{station_id}.done``. Downstream
        stations watching this key will be notified.

        Args:
            session_id: The test session (DUT flow) identifier.
            station_id: The station that completed. Should match
                ``handoff.station_id``.
            handoff: The handoff record to write.

        Returns:
            The KV revision number of the put operation.

        Raises:
            RuntimeError: If not connected or the KV put fails.
            ValueError: If ``station_id`` does not match ``handoff.station_id``
                or ``session_id`` does not match ``handoff.session_id``.
        """
        if station_id != handoff.station_id:
            raise ValueError(
                f"station_id '{station_id}' does not match "
                f"handoff.station_id '{handoff.station_id}'"
            )
        if session_id != handoff.session_id:
            raise ValueError(
                f"session_id '{session_id}' does not match "
                f"handoff.session_id '{handoff.session_id}'"
            )
        await self._ensure_connected()
        key = _handoff_key(session_id, station_id)
        payload = json.dumps(handoff_to_dict(handoff)).encode("utf-8")
        try:
            revision = int(await self._kv.put(key, payload))
        except Exception as e:
            raise RuntimeError(
                f"Failed to write handoff for session '{session_id}' "
                f"station '{station_id}': {e}"
            ) from e
        logger.info(
            "Wrote handoff for session '%s' station '%s' (pass_fail=%s, key=%s, rev=%s)",
            session_id,
            station_id,
            handoff.pass_fail,
            key,
            revision,
        )
        return revision

    async def wait_for_upstream(
        self,
        session_id: str,
        upstream_station_id: str,
        timeout: float = 300.0,
    ) -> HandoffStatus:
        """Wait for an upstream station to complete by watching its KV key.

        Watches the key ``session.{session_id}.station.{upstream_station_id}.done``
        for a PUT operation. Returns as soon as the key is written.

        If the key already exists when this method is called (the upstream
        finished before the watch started), it returns ``DONE`` or ``FAILED``
        immediately based on the handoff's ``pass_fail`` field.

        Args:
            session_id: The test session identifier.
            upstream_station_id: The station to wait for.
            timeout: Maximum seconds to wait. Defaults to 300 (5 minutes).

        Returns:
            - ``HandoffStatus.DONE`` if the upstream completed successfully
              (``pass_fail=True``).
            - ``HandoffStatus.FAILED`` if the upstream completed but reported
              failure (``pass_fail=False``).
            - ``HandoffStatus.TIMEOUT`` if the upstream did not complete
              within ``timeout`` seconds.

        Raises:
            RuntimeError: If not connected or the KV watch cannot be
                established.
        """
        await self._ensure_connected()
        key = _handoff_key(session_id, upstream_station_id)

        # Fast path: check if the key already exists.
        existing = await self._get_handoff_raw(session_id, upstream_station_id)
        if existing is not None:
            status = HandoffStatus.DONE if existing.pass_fail else HandoffStatus.FAILED
            logger.info(
                "Upstream '%s' already done for session '%s' (status=%s)",
                upstream_station_id,
                session_id,
                status.value,
            )
            return status

        # Slow path: watch for the key to appear.
        try:
            watcher = await self._kv.watch(keys=key)
        except Exception as e:
            raise RuntimeError(
                f"Failed to watch handoff key '{key}': {e}"
            ) from e

        try:
            status = await self._drain_watch_until_done(
                watcher, session_id, upstream_station_id, timeout
            )
            return status
        finally:
            try:
                await watcher.stop()
            except Exception:
                logger.debug("Error stopping KV watcher (ignored)", exc_info=True)

    async def _drain_watch_until_done(
        self,
        watcher: Any,
        session_id: str,
        upstream_station_id: str,
        timeout: float,
    ) -> HandoffStatus:
        """Iterate the watcher until a PUT on the target key or timeout.

        Uses ``asyncio.wait_for`` on each ``__anext__`` call with the
        remaining time budget so the deadline is enforced even when the
        watcher blocks indefinitely waiting for the next entry.
        """
        key = _handoff_key(session_id, upstream_station_id)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return HandoffStatus.TIMEOUT
                try:
                    entry = await asyncio.wait_for(
                        watcher.__anext__(), timeout=remaining,
                    )
                except TimeoutError:
                    return HandoffStatus.TIMEOUT
                if entry is None:
                    # End-of-initial-values marker; continue waiting.
                    continue
                if entry.operation is not None:
                    # DEL/PURGE - not a completion signal; keep waiting.
                    continue
                if entry.key != key:
                    continue
                # Match - decode the handoff to check pass_fail.
                handoff = _decode_handoff_entry(entry)
                status = HandoffStatus.DONE if handoff.pass_fail else HandoffStatus.FAILED
                logger.info(
                    "Observed upstream '%s' done for session '%s' (status=%s, rev=%s)",
                    upstream_station_id,
                    session_id,
                    status.value,
                    entry.revision,
                )
                return status
        except StopAsyncIteration:
            # Watcher exhausted without seeing the key - treat as timeout.
            return HandoffStatus.TIMEOUT

    async def get_handoff(
        self,
        session_id: str,
        station_id: str,
    ) -> StationHandoff | None:
        """Read a station's handoff record from KV, if present.

        Args:
            session_id: The test session identifier.
            station_id: The station whose handoff to read.

        Returns:
            The StationHandoff if the key exists, ``None`` otherwise.

        Raises:
            RuntimeError: If not connected or the KV read fails for a reason
                other than key-not-found.
        """
        return await self._get_handoff_raw(session_id, station_id)

    async def _get_handoff_raw(
        self,
        session_id: str,
        station_id: str,
    ) -> StationHandoff | None:
        """Internal: read and decode a handoff entry, returning None if missing."""
        await self._ensure_connected()
        key = _handoff_key(session_id, station_id)
        try:
            entry = await self._kv.get(key)
        except KeyNotFoundError:
            return None
        except Exception as e:
            raise RuntimeError(
                f"Failed to get handoff for session '{session_id}' "
                f"station '{station_id}': {e}"
            ) from e
        if entry.value is None:
            return None
        return _decode_handoff_entry(entry)


def _decode_handoff_entry(entry: Any) -> StationHandoff:
    """Decode a KV entry's value bytes into a StationHandoff.

    Falls back to sensible defaults if the payload is malformed, but always
    returns a StationHandoff (the KV entry's existence is the signal).
    """
    try:
        data = json.loads(entry.value.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        # KV entry exists but payload is corrupt - treat as a bare signal.
        return StationHandoff(
            session_id="",
            station_id="",
            pass_fail=True,
        )
    return handoff_from_dict(data)
