"""CheckpointHandler - pauses executor until an operator responds or timeout.

When a sequence step declares an ``operator_checkpoint``, the
:class:`~ate_platform.executor.process_executor.ProcessExecutor` calls
:meth:`CheckpointHandler.wait_for_response` *before* dispatching the
step's script. The handler:

1. Registers a pending checkpoint keyed by ``run_id`` (in-memory dict,
   mirroring the ``app.state.recorders`` pattern on the cloud side).
2. Publishes a pending-checkpoint event via the optional event bus /
   NATS subject ``ate.checkpoint.{run_id}`` so the operator UI can
   render the modal.
3. Awaits an :class:`asyncio.Event` that is set when the cloud API
   receives the operator's response and calls :meth:`submit_response`.
4. Returns the response, or raises ``TimeoutError`` if
   ``checkpoint.timeout_sec`` elapses first.

The handler is single-instance per executor (created in
``ProcessExecutor.__init__``). It is thread-safe for the
``submit_response``/``cancel`` entrypoints (they use
``call_soon_threadsafe`` to set the event on the executor's loop),
which is required because :meth:`ProcessExecutor.execute` runs in a
worker thread (ThreadPoolExecutor path) while the response arrives on
the FastAPI event loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from shared.operator_checkpoint import OperatorCheckpoint

logger = logging.getLogger(__name__)

__all__ = ["CheckpointHandler", "PendingCheckpoint", "CheckpointResponse", "CheckpointTimeoutError"]


class CheckpointTimeoutError(TimeoutError):
    """Raised when a checkpoint wait exceeds its ``timeout_sec``.

    Subclass of the builtin :class:`TimeoutError` so callers can catch
    either the specific type or the builtin. Carries the ``run_id`` and
    ``step_id`` for diagnostic context.
    """


@dataclass
class CheckpointResponse:
    """The operator's response to a checkpoint, returned to the executor.

    Attributes:
        run_id: Execution run identifier.
        step_id: Step identifier that triggered the checkpoint.
        response: The operator's response payload (text/acknowledgement).
        reason: Optional reason (e.g. visual_check fail reason).
        extra: Optional metadata bag.
    """

    run_id: str
    step_id: str
    response: str
    reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingCheckpoint:
    """Bookkeeping for a checkpoint awaiting an operator response.

    Attributes:
        run_id: Execution run identifier.
        step_id: Step identifier that triggered the checkpoint.
        checkpoint: The full checkpoint definition.
        event: ``asyncio.Event`` set when a response arrives (or cancel).
        response: The response once submitted (None while pending).
        loop: The event loop the awaiting task runs on. Used so
            ``submit_response`` (called from a different thread) can
            safely wake the awaiting task via ``call_soon_threadsafe``.
        timed_out: Set True when the timeout fired (diagnostic).
        cancelled: Set True when :meth:`CheckpointHandler.cancel` was
            called (e.g. execution aborted).
    """

    run_id: str
    step_id: str
    checkpoint: OperatorCheckpoint
    event: asyncio.Event
    response: CheckpointResponse | None = None
    loop: asyncio.AbstractEventLoop | None = None
    timed_out: bool = False
    cancelled: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class CheckpointHandler:
    """Manages pending operator checkpoints and unblocks the executor.

    Lifecycle:

    - ``wait_for_response(run_id, step_id, checkpoint)`` is called from
      the executor (worker thread). It registers a pending checkpoint,
      invokes the optional ``on_pending`` callback (used by the cloud
      adapter to publish an SSE event + NATS message), then awaits the
      ``asyncio.Event`` with a timeout.
    - ``submit_response(run_id, step_id, response)`` is called when the
      cloud API receives the operator's submission. It sets the
      pending checkpoint's response and wakes the awaiting task.
    - ``cancel(run_id)`` aborts any pending checkpoint for a run (used
      when the execution is aborted).

    The handler stores pending checkpoints in a plain ``dict`` keyed by
    ``run_id`` -- at most one checkpoint is pending per run because the
    executor runs steps serially within a run (parallel execution is
    across runs, not within a run).
    """

    def __init__(
        self,
        on_pending: Any | None = None,
    ) -> None:
        """Initialize the handler.

        Args:
            on_pending: Optional async callback invoked when a checkpoint
                becomes pending. Signature::

                    async def on_pending(
                        run_id: str,
                        step_id: str,
                        checkpoint: OperatorCheckpoint,
                    ) -> None

                Used by the cloud adapter to publish an SSE event and a
                NATS message on ``ate.checkpoint.{run_id}``. May be
                ``None`` for unit tests that drive the handler directly.
        """
        self._pending: dict[str, PendingCheckpoint] = {}
        self._lock = threading.Lock()
        self._on_pending = on_pending

    # ------------------------------------------------------------------
    # Public: called from the executor (worker thread)
    # ------------------------------------------------------------------

    def has_pending(self, run_id: str) -> bool:
        """Return True if a checkpoint is currently pending for ``run_id``."""
        with self._lock:
            return run_id in self._pending

    def get_pending(self, run_id: str) -> PendingCheckpoint | None:
        """Return the pending checkpoint for ``run_id`` or ``None``.

        Used by the cloud adapter to build the ``GET .../checkpoint/pending``
        response without holding the lock across the read.
        """
        with self._lock:
            return self._pending.get(run_id)

    async def wait_for_response(
        self,
        run_id: str,
        step_id: str,
        checkpoint: OperatorCheckpoint,
    ) -> CheckpointResponse:
        """Block until the operator responds or ``checkpoint.timeout_sec`` elapses.

        Called from the executor (typically inside a worker thread via
        ``asyncio.run``-style bridge, or directly on the executor's loop
        for the async execution path). The handler is safe to call from
        a thread different from the one that registered the loop: it
        captures ``asyncio.get_running_loop()`` here and uses
        ``call_soon_threadsafe`` from ``submit_response``.

        Args:
            run_id: Execution run identifier.
            step_id: Step identifier that triggered the checkpoint.
            checkpoint: The checkpoint definition (type, prompt, timeout).

        Returns:
            The operator's response.

        Raises:
            CheckpointTimeout: If ``checkpoint.timeout_sec`` elapses
                before a response arrives.
            RuntimeError: If the checkpoint was cancelled (e.g. the
                execution was aborted) -- callers should translate this
                into a step ERROR.
        """
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        pending = PendingCheckpoint(
            run_id=run_id,
            step_id=step_id,
            checkpoint=checkpoint,
            event=event,
            loop=loop,
        )
        with self._lock:
            # At most one pending checkpoint per run -- the executor runs
            # steps serially within a run. If a stale entry exists (e.g.
            # previous timeout not yet cleaned up), overwrite it.
            self._pending[run_id] = pending

        logger.info(
            "Checkpoint pending: run_id=%s step_id=%s type=%s timeout=%.1fs",
            run_id, step_id, checkpoint.type.value, checkpoint.timeout_sec,
        )

        # Notify the cloud adapter (publish SSE + NATS) if configured.
        if self._on_pending is not None:
            try:
                await self._on_pending(run_id, step_id, checkpoint)
            except Exception:
                # The adapter failing must not block the checkpoint wait;
                # the operator can still query GET .../checkpoint/pending.
                logger.exception("on_pending callback failed for run_id=%s", run_id)

        try:
            try:
                await asyncio.wait_for(event.wait(), timeout=checkpoint.timeout_sec)
            except TimeoutError as exc:
                pending.timed_out = True
                raise CheckpointTimeoutError(
                    f"Operator checkpoint timed out after {checkpoint.timeout_sec}s "
                    f"(run_id={run_id}, step_id={step_id})"
                ) from exc

            if pending.cancelled:
                raise RuntimeError(
                    f"Checkpoint cancelled (run_id={run_id}, step_id={step_id})"
                )
            if pending.response is None:
                # Event was set without a response -- treat as cancelled.
                raise RuntimeError(
                    f"Checkpoint event set without response (run_id={run_id}, step_id={step_id})"
                )
            return pending.response
        finally:
            with self._lock:
                # Only clear if this is still our pending entry (a later
                # wait_for_response may have already replaced it).
                current = self._pending.get(run_id)
                if current is pending:
                    self._pending.pop(run_id, None)

    # ------------------------------------------------------------------
    # Public: called from the cloud API / control path (event loop thread)
    # ------------------------------------------------------------------

    def submit_response(
        self,
        run_id: str,
        step_id: str,
        response: str,
        reason: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """Submit the operator's response and wake the awaiting executor.

        Thread-safe: may be called from a different thread than the one
        that called :meth:`wait_for_response`. Uses
        ``call_soon_threadsafe`` on the captured loop to set the event.

        Args:
            run_id: Execution run identifier.
            step_id: Step identifier being answered (must match).
            response: The operator's response payload.
            reason: Optional reason (e.g. visual_check fail reason).
            extra: Optional metadata bag.

        Returns:
            True if a pending checkpoint was found and woken, False if
            no pending checkpoint exists for ``run_id`` (the operator
            submitted too late or for the wrong run).
        """
        with self._lock:
            pending = self._pending.get(run_id)
        if pending is None:
            logger.warning(
                "Response submitted but no pending checkpoint: run_id=%s step_id=%s",
                run_id, step_id,
            )
            return False
        if pending.step_id != step_id:
            logger.warning(
                "Response step_id mismatch: pending=%s submitted=%s (run_id=%s)",
                pending.step_id, step_id, run_id,
            )
            return False

        pending.response = CheckpointResponse(
            run_id=run_id,
            step_id=step_id,
            response=response,
            reason=reason,
            extra=extra or {},
        )

        loop = pending.loop
        if loop is not None:
            # Wake the awaiting task on its own loop, thread-safe.
            loop.call_soon_threadsafe(pending.event.set)
        else:
            # Same-loop fast path (or no loop captured); set directly.
            pending.event.set()

        logger.info(
            "Checkpoint response submitted: run_id=%s step_id=%s",
            run_id, step_id,
        )
        return True

    def cancel(self, run_id: str) -> bool:
        """Cancel any pending checkpoint for ``run_id`` (e.g. on abort).

        Marks the pending checkpoint as cancelled and wakes the awaiting
        executor task, which will raise :class:`RuntimeError`.

        Args:
            run_id: Execution run identifier.

        Returns:
            True if a pending checkpoint was cancelled, False otherwise.
        """
        with self._lock:
            pending = self._pending.get(run_id)
        if pending is None:
            return False
        pending.cancelled = True
        loop = pending.loop
        if loop is not None:
            loop.call_soon_threadsafe(pending.event.set)
        else:
            pending.event.set()
        logger.info("Checkpoint cancelled: run_id=%s step_id=%s", run_id, pending.step_id)
        return True
