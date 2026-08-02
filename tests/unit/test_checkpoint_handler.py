"""Unit tests for CheckpointHandler.

Covers the core lifecycle of an operator checkpoint:
- wait_for_response returns the submitted response (happy path)
- wait_for_response raises CheckpointTimeout when timeout_sec elapses
- submit_response returns False when no checkpoint is pending
- submit_response returns False when step_id mismatches
- cancel wakes the awaiting task with RuntimeError
- on_pending callback is invoked (and failures don't block the wait)
- has_pending / get_pending reflect the registry state
- cross-thread submit_response works (call_soon_threadsafe path)

The handler is async; tests use pytest-asyncio (asyncio_mode=auto).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ate_platform.executor.checkpoint_handler import (
    CheckpointHandler,
    CheckpointTimeoutError,
)
from shared.operator_checkpoint import OperatorCheckpoint, OperatorInteractionType


def _make_checkpoint(
    type_: OperatorInteractionType = OperatorInteractionType.SCAN,
    timeout_sec: float = 5.0,
    prompt: str = "Scan barcode",
    validation_regex: str | None = None,
) -> OperatorCheckpoint:
    """Build an OperatorCheckpoint with sensible test defaults."""
    return OperatorCheckpoint(
        type=type_,
        prompt=prompt,
        timeout_sec=timeout_sec,
        validation_regex=validation_regex,
    )


class TestCheckpointHandlerResponse:
    """Happy path: operator submits a response and the wait resumes."""

    async def test_wait_for_response_returns_submitted_response(self) -> None:
        """Given a pending checkpoint, when response is submitted, then wait returns it."""
        handler = CheckpointHandler()
        run_id = "run-001"
        step_id = "step-scan"
        checkpoint = _make_checkpoint(timeout_sec=2.0)

        # Spawn the waiter, then submit the response after a short delay.
        wait_task = asyncio.create_task(
            handler.wait_for_response(run_id, step_id, checkpoint)
        )
        # Yield to let the waiter register itself.
        await asyncio.sleep(0.05)
        assert handler.has_pending(run_id)

        submitted = handler.submit_response(
            run_id=run_id,
            step_id=step_id,
            response="SN-001",
        )
        assert submitted is True

        response = await asyncio.wait_for(wait_task, timeout=2.0)
        assert response.run_id == run_id
        assert response.step_id == step_id
        assert response.response == "SN-001"
        assert response.reason is None
        # Registry is cleaned up after the wait completes.
        assert not handler.has_pending(run_id)

    async def test_wait_for_response_carries_reason_and_extra(self) -> None:
        """Given a visual_check fail, when submitted, then reason and extra are preserved."""
        handler = CheckpointHandler()
        run_id = "run-002"
        step_id = "step-vc"
        checkpoint = _make_checkpoint(
            type_=OperatorInteractionType.VISUAL_CHECK,
            timeout_sec=2.0,
        )

        wait_task = asyncio.create_task(
            handler.wait_for_response(run_id, step_id, checkpoint)
        )
        await asyncio.sleep(0.05)

        handler.submit_response(
            run_id=run_id,
            step_id=step_id,
            response="fail",
            reason="Solder joint cracked",
            extra={"operator_id": "op-42"},
        )
        response = await asyncio.wait_for(wait_task, timeout=2.0)
        assert response.response == "fail"
        assert response.reason == "Solder joint cracked"
        assert response.extra == {"operator_id": "op-42"}

    async def test_on_pending_callback_invoked(self) -> None:
        """Given an on_pending callback, when checkpoint starts, then callback fires."""
        captured: list[tuple[str, str, OperatorCheckpoint]] = []

        async def on_pending(run_id: str, step_id: str, cp: OperatorCheckpoint) -> None:
            captured.append((run_id, step_id, cp))

        handler = CheckpointHandler(on_pending=on_pending)
        checkpoint = _make_checkpoint(timeout_sec=1.0)

        wait_task = asyncio.create_task(
            handler.wait_for_response("run-cb", "step-cb", checkpoint)
        )
        await asyncio.sleep(0.1)
        handler.submit_response("run-cb", "step-cb", "ok")
        await asyncio.wait_for(wait_task, timeout=2.0)

        assert len(captured) == 1
        assert captured[0][0] == "run-cb"
        assert captured[0][1] == "step-cb"
        assert captured[0][2] is checkpoint

    async def test_on_pending_failure_does_not_block_wait(self) -> None:
        """Given a failing on_pending callback, when checkpoint starts, then wait still proceeds."""

        async def on_pending(run_id: str, step_id: str, cp: OperatorCheckpoint) -> None:
            raise RuntimeError("adapter blew up")

        handler = CheckpointHandler(on_pending=on_pending)
        checkpoint = _make_checkpoint(timeout_sec=2.0)

        wait_task = asyncio.create_task(
            handler.wait_for_response("run-fail-cb", "step-fail-cb", checkpoint)
        )
        await asyncio.sleep(0.1)
        # The wait should still be alive despite the callback failure.
        assert handler.has_pending("run-fail-cb")
        handler.submit_response("run-fail-cb", "step-fail-cb", "ok")
        response = await asyncio.wait_for(wait_task, timeout=2.0)
        assert response.response == "ok"


class TestCheckpointHandlerTimeout:
    """Timeout path: the wait raises CheckpointTimeout after timeout_sec."""

    async def test_wait_for_response_raises_timeout(self) -> None:
        """Given no response, when timeout_sec elapses, then CheckpointTimeoutError is raised."""
        handler = CheckpointHandler()
        checkpoint = _make_checkpoint(timeout_sec=0.1)

        with pytest.raises(CheckpointTimeoutError, match="timed out"):
            await handler.wait_for_response("run-to", "step-to", checkpoint)

        # Registry is cleaned up even on timeout.
        assert not handler.has_pending("run-to")

    async def test_timeout_is_subclass_of_builtin_timeout_error(self) -> None:
        """CheckpointTimeoutError subclasses TimeoutError so callers can catch either."""
        assert issubclass(CheckpointTimeoutError, TimeoutError)


class TestCheckpointHandlerSubmit:
    """submit_response edge cases: no pending, step_id mismatch."""

    async def test_submit_response_returns_false_when_no_pending(self) -> None:
        """Given no pending checkpoint, when submit is called, then returns False."""
        handler = CheckpointHandler()
        assert handler.submit_response("run-none", "step-none", "x") is False

    async def test_submit_response_returns_false_on_step_mismatch(self) -> None:
        """Given a pending checkpoint, when step_id mismatches, then returns False."""
        handler = CheckpointHandler()
        checkpoint = _make_checkpoint(timeout_sec=2.0)

        wait_task = asyncio.create_task(
            handler.wait_for_response("run-mismatch", "step-a", checkpoint)
        )
        await asyncio.sleep(0.05)

        assert handler.submit_response("run-mismatch", "step-b", "x") is False
        # The original wait is still pending.
        assert handler.has_pending("run-mismatch")

        # Clean up: submit the correct response.
        handler.submit_response("run-mismatch", "step-a", "ok")
        await asyncio.wait_for(wait_task, timeout=2.0)


class TestCheckpointHandlerCancel:
    """cancel wakes the awaiting task with RuntimeError."""

    async def test_cancel_wakes_wait_with_runtime_error(self) -> None:
        """Given a pending checkpoint, when cancelled, then wait raises RuntimeError."""
        handler = CheckpointHandler()
        checkpoint = _make_checkpoint(timeout_sec=5.0)

        wait_task = asyncio.create_task(
            handler.wait_for_response("run-cancel", "step-cancel", checkpoint)
        )
        await asyncio.sleep(0.05)

        cancelled = handler.cancel("run-cancel")
        assert cancelled is True

        with pytest.raises(RuntimeError, match="cancelled"):
            await asyncio.wait_for(wait_task, timeout=2.0)

        assert not handler.has_pending("run-cancel")

    async def test_cancel_returns_false_when_no_pending(self) -> None:
        """Given no pending checkpoint, when cancel is called, then returns False."""
        handler = CheckpointHandler()
        assert handler.cancel("run-none") is False


class TestCheckpointHandlerCrossThread:
    """submit_response from a different thread uses call_soon_threadsafe."""

    async def test_submit_response_from_another_thread(self) -> None:
        """Given a wait on loop A, when submit arrives from thread B, then wait resumes."""
        import threading

        handler = CheckpointHandler()
        checkpoint = _make_checkpoint(timeout_sec=3.0)
        run_id = "run-thread"
        step_id = "step-thread"
        result_box: dict[str, Any] = {"submitted": False}

        wait_task = asyncio.create_task(
            handler.wait_for_response(run_id, step_id, checkpoint)
        )
        await asyncio.sleep(0.05)

        def submit_from_thread() -> None:
            result_box["submitted"] = handler.submit_response(
                run_id=run_id, step_id=step_id, response="from-thread",
            )

        thread = threading.Thread(target=submit_from_thread)
        thread.start()
        thread.join(timeout=2.0)

        assert result_box["submitted"] is True
        response = await asyncio.wait_for(wait_task, timeout=2.0)
        assert response.response == "from-thread"


class TestCheckpointHandlerRegistry:
    """has_pending / get_pending reflect the registry."""

    async def test_get_pending_returns_none_when_no_checkpoint(self) -> None:
        """Given no pending checkpoint, when get_pending is called, then returns None."""
        handler = CheckpointHandler()
        assert handler.get_pending("run-x") is None

    async def test_get_pending_returns_entry_while_waiting(self) -> None:
        """Given a pending checkpoint, when get_pending is called, then returns the entry."""
        handler = CheckpointHandler()
        checkpoint = _make_checkpoint(timeout_sec=2.0)

        wait_task = asyncio.create_task(
            handler.wait_for_response("run-get", "step-get", checkpoint)
        )
        await asyncio.sleep(0.05)

        entry = handler.get_pending("run-get")
        assert entry is not None
        assert entry.step_id == "step-get"
        assert entry.checkpoint is checkpoint
        assert entry.response is None
        assert entry.created_at is not None

        handler.submit_response("run-get", "step-get", "ok")
        await asyncio.wait_for(wait_task, timeout=2.0)

    async def test_pending_entry_replaced_on_new_wait(self) -> None:
        """Given a stale pending entry, when a new wait starts, then the entry is replaced."""
        handler = CheckpointHandler()
        checkpoint1 = _make_checkpoint(prompt="first", timeout_sec=5.0)

        # Start a wait that we will not resolve -- it stays pending.
        wait1 = asyncio.create_task(
            handler.wait_for_response("run-replace", "step-1", checkpoint1)
        )
        await asyncio.sleep(0.05)
        entry1 = handler.get_pending("run-replace")
        assert entry1 is not None and entry1.step_id == "step-1"

        # Cancel the first wait so a new wait can register on the same run.
        handler.cancel("run-replace")
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(wait1, timeout=2.0)

        checkpoint2 = _make_checkpoint(prompt="second", timeout_sec=2.0)
        wait2 = asyncio.create_task(
            handler.wait_for_response("run-replace", "step-2", checkpoint2)
        )
        await asyncio.sleep(0.05)
        entry2 = handler.get_pending("run-replace")
        assert entry2 is not None and entry2.step_id == "step-2"
        assert entry2.checkpoint.prompt == "second"

        handler.submit_response("run-replace", "step-2", "ok")
        await asyncio.wait_for(wait2, timeout=2.0)
