"""JetStream stream manager — creates required streams at startup.

Per AGENTS.md §7: stream creation is enforced at startup, not lazily.
If creation fails, startup crashes (no silent degradation).
"""

import json
import logging

from nats.aio.client import Client as NatsClient
from nats.js.api import AckPolicy, ConsumerConfig, RetentionPolicy, StreamConfig
from nats.js.errors import NotFoundError

logger = logging.getLogger(__name__)

# 7 days in seconds (nats-py StreamConfig.max_age is a float in seconds,
# converted to nanoseconds internally by the client).
_MAX_AGE_7_DAYS: int = 7 * 24 * 60 * 60

# 30 days in seconds — DLQ retention window for failed task messages.
_MAX_AGE_30_DAYS: int = 30 * 24 * 60 * 60

# ATE_TASKS — workqueue retention for task distribution to edge workers.
_ATE_TASKS_CONFIG = StreamConfig(
    name="ATE_TASKS",
    subjects=["ate.tasks.*"],
    retention=RetentionPolicy.WORK_QUEUE,
)

# ATE_STATUS — limits retention with 7-day max age for status events.
_ATE_STATUS_CONFIG = StreamConfig(
    name="ATE_STATUS",
    subjects=["ate.status.*"],
    retention=RetentionPolicy.LIMITS,
    max_age=_MAX_AGE_7_DAYS,
)

# ATE_DEAD_LETTERS — limits retention with 30-day max age for failed task
# dispatches. Messages are routed here after the ATE_TASKS consumer exhausts
# max_deliver (3) attempts. The consumer's metadata field records the DLQ
# routing policy (nats-py 2.15.0 has no native dead_letter_policy field, and
# NATS JetStream has no server-side auto-routing to DLQ — the application or
# an advisory subscriber publishes to ate.tasks.*.dlq on delivery failure).
_ATE_DEAD_LETTERS_CONFIG = StreamConfig(
    name="ATE_DEAD_LETTERS",
    subjects=["ate.tasks.*.dlq"],
    retention=RetentionPolicy.LIMITS,
    max_age=_MAX_AGE_30_DAYS,
)

# ATE_EXECUTION_EVENTS - limits retention with 7-day max age for recorded
# execution events (step transitions, measurements, operator interactions,
# scheduler decisions, NATS messages). Subject pattern ate.execution.*.events
# matches per-session recording streams: ate.execution.{session_id}.events.
_ATE_EXECUTION_EVENTS_CONFIG = StreamConfig(
    name="ATE_EXECUTION_EVENTS",
    subjects=["ate.execution.*.events"],
    retention=RetentionPolicy.LIMITS,
    max_age=_MAX_AGE_7_DAYS,
)

# All streams to create on startup, in creation order.
_STREAM_CONFIGS: tuple[StreamConfig, ...] = (
    _ATE_TASKS_CONFIG,
    _ATE_STATUS_CONFIG,
    _ATE_EXECUTION_EVENTS_CONFIG,
)

# --- Durable pull consumers ---
# ack_wait in seconds. ATE_TASKS: 5 min for long test execution; ATE_STATUS: 30s.
_ATE_TASKS_ACK_WAIT: int = 300
_ATE_STATUS_ACK_WAIT: int = 30
_MAX_DELIVER: int = 3

# Dead-letter routing metadata for the ATE_TASKS consumer. nats-py 2.15.0 has
# no dead_letter_policy field on ConsumerConfig, and NATS JetStream has no
# server-side auto-routing to a DLQ stream. This metadata records the routing
# policy so application code (or an advisory subscriber on
# $JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.ATE_TASKS.ate-worker) can publish
# failed messages to the ATE_DEAD_LETTERS stream after max_deliver is exhausted.
_ATE_TASKS_DLQ_METADATA: dict[str, str] = {
    "dead_letter_stream": "ATE_DEAD_LETTERS",
    "dead_letter_subject": "ate.tasks.dlq",
    "max_deliver": str(_MAX_DELIVER),
}

_ATE_TASKS_CONSUMER_CONFIG = ConsumerConfig(
    durable_name="ate-worker",
    ack_policy=AckPolicy.EXPLICIT,
    ack_wait=_ATE_TASKS_ACK_WAIT,
    max_deliver=_MAX_DELIVER,
    metadata=_ATE_TASKS_DLQ_METADATA,
)

_ATE_STATUS_CONSUMER_CONFIG = ConsumerConfig(
    durable_name="ate-status-relay",
    ack_policy=AckPolicy.EXPLICIT,
    ack_wait=_ATE_STATUS_ACK_WAIT,
    filter_subject="ate.status.*",
)

# (stream_name, consumer_config) pairs to create on startup, in creation order.
_CONSUMER_CONFIGS: tuple[tuple[str, ConsumerConfig], ...] = (
    ("ATE_TASKS", _ATE_TASKS_CONSUMER_CONFIG),
    ("ATE_STATUS", _ATE_STATUS_CONSUMER_CONFIG),
)

# --- Worker registry KV ---
# Per-key TTL: auto-expires worker entries if heartbeat stops (NATS 2.10+).
_WORKER_KV_BUCKET: str = "ate-workers"
_WORKER_KV_TTL_SECONDS: int = 30


class StreamManager:
    """Manages JetStream stream, consumer, and KV lifecycle for the ATE platform.

    Creates the required JetStream streams (ATE_TASKS, ATE_STATUS,
    ATE_EXECUTION_EVENTS, ATE_DEAD_LETTERS), durable pull consumers
    (ate-worker, ate-status-relay), and the worker registry KV bucket
    (ate-workers) at startup. All creation is idempotent: existing
    resources are left unchanged.

    Per AGENTS.md §7, creation failures are fatal - each method raises
    ``RuntimeError`` so the FastAPI lifespan aborts startup.
    """

    def __init__(self, nc: NatsClient) -> None:
        """Initialize the stream manager with a connected NATS client.

        Args:
            nc: A connected NATS client. ``nc.jetstream()`` must return a
                ``JetStreamContext`` (sync factory, no network I/O).
        """
        self._nc = nc

    async def create_streams(self) -> None:
        """Create all required JetStream streams idempotently.

        For each stream, checks if it already exists via ``stream_info``;
        if not (raises ``NotFoundError``), creates it via ``add_stream``.
        Any other exception during the check or creation is fatal and
        re-raised as ``RuntimeError``.

        Raises:
            RuntimeError: If stream verification or creation fails.
        """
        js = self._nc.jetstream()
        for config in _STREAM_CONFIGS:
            name = config.name
            if name is None:
                raise RuntimeError(f"Stream config is missing a name: {config}")
            try:
                await js.stream_info(name)
                logger.debug("Stream '%s' already exists, skipping", name)
            except NotFoundError:
                try:
                    await js.add_stream(config=config)
                    logger.info("Created JetStream stream '%s'", name)
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to create JetStream stream '{name}': {e}"
                    ) from e
            except Exception as e:
                raise RuntimeError(
                    f"Failed to verify JetStream stream '{name}': {e}"
                ) from e

    async def create_dead_letter_stream(self) -> None:
        """Create the ATE_DEAD_LETTERS stream idempotently.

        The DLQ stream uses limits retention with a 30-day max age and
        captures messages published to ``ate.tasks.*.dlq``. Must be called
        before ``create_consumers()`` — the ATE_TASKS consumer's metadata
        references this stream as the dead-letter destination.

        Raises:
            RuntimeError: If stream verification or creation fails.
        """
        js = self._nc.jetstream()
        config = _ATE_DEAD_LETTERS_CONFIG
        name = config.name
        if name is None:
            raise RuntimeError(f"Stream config is missing a name: {config}")
        try:
            await js.stream_info(name)
            logger.debug("Stream '%s' already exists, skipping", name)
        except NotFoundError:
            try:
                await js.add_stream(config=config)
                logger.info("Created JetStream stream '%s' (DLQ, 30-day retention)", name)
            except Exception as e:
                raise RuntimeError(
                    f"Failed to create JetStream stream '{name}': {e}"
                ) from e
        except Exception as e:
            raise RuntimeError(
                f"Failed to verify JetStream stream '{name}': {e}"
            ) from e

    async def create_consumers(self) -> None:
        """Create all required JetStream durable pull consumers idempotently.

        For each (stream, consumer) pair, checks if the consumer already
        exists via ``consumer_info``; if not (raises ``NotFoundError``),
        creates it via ``add_consumer``. Any other exception is fatal and
        re-raised as ``RuntimeError``.

        Raises:
            RuntimeError: If consumer verification or creation fails.
        """
        js = self._nc.jetstream()
        for stream_name, config in _CONSUMER_CONFIGS:
            durable = config.durable_name
            if durable is None:
                raise RuntimeError(f"Consumer config is missing durable_name: {config}")
            try:
                await js.consumer_info(stream_name, durable)
                logger.debug(
                    "Consumer '%s' on stream '%s' already exists, skipping",
                    durable, stream_name,
                )
            except NotFoundError:
                try:
                    await js.add_consumer(stream_name, config=config)
                    logger.info(
                        "Created JetStream consumer '%s' on stream '%s'",
                        durable, stream_name,
                    )
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to create JetStream consumer '{durable}' "
                        f"on stream '{stream_name}': {e}"
                    ) from e
            except Exception as e:
                raise RuntimeError(
                    f"Failed to verify JetStream consumer '{durable}' "
                    f"on stream '{stream_name}': {e}"
                ) from e

    async def create_kv_store(self) -> None:
        """Create the worker registry KV store idempotently.

        Creates the ``ate-workers`` KV bucket with a per-key TTL of 30
        seconds (NATS 2.10+). If the bucket already exists, it is left
        unchanged.

        Raises:
            RuntimeError: If KV verification or creation fails.
        """
        js = self._nc.jetstream()
        try:
            await js.key_value(_WORKER_KV_BUCKET)
            logger.debug("KV bucket '%s' already exists, skipping", _WORKER_KV_BUCKET)
        except NotFoundError:
            try:
                await js.create_key_value(
                    bucket=_WORKER_KV_BUCKET, ttl=_WORKER_KV_TTL_SECONDS
                )
                logger.info(
                    "Created KV bucket '%s' (TTL=%ss)",
                    _WORKER_KV_BUCKET, _WORKER_KV_TTL_SECONDS,
                )
            except Exception as e:
                raise RuntimeError(
                    f"Failed to create KV bucket '{_WORKER_KV_BUCKET}': {e}"
                ) from e
        except Exception as e:
            raise RuntimeError(
                f"Failed to verify KV bucket '{_WORKER_KV_BUCKET}': {e}"
            ) from e

    async def register_worker(
        self, worker_id: str, metadata: dict[str, object]
    ) -> int:
        """Register a worker in the KV heartbeat registry.

        Puts a JSON-serialized metadata entry at key ``workers.{worker_id}``
        in the ``ate-workers`` KV bucket. The bucket's per-key TTL (30s)
        auto-expires the entry if the worker stops heartbeating, so a
        worker must call this periodically to stay registered.

        Args:
            worker_id: Unique worker identifier.
            metadata: Worker metadata fields (hostname, capabilities,
                started_at, max_concurrent_tasks, current_tasks).

        Returns:
            The KV revision number of the put operation.

        Raises:
            RuntimeError: If the KV bucket is unavailable or the put fails.
        """
        js = self._nc.jetstream()
        try:
            kv = await js.key_value(_WORKER_KV_BUCKET)
        except Exception as e:
            raise RuntimeError(
                f"KV bucket '{_WORKER_KV_BUCKET}' not available "
                f"for worker registration: {e}"
            ) from e
        key = f"workers.{worker_id}"
        payload = json.dumps(metadata).encode("utf-8")
        try:
            revision = await kv.put(key, payload)
            logger.info(
                "Registered worker '%s' (key=%s, rev=%s)", worker_id, key, revision
            )
            return revision
        except Exception as e:
            raise RuntimeError(f"Failed to register worker '{worker_id}': {e}") from e
