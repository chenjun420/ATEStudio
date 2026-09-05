"""Failure Indexer — indexes failed test execution events in Qdrant for RAG diagnosis.

Subscribes to STEP_FAILED, EXECUTION_COMPLETED (result=FAILED), and SPC
ALARM_RAISED events, extracts metadata, embeds text via the injected
EmbeddingService, and stores in Qdrant for similarity search of past failures.

Two entry points feed the indexer:

1. ``handle_status_event(raw_event)`` — the relay/queue path. The edge
   publishes ``{"type": "<EVENT_TYPE>", "data": {...}}`` envelopes on
   ate.status.*; ExecutionStatusRelay forwards every consumed status event
   here after its durable DB update + SSE push. This is the path real edge
   failures and SPC alarms travel.
2. ``subscribe_to_events(bridge)`` — wraps ``SSEBridge.publish_event`` for
   events originated inside the cloud (local mode / API-published events).

All indexing is non-blocking — failures are logged but never interrupt the
event path.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

from ate_cloud.config import settings
from ate_cloud.nats.sse_bridge import SSEBridge
from shared.events import Event, EventType

logger = logging.getLogger(__name__)

# Fields to extract from STEP_FAILED / EXECUTION_COMPLETED event data,
# plus SPC ALARM_RAISED payload fields.
_METADATA_FIELDS: list[str] = [
    "sequence_yaml",
    "failed_step_id",
    "failed_step_name",
    "error_message",
    "variable_snapshot",
    "step_history",
    "run_id",
    "plan_name",
    "status",
    # SPC ALARM_RAISED payload (see services/spc.py _index_alert_async)
    "alarm_id",
    "product_type",
    "measurement_name",
    "rule",
    "severity",
    "message",
    "value",
    "sample_count",
]


class FailureIndexer:
    """Indexes failed test execution events in Qdrant for similarity search.

    Attributes:
        _qdrant_client: Qdrant client instance (or mock).
        _embedding_model: Callable that takes text and returns embedding vector.
        _collection_name: Qdrant collection name for failure vectors.
        _embedding_dim: Dimension of embedding vectors.
    """

    def __init__(
        self,
        qdrant_client: Any,
        embedding_model: Callable[[str], list[float]] | None = None,
        collection_name: str | None = None,
        embedding_dim: int | None = None,
        embedding_service: Any | None = None,
    ) -> None:
        """Initialize the failure indexer.

        Accepts either a sync ``embedding_model`` callable (legacy) or an
        ``embedding_service`` exposing an async ``embed(text)`` (preferred).
        When both are provided, ``embedding_service`` takes precedence.

        Args:
            qdrant_client: Qdrant client instance.
            embedding_model: Callable text → embedding vector (e.g., DeepAgents embed).
            collection_name: Qdrant collection name (defaults to settings).
            embedding_dim: Vector dimensions (defaults to settings).
            embedding_service: Async embedding service (EmbeddingService) with
                an ``embed`` coroutine — used over ``embedding_model`` when set.
        """
        self._qdrant_client = qdrant_client
        self._embedding_model = embedding_model
        self._embedding_service = embedding_service
        self._collection_name = collection_name or settings.qdrant_collection_failures
        self._embedding_dim = embedding_dim or settings.embedding_dimensions
        # Best-effort automatic KG-evolution hook (task 16). Set via
        # set_evolution_trigger; when unset, indexing behaves exactly as before.
        self._evolution_hook: Callable[[dict[str, Any]], Awaitable[bool]] | None = None

    def set_evolution_trigger(
        self,
        hook: Callable[[dict[str, Any]], Awaitable[bool]] | None,
    ) -> None:
        """Register the best-effort auto KG-evolution hook (or None to disable).

        The hook is awaited AFTER a failure is persisted to the failure index
        and must return ``True`` on success / ``False`` on a degraded skip.
        It MUST NOT raise — failures are swallowed here as a backstop so
        evolution can never break indexing.
        """
        self._evolution_hook = hook

    async def ensure_collection(self) -> None:
        """Create Qdrant collection if it does not exist."""
        try:
            from qdrant_client.http import models as qmodels

            collections = self._qdrant_client.get_collections()
            collection_names = {c.name for c in collections.collections}

            if self._collection_name not in collection_names:
                self._qdrant_client.create_collection(
                    collection_name=self._collection_name,
                    vectors_config=qmodels.VectorParams(
                        size=self._embedding_dim,
                        distance=qmodels.Distance.COSINE,
                    ),
                )
                logger.info(
                    "Created Qdrant collection '%s' (dim=%d, distance=COSINE)",
                    self._collection_name,
                    self._embedding_dim,
                )
            else:
                logger.debug("Qdrant collection '%s' already exists", self._collection_name)
        except Exception as e:
            logger.error("Failed to ensure Qdrant collection '%s': %s", self._collection_name, e)

    def index_failure(self, event: Event) -> None:
        """Index a failure event in Qdrant (non-blocking).

        Extracts metadata from STEP_FAILED, EXECUTION_COMPLETED(result=FAILED),
        or ALARM_RAISED events, computes an embedding, and stores the point in
        Qdrant.

        Runs in the background via asyncio.create_task — errors are logged but
        never raised to the caller.

        Args:
            event: The Event object with failure data.
        """
        if not self._should_index(event):
            return

        asyncio.create_task(self._index_failure_async(event))

    def handle_status_event(self, raw_event: dict[str, Any]) -> None:
        """Relay/queue entry point: index failures from a raw status envelope.

        The edge publishes status messages on ate.status.* as
        ``{"type": "<EVENT_TYPE>", "data": {...}}`` (the same envelope
        SSEBridge queues). ExecutionStatusRelay calls this for every consumed
        message AFTER the durable DB update and SSE push, so indexing never
        affects ack/nak or DB semantics. Only STEP_FAILED,
        EXECUTION_COMPLETED(FAILED), and ALARM_RAISED are indexed; other
        event types are a cheap no-op.

        Failures (unknown event type, missing data) are swallowed — indexing
        must never break the relay path.

        Args:
            raw_event: Raw status event dict with ``type`` and ``data`` keys.
        """
        try:
            raw_data = raw_event.get("data")
            data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
            event = Event(
                type=EventType(str(raw_event.get("type", ""))),
                data=data,
            )
        except (ValueError, KeyError, TypeError):
            # Unknown/unparseable event type — not indexable.
            return
        if self._should_index(event):
            self.index_failure(event)

    def _should_index(self, event: Event) -> bool:
        """Check whether this event should be indexed as a failure/alarm.

        Returns True for:
          - STEP_FAILED events
          - EXECUTION_COMPLETED events with status == "FAILED"
          - ALARM_RAISED events (SPC process alarms)
        """
        if event.type == EventType.STEP_FAILED:
            return True
        if event.type == EventType.EXECUTION_COMPLETED:
            data = event.data
            status = str(data.get("status", "")).upper() if isinstance(data, dict) else ""
            return status == "FAILED"
        if event.type == EventType.ALARM_RAISED:
            return True
        return False

    async def _index_failure_async(self, event: Event) -> None:
        """Async worker: extract metadata, embed, store in Qdrant."""
        try:
            metadata = self._extract_metadata(event)
            text = self._build_embed_text(metadata)
            vector = await self._embed(text)

            payload = {k: v for k, v in metadata.items() if v is not None}
            point_id = str(uuid.uuid4())

            from qdrant_client.http import models as qmodels

            self._qdrant_client.upsert(
                collection_name=self._collection_name,
                points=[
                    qmodels.PointStruct(
                        id=point_id,
                        vector=vector,
                        payload=payload,
                    ),
                ],
            )
            logger.debug(
                "Indexed failure: run_id=%s, step=%s",
                metadata.get("run_id"),
                metadata.get("failed_step_id"),
            )
            # Automatic failure→KG evolution (task 16). Best-effort and
            # awaited-but-non-fatal: the hook swallows its own failures; the
            # backstop here guarantees evolution can never break indexing.
            await self._trigger_evolution(metadata)
        except Exception as e:
            logger.error("Failed to index failure event: %s", e, exc_info=True)

    async def _trigger_evolution(self, metadata: dict[str, Any]) -> None:
        """Invoke the auto KG-evolution hook (best-effort; never raises)."""
        hook = self._evolution_hook
        if hook is None:
            return
        try:
            await hook(metadata)
        except Exception as e:  # noqa: BLE001 — indexing must survive evolution errors
            logger.warning("Auto KG evolution hook failed (indexing unaffected): %s", e)

    def _extract_metadata(self, event: Event) -> dict[str, Any]:
        """Extract metadata from event data.

        Grabs all known fields from _METADATA_FIELDS, plus stores the
        event_type and timestamp for completeness.
        """
        data = event.data if isinstance(event.data, dict) else {}
        metadata: dict[str, Any] = {
            "event_type": event.type.value,
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        }
        for field in _METADATA_FIELDS:
            value = data.get(field)
            if value is not None:
                metadata[field] = value
        # For STEP_FAILED, use step_id as failed_step_id if not already set
        if event.type == EventType.STEP_FAILED and "failed_step_id" not in metadata:
            metadata["failed_step_id"] = data.get("step_id")
        return metadata

    def _build_embed_text(self, metadata: dict[str, Any]) -> str:
        """Build text to embed from extracted metadata.

        Concatenates: failed_step_name + error_message + variable_snapshot
        for execution failures; for SPC alarms (ALARM_RAISED) it uses the
        measurement name, rule, and alarm message.
        """
        parts: list[str] = []
        step_name = metadata.get("failed_step_name") or metadata.get("failed_step_id", "")
        if step_name:
            parts.append(str(step_name))
        measurement = metadata.get("measurement_name")
        if measurement:
            parts.append(str(measurement))
        rule = metadata.get("rule")
        if rule:
            parts.append(str(rule))
        error_msg = (
            metadata.get("error_message")
            or metadata.get("message")
            or metadata.get("error")
            or ""
        )
        if error_msg:
            parts.append(str(error_msg))
        var_snapshot = metadata.get("variable_snapshot")
        if var_snapshot:
            parts.append(str(var_snapshot))
        return " ".join(parts)

    async def _embed(self, text: str) -> list[float]:
        """Compute embedding for text via the configured embedding backend.

        Uses ``embedding_service.embed`` when available (async), otherwise
        falls back to the sync ``embedding_model`` callable. Returns a
        zero-vector on failure (never raises).
        """
        if not text.strip():
            return [0.0] * self._embedding_dim
        try:
            if self._embedding_service is not None:
                return cast(list[float], await self._embedding_service.embed(text))
            if self._embedding_model is not None:
                return self._embedding_model(text)
            return [0.0] * self._embedding_dim
        except Exception as e:
            logger.error("Embedding failed: %s", e)
            return [0.0] * self._embedding_dim

    async def search_similar_failures(
        self,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Search for similar past failures via embedding + Qdrant.

        Args:
            query: Natural-language or error description to search.
            top_k: Maximum number of results to return.

        Returns:
            List of dicts with 'score', 'id', and payload metadata fields.
        """
        try:
            vector = await self._embed(query)
            results = self._qdrant_client.search(
                collection_name=self._collection_name,
                query_vector=vector,
                limit=top_k,
                with_payload=True,
            )
            return [
                {
                    "id": r.id,
                    "score": r.score,
                    **({} if r.payload is None else r.payload),
                }
                for r in results
            ]
        except Exception as e:
            logger.warning("Similarity search failed: %s", e)
            return []

    def subscribe_to_events(self, bridge: SSEBridge | None) -> None:
        """Subscribe to failure events via the SSE bridge.

        The FailureIndexer hooks into the bridge by wrapping its publish_event
        method to intercept STEP_FAILED and EXECUTION_COMPLETED events.

        In practice this means the indexer is called whenever the bridge
        publishes a failure-related event. This is a lightweight hook that
        adds no latency to the publishing path (indexing is async).

        Args:
            bridge: The SSEBridge instance, or None (no-op).
        """
        if bridge is None:
            logger.warning("No SSE bridge provided; failure indexing disabled")
            return

        # Wrap publish_event to intercept failure events
        original_publish = bridge.publish_event

        async def patched_publish(
            run_id: str, event_type: str, data: dict[str, Any]
        ) -> None:
            await original_publish(run_id, event_type, data)
            # Non-blocking index after publish
            try:
                event = Event(
                    type=EventType(event_type),
                    data=data,
                )
                if self._should_index(event):
                    self.index_failure(event)
            except (ValueError, KeyError):
                pass  # Unknown event type, skip

        bridge.publish_event = patched_publish  # type: ignore[method-assign]
        logger.info("Failure indexer subscribed to SSE bridge events")
