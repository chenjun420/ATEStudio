"""ExecutionStatusRelay — background task bridging ATE_STATUS NATS to DB + SSE.

Subscribes to the ATE_STATUS JetStream stream via a durable pull consumer
("ate-status-relay", created at startup by StreamManager). For each status
message the relay:

1. Updates the Execution DB record — appends the step result to the
   ``step_results`` JSON column (dict keyed by ``step_id``) and updates the
   execution ``status`` when the event carries an execution-level status.
2. Pushes the event to ``SSEBridge.push_to_queue_only()`` — the LOCAL
   asyncio.Queue only, with NO NATS publish. This prevents a feedback loop
   (the event already arrived FROM NATS).
3. Acks the JetStream message ONLY after both the DB update and the SSE
   push succeed. On either failure the message is nacked so JetStream
   redelivers it.

The relay is wired as an ``asyncio.create_task(relay.start())`` background
task in the FastAPI lifespan (see ``ate_cloud.main``).
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.execution import Execution
from ate_cloud.nats.sse_bridge import SSEBridge

logger = logging.getLogger(__name__)

# JetStream pull consumer config — binds to the durable consumer created by
# StreamManager.create_consumers() (Todo 4).
_ATE_STATUS_STREAM = "ATE_STATUS"
_ATE_STATUS_SUBJECT = "ate.status.*"
_ATE_STATUS_DURABLE = "ate-status-relay"

# Fetch tuning: small batch, short timeout for responsive shutdown.
_FETCH_BATCH: int = 1
_FETCH_TIMEOUT: float = 1.0

# Execution-level statuses that update the Execution.status column.
# Step-level statuses (PASSED, FAILED, SKIPPED, etc.) only update step_results.
_EXECUTION_STATUSES: frozenset[str] = frozenset(
    {"PENDING", "RUNNING", "COMPLETED", "FAILED", "ABORTED"}
)


class ExecutionStatusRelay:
    """Background relay from ATE_STATUS JetStream to DB + SSE queue.

    The relay binds to the existing durable pull consumer "ate-status-relay"
    on the ATE_STATUS stream. Each fetched message updates the Execution DB
    record and is pushed to the SSE bridge's local queue (without republishing
    to NATS, avoiding a feedback loop).

    The message is acked only after both the DB update and the SSE push
    complete successfully. On failure, the message is nacked so JetStream
    redelivers it.
    """

    def __init__(
        self,
        nats_client: NatsClient,
        sse_bridge: SSEBridge,
        async_session_factory: Callable[[], AsyncSession],
    ) -> None:
        """Initialize the relay.

        Args:
            nats_client: A connected NATS client. ``nats_client.jetstream()``
                must return a ``JetStreamContext`` (sync factory, no I/O).
            sse_bridge: The SSE bridge used to push events to the local queue.
            async_session_factory: Factory that returns a new ``AsyncSession``
                for DB updates. Used as ``async with factory() as session:``.
        """
        self._nc = nats_client
        self._sse_bridge = sse_bridge
        self._session_factory = async_session_factory
        self._psub: JetStreamContext.PullSubscription | None = None
        self._running = False

    async def start(self) -> None:
        """Bind to the durable pull consumer and run the consume loop.

        This coroutine blocks until ``stop()`` is called or the task is
        cancelled. Wire as ``asyncio.create_task(relay.start())`` in the
        FastAPI lifespan; cancel the task on shutdown.
        """
        js = self._nc.jetstream()
        self._psub = await js.pull_subscribe(
            _ATE_STATUS_SUBJECT, durable=_ATE_STATUS_DURABLE
        )
        self._running = True
        logger.info(
            "ExecutionStatusRelay started (stream=%s, durable=%s)",
            _ATE_STATUS_STREAM, _ATE_STATUS_DURABLE,
        )
        try:
            await self._consume_loop()
        finally:
            self._running = False
            await self._cleanup()

    async def stop(self) -> None:
        """Signal the relay to stop and clean up the pull subscription.

        Sets ``_running = False`` so the consume loop exits on the next
        iteration. For immediate shutdown, cancel the task wrapping
        ``start()`` — the ``CancelledError`` propagates through ``fetch()``
        and the ``finally`` block in ``start()`` runs cleanup.
        """
        self._running = False
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Unsubscribe the pull subscription if active."""
        if self._psub is not None:
            try:
                await self._psub.unsubscribe()
            except Exception as e:
                logger.warning("Error unsubscribing relay pull subscription: %s", e)
            finally:
                self._psub = None

    async def _consume_loop(self) -> None:
        """Pull messages from JetStream and dispatch to ``_process_message``."""
        while self._running and self._psub is not None:
            try:
                msgs = await self._psub.fetch(
                    batch=_FETCH_BATCH, timeout=_FETCH_TIMEOUT
                )
            except TimeoutError:
                # Normal idle polling — yield to event loop to avoid tight spin.
                await asyncio.sleep(0)
                continue
            except asyncio.CancelledError:
                logger.debug("Relay consume loop cancelled")
                break
            except Exception as e:
                logger.error("Error fetching from ATE_STATUS: %s", e)
                await asyncio.sleep(1.0)
                continue

            for msg in msgs:
                await self._process_message(msg)

    async def _process_message(self, msg: Any) -> None:
        """Process a single status message: parse, update DB, push to SSE, ack.

        The message is acked ONLY after both the DB update and the SSE push
        succeed. On any failure the message is nacked so JetStream redelivers
        it.

        Args:
            msg: The JetStream message (has ``.data`` bytes, ``.ack()``,
                ``.nak()`` async methods).
        """
        try:
            event: dict[str, Any] = json.loads(msg.data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.error("Failed to parse status message JSON: %s", e)
            await msg.nak()
            return

        try:
            await self._update_db(event)
            run_id = event.get("run_id", "")
            await self._sse_bridge.push_to_queue_only(run_id, event)
            await msg.ack()
        except Exception as e:
            logger.error(
                "Failed to process status message for run_id=%s "
                "(will be redelivered): %s",
                event.get("run_id"), e,
            )
            await msg.nak()

    async def _update_db(self, event: dict[str, Any]) -> None:
        """Update the Execution DB record with the status event.

        Appends the step result to ``step_results`` (dict keyed by
        ``step_id``) and updates ``status`` when the event carries an
        execution-level status. If the execution record is not found, logs
        a warning and returns without raising (the message is still acked —
        redelivery won't help for a missing execution).

        Args:
            event: The parsed status event dict.
        """
        run_id = event.get("run_id")
        if not run_id:
            logger.warning("Status event has no run_id, skipping DB update")
            return

        async with self._session_factory() as session:
            result = await session.execute(
                select(Execution).where(Execution.id == run_id)
            )
            execution = result.scalar_one_or_none()
            if execution is None:
                logger.warning(
                    "Execution not found for run_id=%s, skipping DB update", run_id
                )
                return

            step_id = event.get("step_id")
            if step_id is not None:
                step_results = dict(execution.step_results or {})
                step_results[step_id] = {
                    "status": event.get("status"),
                    "outputs": event.get("outputs"),
                    "timestamp": event.get("timestamp"),
                }
                execution.step_results = step_results

            status = event.get("status")
            if status in _EXECUTION_STATUSES:
                execution.status = status

            await session.commit()
