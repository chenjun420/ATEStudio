"""NATS subscriber integration tests using mocks.

Tests message handling, graceful degradation, and error scenarios.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from ate_cloud.nats.subscriber import NATSSubscriber


class MockNatsMessage:
    """Mock NATS message for testing."""

    def __init__(self, subject: str, data: dict):
        self.subject = subject
        self.data = json.dumps(data).encode()
        self._ack_called = False
        self._nak_called = False

    async def ack(self):
        self._ack_called = True

    async def nak(self):
        self._nak_called = True

    @property
    def acked(self):
        return self._ack_called

    @property
    def nacked(self):
        return self._nak_called


class MockJetStream:
    """Mock JetStream context for testing."""

    def __init__(self):
        self.add_stream = AsyncMock()
        self.pull_subscribe = AsyncMock()


class MockPullSubscription:
    """Mock pull subscription for testing."""

    def __init__(self):
        self.fetch = AsyncMock(side_effect=asyncio.TimeoutError)
        self.unsubscribe = AsyncMock()


class TestNATSSubscriberLifecycle:
    """Tests for subscriber start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_subscriber_start_stop(self):
        """Test starting and stopping the subscriber."""
        # Create mock NATS client
        mock_nc = MagicMock()
        mock_js = MockJetStream()
        mock_psub = MockPullSubscription()

        # Setup mocks
        mock_nc.jetstream = MagicMock(return_value=mock_js)
        mock_js.pull_subscribe.return_value = mock_psub

        # Create subscriber
        subscriber = NATSSubscriber(mock_nc)

        # Start subscriber
        await subscriber.start()
        assert subscriber._running is True
        assert subscriber._task is not None

        # Stop subscriber
        await subscriber.stop()
        assert subscriber._running is False
        assert subscriber._task is None

    @pytest.mark.asyncio
    async def test_subscriber_already_running_warning(self):
        """Test that starting an already running subscriber logs warning."""
        mock_nc = MagicMock()
        mock_js = MockJetStream()
        mock_psub = MockPullSubscription()

        mock_nc.jetstream = MagicMock(return_value=mock_js)
        mock_js.pull_subscribe.return_value = mock_psub

        subscriber = NATSSubscriber(mock_nc)

        await subscriber.start()
        assert subscriber._running is True

        # Start again - should not create new task
        original_task = subscriber._task
        await subscriber.start()
        assert subscriber._task is original_task

        await subscriber.stop()


class TestNATSMessageHandling:
    """Tests for message handling logic."""

    @pytest.mark.asyncio
    async def test_handle_message_success(self):
        """Test successful message handling."""
        mock_nc = MagicMock()
        subscriber = NATSSubscriber(mock_nc)

        # Create valid message
        msg = MockNatsMessage(
            subject="ate.results.test",
            data={"step_id": "step-123", "status": "passed"}
        )

        # Handle message directly
        await subscriber._handle_message(msg)  # type: ignore

        # Should ACK on success
        assert msg.acked is True
        assert msg.nacked is False

    @pytest.mark.asyncio
    async def test_handle_message_invalid_json(self):
        """Test handling message with invalid JSON."""
        mock_nc = MagicMock()
        subscriber = NATSSubscriber(mock_nc)

        # Create message with invalid JSON
        msg = MockNatsMessage(
            subject="ate.results.test",
            data={"step_id": "step-123", "status": "passed"}
        )
        # Corrupt the data
        msg.data = b"not valid json"

        # Handle message directly
        await subscriber._handle_message(msg)  # type: ignore

        # Should NAK on parse error
        assert msg.nacked is True
        assert msg.acked is False

    @pytest.mark.asyncio
    async def test_handle_message_unknown_subject(self):
        """Test handling message with unknown subject."""
        mock_nc = MagicMock()
        subscriber = NATSSubscriber(mock_nc)

        # Create message with unknown subject
        msg = MockNatsMessage(
            subject="unknown.subject",
            data={"data": "test"}
        )

        # Handle message - should still ACK (just logs debug)
        await subscriber._handle_message(msg)  # type: ignore

        assert msg.acked is True


class TestNATSGracefulDegradation:
    """Tests for graceful degradation when NATS is unavailable."""

    @pytest.mark.asyncio
    async def test_nats_unavailable_graceful_degradation(self):
        """Test that subscriber handles NATS unavailability gracefully."""
        mock_nc = MagicMock()

        # Simulate NATS connection failure
        mock_nc.jetstream = MagicMock(side_effect=Exception("NATS connection failed"))

        subscriber = NATSSubscriber(mock_nc)

        # Should not raise, just log warning and set running=False
        await subscriber.start()

        assert subscriber._running is False
        assert subscriber._task is None

    @pytest.mark.asyncio
    async def test_stream_already_exists(self):
        """Test handling when stream already exists."""
        mock_nc = MagicMock()
        mock_js = MockJetStream()
        mock_psub = MockPullSubscription()

        # First call raises "stream name already in use"
        mock_js.add_stream.side_effect = Exception("stream name already in use")
        mock_nc.jetstream = MagicMock(return_value=mock_js)
        mock_js.pull_subscribe.return_value = mock_psub

        subscriber = NATSSubscriber(mock_nc)

        # Should handle gracefully and continue
        await subscriber.start()
        assert subscriber._running is True

        await subscriber.stop()

    @pytest.mark.asyncio
    async def test_consume_loop_timeout_continues(self):
        """Test that consume loop continues on timeout."""
        mock_nc = MagicMock()
        mock_js = MockJetStream()

        # Create mock that times out
        mock_psub = MockPullSubscription()

        timeout_count = 0

        async def timeout_fetch(*args, **kwargs):
            nonlocal timeout_count
            timeout_count += 1
            if timeout_count < 3:
                raise asyncio.TimeoutError()
            else:
                # After a few timeouts, raise CancelledError to stop
                raise asyncio.CancelledError()

        mock_psub.fetch = timeout_fetch
        mock_nc.jetstream = MagicMock(return_value=mock_js)
        mock_js.pull_subscribe.return_value = mock_psub

        subscriber = NATSSubscriber(mock_nc)
        await subscriber.start()

        # Let the loop run for a bit
        await asyncio.sleep(0.1)

        # Should have had multiple timeouts
        assert timeout_count >= 2

        await subscriber.stop()


class TestNATSResultHandling:
    """Tests for result message handling."""

    @pytest.mark.asyncio
    async def test_handle_result_message(self):
        """Test handling result messages."""
        mock_nc = MagicMock()
        subscriber = NATSSubscriber(mock_nc)

        # Handle result message
        data = {
            "step_id": "step-456",
            "status": "failed",
            "error": "Timeout waiting for response"
        }

        # Should not raise
        await subscriber._handle_result("ate.results.device-001", data)