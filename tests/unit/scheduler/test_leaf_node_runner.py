"""Unit tests for LeafNodeRunner.

Tests cover:
- start() connects to local and remote NATS, starts worker
- start() succeeds even when remote is unreachable (WAN down mode)
- is_wan_connected returns cached WAN status
- publish_upstream publishes directly when WAN is up
- publish_upstream buffers when WAN is down
- publish_upstream buffers on publish failure (WAN drops mid-publish)
- sync_backlog replays all buffered messages when WAN is up
- sync_backlog keeps failed messages in buffer (partial failure)
- sync_backlog is skipped when WAN is down or buffer is empty
- reconnected_cb triggers sync_backlog
- disconnected_cb sets wan_connected=False
- _check_wan publishes ping to detect WAN status
- _check_wan returns False on publish error or disconnected remote
- WAN down: worker still processes local tasks, messages buffer
- Full cycle: WAN down → buffer → WAN reconnect → sync_backlog replays
- stop() cleans up all connections and tasks
- stop() handles missing remote connection gracefully
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ate_platform.scheduler.leaf_node_runner import (
    BufferedMessage,
    LeafNodeRunner,
)


def _make_mock_nc(connected: bool = True) -> MagicMock:
    """Create a mock NATS client with all methods LeafNodeRunner uses."""
    mock_nc = MagicMock()
    mock_nc.is_connected = connected
    mock_nc.publish = AsyncMock()
    mock_nc.flush = AsyncMock()
    mock_nc.close = AsyncMock()
    mock_nc.drain = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=MagicMock())
    return mock_nc


def _make_mock_worker() -> MagicMock:
    """Create a mock JetStreamWorker with async start/stop."""
    mock_worker = MagicMock()
    mock_worker.start = AsyncMock()
    mock_worker.stop = AsyncMock()
    mock_worker.pull_and_process_one = AsyncMock(return_value=True)
    mock_worker.worker_id = "test-worker-id"
    return mock_worker


class TestLeafNodeRunnerStart:
    """Tests for LeafNodeRunner.start() and connection management."""

    @pytest.mark.asyncio
    async def test_start_connects_local_and_remote(self) -> None:
        """start() should connect to local and remote NATS, start worker."""
        mock_local = _make_mock_nc()
        mock_remote = _make_mock_nc()
        mock_worker = _make_mock_worker()

        with patch(
            "ate_platform.scheduler.leaf_node_runner.nats.connect",
            new_callable=AsyncMock,
            side_effect=[mock_local, mock_remote],
        ):
            runner = LeafNodeRunner(
                local_url="nats://local:4222",
                remote_url="nats://remote:4222",
                worker=mock_worker,
            )
            await runner.start()

        # Worker started with local connection
        mock_worker.start.assert_awaited_once_with(nc=mock_local)
        # WAN connected (remote is connected)
        assert runner.is_wan_connected is True
        assert runner.buffer_size == 0

        await runner.stop()

    @pytest.mark.asyncio
    async def test_start_remote_unreachable_wan_down(self) -> None:
        """start() should succeed even if remote NATS is unreachable."""
        mock_local = _make_mock_nc()
        mock_worker = _make_mock_worker()

        with patch(
            "ate_platform.scheduler.leaf_node_runner.nats.connect",
            new_callable=AsyncMock,
            side_effect=[mock_local, ConnectionRefusedError("remote unreachable")],
        ):
            runner = LeafNodeRunner(
                local_url="nats://local:4222",
                remote_url="nats://remote:4222",
                worker=mock_worker,
            )
            await runner.start()

        # Worker still started (local connection works)
        mock_worker.start.assert_awaited_once()
        # WAN is down
        assert runner.is_wan_connected is False
        assert runner._remote_nc is None

        await runner.stop()

    @pytest.mark.asyncio
    async def test_start_passes_callbacks_to_remote_connect(self) -> None:
        """start() should pass reconnect callbacks to nats.connect for remote."""
        mock_local = _make_mock_nc()
        mock_remote = _make_mock_nc()
        mock_worker = _make_mock_worker()

        with patch(
            "ate_platform.scheduler.leaf_node_runner.nats.connect",
            new_callable=AsyncMock,
            side_effect=[mock_local, mock_remote],
        ) as mock_connect:
            runner = LeafNodeRunner(worker=mock_worker)
            await runner.start()

            # Second call (remote) should have reconnect callbacks
            remote_call_kwargs = mock_connect.call_args_list[1].kwargs
            assert "reconnected_cb" in remote_call_kwargs
            assert "disconnected_cb" in remote_call_kwargs
            assert "closed_cb" in remote_call_kwargs
            assert "error_cb" in remote_call_kwargs
            assert remote_call_kwargs["allow_reconnect"] is True
            assert remote_call_kwargs["max_reconnect_attempts"] == -1

        await runner.stop()


class TestLeafNodeRunnerWanStatus:
    """Tests for is_wan_connected property and WAN detection."""

    @pytest.mark.asyncio
    async def test_is_wan_connected_returns_cached_status(self) -> None:
        """is_wan_connected should return the cached WAN status."""
        mock_worker = _make_mock_worker()
        runner = LeafNodeRunner(worker=mock_worker)

        runner._wan_connected = True
        assert runner.is_wan_connected is True

        runner._wan_connected = False
        assert runner.is_wan_connected is False

    @pytest.mark.asyncio
    async def test_check_wan_publishes_to_remote_subject(self) -> None:
        """_check_wan should publish a ping to detect WAN status."""
        mock_remote = _make_mock_nc()
        mock_worker = _make_mock_worker()

        runner = LeafNodeRunner(worker=mock_worker)
        runner._remote_nc = mock_remote

        result = await runner._check_wan()

        assert result is True
        assert runner.is_wan_connected is True
        mock_remote.publish.assert_awaited_once()
        # Verify ping payload
        published_subject = mock_remote.publish.call_args.args[0]
        published_data = mock_remote.publish.call_args.args[1]
        assert published_subject == "ate.wan-check"
        assert published_data == b"ping"
        mock_remote.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_check_wan_returns_false_on_publish_error(self) -> None:
        """_check_wan should return False when publish fails."""
        mock_remote = _make_mock_nc()
        mock_remote.publish = AsyncMock(side_effect=ConnectionError("connection lost"))
        mock_worker = _make_mock_worker()

        runner = LeafNodeRunner(worker=mock_worker)
        runner._remote_nc = mock_remote
        runner._wan_connected = True

        result = await runner._check_wan()

        assert result is False
        assert runner.is_wan_connected is False

    @pytest.mark.asyncio
    async def test_check_wan_returns_false_when_not_connected(self) -> None:
        """_check_wan should return False when remote reports not connected."""
        mock_remote = _make_mock_nc(connected=False)
        mock_worker = _make_mock_worker()

        runner = LeafNodeRunner(worker=mock_worker)
        runner._remote_nc = mock_remote

        result = await runner._check_wan()

        assert result is False
        assert runner.is_wan_connected is False

    @pytest.mark.asyncio
    async def test_check_wan_returns_false_when_no_remote(self) -> None:
        """_check_wan should return False when remote_nc is None."""
        mock_worker = _make_mock_worker()
        runner = LeafNodeRunner(worker=mock_worker)

        result = await runner._check_wan()

        assert result is False
        assert runner.is_wan_connected is False

    @pytest.mark.asyncio
    async def test_disconnected_cb_sets_wan_down(self) -> None:
        """disconnected_cb should set wan_connected=False."""
        mock_worker = _make_mock_worker()
        runner = LeafNodeRunner(worker=mock_worker)
        runner._wan_connected = True

        await runner._on_remote_disconnected()

        assert runner.is_wan_connected is False

    @pytest.mark.asyncio
    async def test_closed_cb_sets_wan_down(self) -> None:
        """closed_cb should set wan_connected=False."""
        mock_worker = _make_mock_worker()
        runner = LeafNodeRunner(worker=mock_worker)
        runner._wan_connected = True

        await runner._on_remote_closed()

        assert runner.is_wan_connected is False


class TestLeafNodeRunnerPublishUpstream:
    """Tests for publish_upstream buffering logic."""

    @pytest.mark.asyncio
    async def test_publish_upstream_when_wan_up(self) -> None:
        """publish_upstream should publish directly when WAN is up."""
        mock_remote = _make_mock_nc()
        mock_worker = _make_mock_worker()

        runner = LeafNodeRunner(worker=mock_worker)
        runner._remote_nc = mock_remote
        runner._wan_connected = True

        result = await runner.publish_upstream("ate.status.exec-1", b'{"status":"pass"}')

        assert result is True
        mock_remote.publish.assert_awaited_once_with("ate.status.exec-1", b'{"status":"pass"}')
        assert runner.buffer_size == 0

    @pytest.mark.asyncio
    async def test_publish_upstream_when_wan_down(self) -> None:
        """publish_upstream should buffer when WAN is down."""
        mock_worker = _make_mock_worker()

        runner = LeafNodeRunner(worker=mock_worker)
        runner._wan_connected = False

        result = await runner.publish_upstream("ate.status.exec-1", b'{"status":"pass"}')

        assert result is False
        assert runner.buffer_size == 1
        assert runner._buffer[0].subject == "ate.status.exec-1"
        assert runner._buffer[0].data == b'{"status":"pass"}'

    @pytest.mark.asyncio
    async def test_publish_upstream_buffers_on_failure(self) -> None:
        """publish_upstream should buffer when remote publish fails."""
        mock_remote = _make_mock_nc()
        mock_remote.publish = AsyncMock(side_effect=ConnectionError("connection lost"))
        mock_worker = _make_mock_worker()

        runner = LeafNodeRunner(worker=mock_worker)
        runner._remote_nc = mock_remote
        runner._wan_connected = True

        result = await runner.publish_upstream("ate.status.exec-1", b"data")

        assert result is False
        assert runner.is_wan_connected is False
        assert runner.buffer_size == 1

    @pytest.mark.asyncio
    async def test_publish_upstream_multiple_messages_buffer(self) -> None:
        """Multiple publish_upstream calls should accumulate in buffer."""
        mock_worker = _make_mock_worker()
        runner = LeafNodeRunner(worker=mock_worker)
        runner._wan_connected = False

        await runner.publish_upstream("ate.status.1", b"data1")
        await runner.publish_upstream("ate.status.2", b"data2")
        await runner.publish_upstream("ate.status.3", b"data3")

        assert runner.buffer_size == 3
        assert runner._buffer[0].subject == "ate.status.1"
        assert runner._buffer[2].subject == "ate.status.3"


class TestLeafNodeRunnerSyncBacklog:
    """Tests for sync_backlog replay logic."""

    @pytest.mark.asyncio
    async def test_sync_backlog_replays_all_buffered(self) -> None:
        """sync_backlog should replay all buffered messages when WAN is up."""
        mock_remote = _make_mock_nc()
        mock_worker = _make_mock_worker()

        runner = LeafNodeRunner(worker=mock_worker)
        runner._remote_nc = mock_remote
        runner._wan_connected = True
        runner._buffer = [
            BufferedMessage(subject="ate.status.1", data=b"data1"),
            BufferedMessage(subject="ate.status.2", data=b"data2"),
            BufferedMessage(subject="ate.status.3", data=b"data3"),
        ]

        replayed = await runner.sync_backlog()

        assert replayed == 3
        assert runner.buffer_size == 0
        assert mock_remote.publish.call_count == 3

    @pytest.mark.asyncio
    async def test_sync_backlog_with_partial_failure(self) -> None:
        """sync_backlog should keep failed messages in buffer."""
        mock_remote = _make_mock_nc()
        mock_remote.publish = AsyncMock(
            side_effect=[None, ConnectionError("failed"), None],
        )
        mock_worker = _make_mock_worker()

        runner = LeafNodeRunner(worker=mock_worker)
        runner._remote_nc = mock_remote
        runner._wan_connected = True
        runner._buffer = [
            BufferedMessage(subject="ate.status.1", data=b"data1"),
            BufferedMessage(subject="ate.status.2", data=b"data2"),
            BufferedMessage(subject="ate.status.3", data=b"data3"),
        ]

        replayed = await runner.sync_backlog()

        assert replayed == 2
        assert runner.buffer_size == 1
        assert runner.is_wan_connected is False
        assert runner._buffer[0].subject == "ate.status.2"

    @pytest.mark.asyncio
    async def test_sync_backlog_skipped_when_wan_down(self) -> None:
        """sync_backlog should do nothing when WAN is not connected."""
        mock_worker = _make_mock_worker()
        runner = LeafNodeRunner(worker=mock_worker)
        runner._wan_connected = False
        runner._buffer = [BufferedMessage(subject="ate.status.1", data=b"data1")]

        replayed = await runner.sync_backlog()

        assert replayed == 0
        assert runner.buffer_size == 1

    @pytest.mark.asyncio
    async def test_sync_backlog_skipped_when_empty(self) -> None:
        """sync_backlog should return 0 when buffer is empty."""
        mock_worker = _make_mock_worker()
        runner = LeafNodeRunner(worker=mock_worker)
        runner._wan_connected = True

        replayed = await runner.sync_backlog()

        assert replayed == 0

    @pytest.mark.asyncio
    async def test_sync_backlog_skipped_when_no_remote(self) -> None:
        """sync_backlog should return 0 when remote_nc is None."""
        mock_worker = _make_mock_worker()
        runner = LeafNodeRunner(worker=mock_worker)
        runner._wan_connected = True
        runner._remote_nc = None
        runner._buffer = [BufferedMessage(subject="ate.status.1", data=b"data1")]

        replayed = await runner.sync_backlog()

        assert replayed == 0
        assert runner.buffer_size == 1

    @pytest.mark.asyncio
    async def test_sync_backlog_preserves_new_arrivals(self) -> None:
        """sync_backlog should preserve messages added during replay.

        Messages that arrive via publish_upstream while sync_backlog is
        running should not be lost — they remain in the buffer after sync.
        """
        mock_remote = _make_mock_nc()
        mock_worker = _make_mock_worker()

        runner = LeafNodeRunner(worker=mock_worker)
        runner._remote_nc = mock_remote
        runner._wan_connected = True
        runner._buffer = [BufferedMessage(subject="ate.status.1", data=b"data1")]

        # Make publish slow so we can inject a new message mid-sync
        async def slow_publish(subject: str, data: bytes) -> None:
            if subject == "ate.status.1":
                # Simulate a new message arriving during sync
                runner._buffer.append(
                    BufferedMessage(subject="ate.status.new", data=b"new")
                )

        mock_remote.publish = slow_publish

        replayed = await runner.sync_backlog()

        assert replayed == 1
        # The new message should still be in the buffer
        assert runner.buffer_size == 1
        assert runner._buffer[0].subject == "ate.status.new"

    @pytest.mark.asyncio
    async def test_sync_backlog_prevents_reentrancy(self) -> None:
        """sync_backlog should not run concurrently with itself."""
        mock_remote = _make_mock_nc()
        mock_worker = _make_mock_worker()

        runner = LeafNodeRunner(worker=mock_worker)
        runner._remote_nc = mock_remote
        runner._wan_connected = True
        runner._buffer = [BufferedMessage(subject="ate.status.1", data=b"data1")]

        # Manually set _syncing to simulate concurrent call
        runner._syncing = True
        replayed = await runner.sync_backlog()
        assert replayed == 0
        assert runner.buffer_size == 1

        # Now run normally
        runner._syncing = False
        replayed = await runner.sync_backlog()
        assert replayed == 1
        assert runner.buffer_size == 0


class TestLeafNodeRunnerReconnect:
    """Tests for reconnected_cb triggering sync_backlog."""

    @pytest.mark.asyncio
    async def test_reconnect_cb_triggers_sync_backlog(self) -> None:
        """reconnected_cb should set wan_connected=True and call sync_backlog."""
        mock_remote = _make_mock_nc()
        mock_worker = _make_mock_worker()

        runner = LeafNodeRunner(worker=mock_worker)
        runner._remote_nc = mock_remote
        runner._wan_connected = False
        runner._buffer = [BufferedMessage(subject="ate.status.1", data=b"data1")]

        # Simulate reconnect callback
        await runner._on_remote_reconnect()

        assert runner.is_wan_connected is True
        assert runner.buffer_size == 0
        mock_remote.publish.assert_awaited_once_with("ate.status.1", b"data1")

    @pytest.mark.asyncio
    async def test_reconnect_cb_with_empty_buffer(self) -> None:
        """reconnected_cb should set wan_connected=True even with empty buffer."""
        mock_remote = _make_mock_nc()
        mock_worker = _make_mock_worker()

        runner = LeafNodeRunner(worker=mock_worker)
        runner._remote_nc = mock_remote
        runner._wan_connected = False

        await runner._on_remote_reconnect()

        assert runner.is_wan_connected is True
        assert runner.buffer_size == 0


class TestLeafNodeRunnerWanDownLocalOps:
    """Tests verifying local operations continue when WAN is down."""

    @pytest.mark.asyncio
    async def test_wan_down_worker_still_processes(self) -> None:
        """When WAN is down, worker should still process local tasks."""
        mock_local = _make_mock_nc()
        mock_worker = _make_mock_worker()
        mock_worker.pull_and_process_one = AsyncMock(return_value=True)

        with patch(
            "ate_platform.scheduler.leaf_node_runner.nats.connect",
            new_callable=AsyncMock,
            side_effect=[mock_local, ConnectionRefusedError("remote unreachable")],
        ):
            runner = LeafNodeRunner(worker=mock_worker)
            await runner.start()

        # WAN is down
        assert runner.is_wan_connected is False

        # But worker can still process tasks
        result = await runner.worker.pull_and_process_one(timeout=1.0)
        assert result is True
        mock_worker.pull_and_process_one.assert_awaited_once()

        # And messages can be buffered
        await runner.publish_upstream("ate.status.exec-1", b"status-data")
        assert runner.buffer_size == 1

        await runner.stop()

    @pytest.mark.asyncio
    async def test_full_cycle_wan_down_buffer_reconnect_replay(self) -> None:
        """Full cycle: WAN down → buffer → WAN up → sync_backlog replays."""
        mock_local = _make_mock_nc()
        mock_remote = _make_mock_nc()
        mock_worker = _make_mock_worker()

        with patch(
            "ate_platform.scheduler.leaf_node_runner.nats.connect",
            new_callable=AsyncMock,
            side_effect=[mock_local, mock_remote],
        ):
            runner = LeafNodeRunner(worker=mock_worker)
            await runner.start()

        # Simulate WAN disconnect
        await runner._on_remote_disconnected()
        assert runner.is_wan_connected is False

        # Buffer messages while WAN is down
        await runner.publish_upstream("ate.status.1", b"data1")
        await runner.publish_upstream("ate.status.2", b"data2")
        assert runner.buffer_size == 2

        # Simulate WAN reconnect — should trigger sync_backlog
        await runner._on_remote_reconnect()
        assert runner.is_wan_connected is True
        assert runner.buffer_size == 0

        # Verify both buffered messages were replayed
        publish_calls = mock_remote.publish.call_args_list
        replayed_subjects = [
            call.args[0] for call in publish_calls if call.args[0] != "ate.wan-check"
        ]
        assert "ate.status.1" in replayed_subjects
        assert "ate.status.2" in replayed_subjects

        await runner.stop()

    @pytest.mark.asyncio
    async def test_wan_down_then_up_no_buffer(self) -> None:
        """WAN reconnect with empty buffer should not error."""
        mock_local = _make_mock_nc()
        mock_remote = _make_mock_nc()
        mock_worker = _make_mock_worker()

        with patch(
            "ate_platform.scheduler.leaf_node_runner.nats.connect",
            new_callable=AsyncMock,
            side_effect=[mock_local, mock_remote],
        ):
            runner = LeafNodeRunner(worker=mock_worker)
            await runner.start()

        await runner._on_remote_disconnected()
        assert runner.is_wan_connected is False

        # No messages buffered

        await runner._on_remote_reconnect()
        assert runner.is_wan_connected is True
        assert runner.buffer_size == 0

        await runner.stop()


class TestLeafNodeRunnerStop:
    """Tests for stop() cleanup."""

    @pytest.mark.asyncio
    async def test_stop_cleans_up_connections(self) -> None:
        """stop() should stop worker and close remote connection."""
        mock_local = _make_mock_nc()
        mock_remote = _make_mock_nc()
        mock_worker = _make_mock_worker()

        with patch(
            "ate_platform.scheduler.leaf_node_runner.nats.connect",
            new_callable=AsyncMock,
            side_effect=[mock_local, mock_remote],
        ):
            runner = LeafNodeRunner(worker=mock_worker)
            await runner.start()

        await runner.stop()

        mock_worker.stop.assert_awaited_once()
        mock_remote.drain.assert_awaited_once()
        mock_remote.close.assert_awaited_once()
        assert runner.is_wan_connected is False
        assert runner._remote_nc is None
        assert runner._wan_check_task is None

    @pytest.mark.asyncio
    async def test_stop_with_no_remote_connection(self) -> None:
        """stop() should handle case where remote was never established."""
        mock_local = _make_mock_nc()
        mock_worker = _make_mock_worker()

        with patch(
            "ate_platform.scheduler.leaf_node_runner.nats.connect",
            new_callable=AsyncMock,
            side_effect=[mock_local, ConnectionRefusedError("unreachable")],
        ):
            runner = LeafNodeRunner(worker=mock_worker)
            await runner.start()

        await runner.stop()

        mock_worker.stop.assert_awaited_once()
        assert runner._remote_nc is None

    @pytest.mark.asyncio
    async def test_stop_cancels_wan_monitor_task(self) -> None:
        """stop() should cancel the background WAN monitor task."""
        mock_local = _make_mock_nc()
        mock_remote = _make_mock_nc()
        mock_worker = _make_mock_worker()

        with patch(
            "ate_platform.scheduler.leaf_node_runner.nats.connect",
            new_callable=AsyncMock,
            side_effect=[mock_local, mock_remote],
        ):
            runner = LeafNodeRunner(worker=mock_worker)
            await runner.start()

        assert runner._wan_check_task is not None
        assert not runner._wan_check_task.done()

        await runner.stop()

        assert runner._wan_check_task is None
        assert runner._running is False

    @pytest.mark.asyncio
    async def test_stop_handles_remote_drain_error(self) -> None:
        """stop() should not raise if remote drain/close fails."""
        mock_local = _make_mock_nc()
        mock_remote = _make_mock_nc()
        mock_remote.drain = AsyncMock(side_effect=Exception("drain failed"))
        mock_worker = _make_mock_worker()

        with patch(
            "ate_platform.scheduler.leaf_node_runner.nats.connect",
            new_callable=AsyncMock,
            side_effect=[mock_local, mock_remote],
        ):
            runner = LeafNodeRunner(worker=mock_worker)
            await runner.start()

        # Should not raise
        await runner.stop()

        mock_worker.stop.assert_awaited_once()


class TestBufferedMessage:
    """Tests for BufferedMessage dataclass."""

    def test_buffered_message_fields(self) -> None:
        """BufferedMessage should store subject and data."""
        msg = BufferedMessage(subject="ate.status.1", data=b'{"status":"pass"}')
        assert msg.subject == "ate.status.1"
        assert msg.data == b'{"status":"pass"}'

    def test_buffered_message_with_empty_data(self) -> None:
        """BufferedMessage should handle empty data."""
        msg = BufferedMessage(subject="ate.control.abort", data=b"")
        assert msg.subject == "ate.control.abort"
        assert msg.data == b""
