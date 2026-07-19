"""Unit tests for NATSPublisher."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import nats
from nats.errors import ConnectionClosedError, NoServersError, TimeoutError

from ate_platform.data.publisher import NATSPublisher


class TestNATSPublisher:
    """Tests for NATSPublisher class."""

    def test_init_default_values(self) -> None:
        """Test publisher initialization with default values."""
        publisher = NATSPublisher(["nats://localhost:4222"])
        
        assert publisher._servers == ["nats://localhost:4222"]
        assert publisher._stream_name == "ate_results"
        assert publisher._reconnect_backoff == [1, 2, 5, 10, 30]
        assert publisher._nc is None
        assert publisher._js is None

    def test_init_custom_values(self) -> None:
        """Test publisher initialization with custom values."""
        publisher = NATSPublisher(
            servers=["nats://server1:4222", "nats://server2:4222"],
            stream_name="custom_stream",
            reconnect_backoff=[0.5, 1.0, 2.0],
            max_reconnect_attempts=5,
        )
        
        assert publisher._servers == ["nats://server1:4222", "nats://server2:4222"]
        assert publisher._stream_name == "custom_stream"
        assert publisher._reconnect_backoff == [0.5, 1.0, 2.0]
        assert publisher._max_reconnect_attempts == 5

    def test_is_connected_false_initially(self) -> None:
        """Test is_connected returns False before connection."""
        publisher = NATSPublisher(["nats://localhost:4222"])
        assert publisher.is_connected is False

    @pytest.mark.asyncio
    async def test_connect_success(self) -> None:
        """Test successful connection to NATS."""
        publisher = NATSPublisher(["nats://localhost:4222"])
        
        # Mock NATS connection
        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_nc.jetstream = MagicMock()
        mock_js = MagicMock()
        mock_nc.jetstream.return_value = mock_js
        mock_js.stream_info = AsyncMock(side_effect=Exception("Stream not found"))
        mock_js.add_stream = AsyncMock()
        
        with patch("ate_platform.data.publisher.nats.connect", AsyncMock(return_value=mock_nc)):
            await publisher.connect()
        
        assert publisher._nc is not None
        assert publisher._js is not None
        mock_js.add_stream.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_failure(self) -> None:
        """Test connection failure handling."""
        publisher = NATSPublisher(["nats://localhost:4222"])
        
        with patch(
            "ate_platform.data.publisher.nats.connect",
            AsyncMock(side_effect=NoServersError("No servers available")),
        ):
            with pytest.raises(NoServersError):
                await publisher.connect()
        
        assert publisher._nc is None
        assert publisher._js is None

    @pytest.mark.asyncio
    async def test_close(self) -> None:
        """Test closing the connection."""
        publisher = NATSPublisher(["nats://localhost:4222"])
        
        # Mock connection
        mock_nc = MagicMock()
        mock_nc.drain = AsyncMock()
        mock_nc.close = AsyncMock()
        publisher._nc = mock_nc
        
        await publisher.close()
        
        mock_nc.drain.assert_called_once()
        mock_nc.close.assert_called_once()
        assert publisher._nc is None
        assert publisher._js is None

    @pytest.mark.asyncio
    async def test_close_no_connection(self) -> None:
        """Test closing when not connected."""
        publisher = NATSPublisher(["nats://localhost:4222"])
        
        # Should not raise
        await publisher.close()
        assert publisher._nc is None

    @pytest.mark.asyncio
    async def test_publish_success(self) -> None:
        """Test successful message publishing."""
        publisher = NATSPublisher(["nats://localhost:4222"])
        
        # Mock connection
        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_js = MagicMock()
        mock_ack = MagicMock(seq=123)
        mock_js.publish = AsyncMock(return_value=mock_ack)
        publisher._nc = mock_nc
        publisher._js = mock_js
        
        result = await publisher.publish("ate.results.test1", b'{"status": "pass"}')
        
        assert result is True
        mock_js.publish.assert_called_once_with(
            "ate.results.test1", b'{"status": "pass"}', headers=None
        )

    @pytest.mark.asyncio
    async def test_publish_with_headers(self) -> None:
        """Test message publishing with headers."""
        publisher = NATSPublisher(["nats://localhost:4222"])
        
        # Mock connection
        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_js = MagicMock()
        mock_ack = MagicMock(seq=123)
        mock_js.publish = AsyncMock(return_value=mock_ack)
        publisher._nc = mock_nc
        publisher._js = mock_js
        
        headers = {"content-type": "application/json"}
        result = await publisher.publish(
            "ate.results.test1", b'{"status": "pass"}', headers=headers
        )
        
        assert result is True
        mock_js.publish.assert_called_once_with(
            "ate.results.test1", b'{"status": "pass"}', headers=headers
        )

    @pytest.mark.asyncio
    async def test_publish_not_connected(self) -> None:
        """Test publishing when not connected."""
        publisher = NATSPublisher(["nats://localhost:4222"])
        
        # Mock reconnection failure
        publisher._try_reconnect = AsyncMock(return_value=False)  # type: ignore
        
        result = await publisher.publish("ate.results.test1", b'{"status": "pass"}')
        
        assert result is False

    @pytest.mark.asyncio
    async def test_publish_connection_error(self) -> None:
        """Test publishing with connection error."""
        publisher = NATSPublisher(["nats://localhost:4222"])
        
        # Mock connection
        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_js = MagicMock()
        mock_js.publish = AsyncMock(side_effect=ConnectionClosedError("Connection closed"))
        publisher._nc = mock_nc
        publisher._js = mock_js
        
        result = await publisher.publish("ate.results.test1", b'{"status": "pass"}')
        
        assert result is False

    @pytest.mark.asyncio
    async def test_create_stream_existing(self) -> None:
        """Test creating stream when it already exists."""
        publisher = NATSPublisher(["nats://localhost:4222"])
        
        # Mock JetStream
        mock_js = MagicMock()
        mock_js.stream_info = AsyncMock(return_value=MagicMock())
        publisher._js = mock_js
        
        await publisher.create_stream()
        
        mock_js.stream_info.assert_called_once_with("ate_results")

    @pytest.mark.asyncio
    async def test_create_stream_new(self) -> None:
        """Test creating a new stream."""
        publisher = NATSPublisher(["nats://localhost:4222"])
        
        # Mock JetStream
        mock_js = MagicMock()
        mock_js.stream_info = AsyncMock(side_effect=Exception("Stream not found"))
        mock_js.add_stream = AsyncMock()
        publisher._js = mock_js
        
        await publisher.create_stream()
        
        mock_js.add_stream.assert_called_once()
        call_args = mock_js.add_stream.call_args
        assert call_args[1]["name"] == "ate_results"
        assert call_args[1]["subjects"] == ["ate.>"]

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        """Test async context manager usage."""
        publisher = NATSPublisher(["nats://localhost:4222"])
        
        # Mock NATS connection
        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_nc.jetstream = MagicMock()
        mock_js = MagicMock()
        mock_nc.jetstream.return_value = mock_js
        mock_js.stream_info = AsyncMock(side_effect=Exception("Stream not found"))
        mock_js.add_stream = AsyncMock()
        mock_nc.drain = AsyncMock()
        mock_nc.close = AsyncMock()
        
        with patch("ate_platform.data.publisher.nats.connect", AsyncMock(return_value=mock_nc)):
            async with publisher as pub:
                assert pub._nc is not None
        
        mock_nc.drain.assert_called_once()
        mock_nc.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconnect_backoff_sequence(self) -> None:
        """Test that reconnection follows backoff sequence."""
        publisher = NATSPublisher(
            ["nats://localhost:4222"],
            reconnect_backoff=[0.1, 0.2, 0.3],
        )
        
        # Mock connection to fail twice then succeed
        call_count = 0

        async def mock_connect(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise NoServersError("No servers available")
            
            mock_nc = MagicMock()
            mock_nc.is_connected = True
            mock_nc.jetstream = MagicMock()
            mock_js = MagicMock()
            mock_nc.jetstream.return_value = mock_js
            mock_js.stream_info = AsyncMock(side_effect=Exception())
            mock_js.add_stream = AsyncMock()
            return mock_nc

        with patch("ate_platform.data.publisher.nats.connect", mock_connect):
            result = await publisher._try_reconnect()
        
        # Should succeed after 2 failures (calls on 3rd attempt)
        assert result is True
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_reconnect_max_attempts(self) -> None:
        """Test reconnection stops after max attempts."""
        publisher = NATSPublisher(
            ["nats://localhost:4222"],
            reconnect_backoff=[0.1, 0.2],
            max_reconnect_attempts=2,
        )
        
        # Mock connection to always fail
        with patch(
            "ate_platform.data.publisher.nats.connect",
            AsyncMock(side_effect=NoServersError("No servers available")),
        ):
            result = await publisher._try_reconnect()
        
        # Should fail after exhausting backoff sequence
        assert result is False

    @pytest.mark.asyncio
    async def test_publish_in_disconnected_mode(self) -> None:
        """Test that publisher handles disconnected mode gracefully."""
        publisher = NATSPublisher(["nats://localhost:4222"])
        
        # Mock reconnection to fail immediately
        publisher._try_reconnect = AsyncMock(return_value=False)  # type: ignore
        
        # No connection established
        assert publisher.is_connected is False
        
        # Attempt to publish should return False
        result = await publisher.publish("ate.results.test1", b'{"data": "test"}')
        assert result is False