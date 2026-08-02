"""Tests for ExecutionStatusRelay (Todo 9).

Verifies that the relay:
1. Updates the Execution DB record (status + step_results) on each status message
2. Pushes events to SSEBridge.push_to_queue_only() (NOT publish_event)
3. Handles out-of-order step messages correctly (dict keyed by step_id)
4. Acks messages ONLY after both DB update and SSE push succeed (naks on failure)
5. Is wired as a background asyncio task in the FastAPI lifespan
"""

import json
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ate_cloud.main as main_module
from ate_cloud.main import lifespan
from ate_cloud.services.execution_status_relay import ExecutionStatusRelay

# --- Test fixtures and helpers ---


class _FakeExecution:
    """Minimal mutable Execution stand-in for DB update tests."""

    def __init__(
        self,
        id: str = "run-1",
        status: str = "PENDING",
        step_results: dict | None = None,
    ) -> None:
        self.id = id
        self.status = status
        self.step_results = step_results


class _FakeMsg:
    """JetStream message stand-in with bytes ``.data`` and async ack/nak."""

    def __init__(self, data: dict) -> None:
        self.data = json.dumps(data).encode()
        self.ack = AsyncMock()
        self.nak = AsyncMock()


class _FakeSessionCtx:
    """Async context manager wrapping a mock AsyncSession."""

    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *args: object) -> bool:
        return False


def _make_session_factory(
    execution: _FakeExecution | None,
) -> tuple[MagicMock, AsyncMock]:
    """Build a mock async_session_factory and the underlying mock session.

    The factory returns a ``_FakeSessionCtx`` whose session.execute returns
    a result whose scalar_one_or_none returns ``execution``.

    Args:
        execution: The Execution object the DB query returns (or None).

    Returns:
        (factory, mock_session) — the callable factory and the mock session.
    """
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = execution
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    factory = MagicMock(return_value=_FakeSessionCtx(mock_session))
    return factory, mock_session


def _make_relay(
    execution: _FakeExecution | None = None,
    sse_bridge: MagicMock | None = None,
) -> tuple[ExecutionStatusRelay, AsyncMock, MagicMock, _FakeExecution]:
    """Build a relay with fully mocked dependencies.

    Args:
        execution: The Execution the mock DB returns. Created fresh if None.
        sse_bridge: Optional pre-built SSE bridge mock. Created if None.

    Returns:
        (relay, mock_session, mock_sse, execution) — the relay and its mocks.
    """
    if execution is None:
        execution = _FakeExecution()
    if sse_bridge is None:
        sse_bridge = MagicMock()
        sse_bridge.push_to_queue_only = AsyncMock()
        sse_bridge.publish_event = AsyncMock()
    factory, mock_session = _make_session_factory(execution)
    relay = ExecutionStatusRelay(
        nats_client=MagicMock(),
        sse_bridge=sse_bridge,
        async_session_factory=factory,
    )
    return relay, mock_session, sse_bridge, execution


# --- Lifespan test fixtures (mirrors test_main_nats_startup.py) ---


@pytest.fixture
def mock_app() -> MagicMock:
    """Create a mock FastAPI app with a state attribute for lifespan."""
    app = MagicMock()
    app.state = MagicMock()
    return app


@pytest.fixture(autouse=True)
def reset_nats_client() -> Generator[None, None, None]:
    """Reset the module-level _nats_client to None before and after each test."""
    saved = main_module._nats_client
    main_module._nats_client = None
    yield
    main_module._nats_client = saved


class TestExecutionStatusRelay:
    """Tests for ExecutionStatusRelay message processing and lifespan wiring."""

    @pytest.mark.asyncio
    async def test_relay_updates_db(self) -> None:
        """Relay updates Execution.status and appends to step_results on each message."""
        execution = _FakeExecution(id="run-1", status="PENDING")
        relay, mock_session, _, _ = _make_relay(execution=execution)

        event = {
            "run_id": "run-1",
            "step_id": "step-1",
            "status": "RUNNING",
            "outputs": {"voltage": 5.0},
            "timestamp": 1234567890,
        }
        await relay._process_message(_FakeMsg(event))

        # DB session was used: execute called, commit awaited
        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

        # Execution status updated (RUNNING is an execution-level status)
        assert execution.status == "RUNNING"

        # step_results updated with the step entry
        assert execution.step_results is not None
        assert "step-1" in execution.step_results
        assert execution.step_results["step-1"]["status"] == "RUNNING"
        assert execution.step_results["step-1"]["outputs"] == {"voltage": 5.0}

    @pytest.mark.asyncio
    async def test_relay_pushes_to_push_to_queue_only(self) -> None:
        """Relay calls push_to_queue_only (NOT publish_event) to avoid NATS feedback loop."""
        relay, _, mock_sse, _ = _make_relay()

        event = {
            "run_id": "run-1",
            "step_id": "step-1",
            "status": "PASSED",
            "outputs": {"result": "ok"},
        }
        msg = _FakeMsg(event)
        await relay._process_message(msg)

        # push_to_queue_only called with run_id and the raw event dict
        mock_sse.push_to_queue_only.assert_awaited_once_with("run-1", event)

        # publish_event was NOT called — prevents NATS feedback loop
        mock_sse.publish_event.assert_not_awaited()

        # Message was acked (both DB and SSE succeeded)
        msg.ack.assert_awaited_once()
        msg.nak.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_out_of_order_handling(self) -> None:
        """Out-of-order step messages both land in step_results (dict keyed by step_id)."""
        execution = _FakeExecution(id="run-1", status="RUNNING")
        relay, mock_session, _, _ = _make_relay(execution=execution)

        # Process step-2 BEFORE step-1 (out of order)
        event_step2 = {
            "run_id": "run-1",
            "step_id": "step-2",
            "status": "PASSED",
            "outputs": {"current": 2.1},
            "timestamp": 200,
        }
        await relay._process_message(_FakeMsg(event_step2))

        # step-2 is in step_results, step-1 is not yet
        assert "step-2" in execution.step_results
        assert "step-1" not in execution.step_results

        # Now process step-1 (arrived late)
        event_step1 = {
            "run_id": "run-1",
            "step_id": "step-1",
            "status": "PASSED",
            "outputs": {"voltage": 5.0},
            "timestamp": 100,
        }
        await relay._process_message(_FakeMsg(event_step1))

        # Both steps are present — dict keyed by step_id handles any order
        assert "step-1" in execution.step_results
        assert "step-2" in execution.step_results
        assert execution.step_results["step-1"]["outputs"] == {"voltage": 5.0}
        assert execution.step_results["step-2"]["outputs"] == {"current": 2.1}

        # Two DB commits (one per message)
        assert mock_session.commit.await_count == 2

    @pytest.mark.asyncio
    async def test_ack_after_db_and_sse(self) -> None:
        """Message is acked ONLY after both DB commit and SSE push; nacked on either failure."""
        # --- Case 1: both succeed → ack called after commit and push ---
        execution = _FakeExecution(id="run-1")
        relay, mock_session, mock_sse, _ = _make_relay(execution=execution)

        call_order: list[str] = []

        async def _track_commit() -> None:
            call_order.append("db_commit")

        async def _track_push(run_id: str, event: dict) -> None:
            call_order.append("sse_push")

        mock_session.commit.side_effect = _track_commit
        mock_sse.push_to_queue_only.side_effect = _track_push

        msg_ok = _FakeMsg({"run_id": "run-1", "step_id": "s1", "status": "PASSED"})
        msg_ok.ack.side_effect = lambda: call_order.append("ack")  # type: ignore[method-assign]
        await relay._process_message(msg_ok)

        assert call_order == ["db_commit", "sse_push", "ack"]
        msg_ok.ack.assert_awaited_once()

        # --- Case 2: DB failure → ack NOT called, nak called ---
        relay2, mock_session2, _, _ = _make_relay(execution=_FakeExecution(id="run-2"))
        mock_session2.commit.side_effect = RuntimeError("DB connection lost")

        msg_db_fail = _FakeMsg({"run_id": "run-2", "step_id": "s1", "status": "PASSED"})
        await relay2._process_message(msg_db_fail)

        msg_db_fail.ack.assert_not_awaited()
        msg_db_fail.nak.assert_awaited_once()

        # --- Case 3: SSE push failure → ack NOT called, nak called ---
        relay3, _, mock_sse3, _ = _make_relay(execution=_FakeExecution(id="run-3"))
        mock_sse3.push_to_queue_only.side_effect = RuntimeError("SSE queue full")

        msg_sse_fail = _FakeMsg({"run_id": "run-3", "step_id": "s1", "status": "PASSED"})
        await relay3._process_message(msg_sse_fail)

        msg_sse_fail.ack.assert_not_awaited()
        msg_sse_fail.nak.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_wiring(self, mock_app: MagicMock) -> None:
        """Relay is constructed and started as a background task in lifespan."""
        mock_nc = AsyncMock()
        mock_nc.is_connected = True
        mock_js = AsyncMock()
        mock_js.account_info = AsyncMock(return_value=MagicMock())
        mock_nc.jetstream = MagicMock(return_value=mock_js)
        mock_bridge = AsyncMock()
        mock_indexer = MagicMock()
        mock_indexer.ensure_collection = AsyncMock()
        mock_relay = AsyncMock()

        with (
            patch("ate_cloud.main.nats.connect", new=AsyncMock(return_value=mock_nc)),
            patch("ate_cloud.main.SSEBridge", return_value=mock_bridge),
            patch("ate_cloud.main.FailureIndexer", return_value=mock_indexer),
            patch("ate_cloud.main.ScriptVersioningService"),
            patch("ate_cloud.main.ExecutionStatusRelay", return_value=mock_relay),
        ):
            async with lifespan(mock_app):
                # The relay was constructed (patched) and stored on app.state
                assert mock_app.state.status_relay is mock_relay

                # start() was called via asyncio.create_task — await the task
                # to ensure the mock coroutine completes before asserting
                relay_task = mock_app.state.status_relay_task
                assert relay_task is not None
                await relay_task
                mock_relay.start.assert_awaited_once()

            # On shutdown, the task was handled (done or cancelled)
            assert mock_app.state.status_relay_task.done()
