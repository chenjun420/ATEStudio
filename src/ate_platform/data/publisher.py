"""NATS JetStream Publisher for ATE results."""

import asyncio
import logging
from typing import TYPE_CHECKING

import nats
from nats.errors import ConnectionClosedError, NoServersError, TimeoutError

if TYPE_CHECKING:
    from nats import JetStreamContext

logger = logging.getLogger(__name__)


class NATSPublisher:
    """NATS JetStream publisher with automatic reconnection support.

    Features:
        - Automatic reconnection with exponential backoff
        - JetStream for durable messaging
        - Graceful error handling
        - Support for disconnected mode

    Example:
        >>> publisher = NATSPublisher(["nats://localhost:4222"])
        >>> await publisher.connect()
        >>> await publisher.publish("ate.results.test1", b'{"status": "pass"}')
        >>> await publisher.close()
    """

    def __init__(
        self,
        servers: list[str],
        stream_name: str = "ate_results",
        reconnect_backoff: list[float] | None = None,
        max_reconnect_attempts: int = 10,
    ) -> None:
        """Initialize NATS publisher.

        Args:
            servers: List of NATS server URLs
            stream_name: Name of the JetStream stream
            reconnect_backoff: List of backoff times in seconds (default: [1, 2, 5, 10, 30])
            max_reconnect_attempts: Maximum number of reconnection attempts
        """
        self._servers: list[str] = servers
        self._stream_name: str = stream_name
        self._reconnect_backoff: list[float] = reconnect_backoff or [1, 2, 5, 10, 30]
        self._max_reconnect_attempts: int = max_reconnect_attempts
        self._nc: nats.NATS | None = None
        self._js: JetStreamContext | None = None
        self._reconnect_attempt: int = 0
        self._is_reconnecting: bool = False

    @property
    def is_connected(self) -> bool:
        """Check if connected to NATS server."""
        return self._nc is not None and self._nc.is_connected

    async def connect(self) -> None:
        """Connect to NATS server with automatic reconnection support.

        Raises:
            NoServersError: If no servers are available after all retries
            TimeoutError: If connection times out
        """
        try:
            # Connect with reconnection enabled
            self._nc = await nats.connect(
                servers=self._servers,
                allow_reconnect=True,
                max_reconnect_attempts=self._max_reconnect_attempts,
                reconnect_time_wait=int(self._reconnect_backoff[0]),
                error_cb=self._error_callback,
                disconnected_cb=self._disconnected_callback,
                reconnected_cb=self._reconnected_callback,
                closed_cb=self._closed_callback,
            )

            # Get JetStream context
            self._js = self._nc.jetstream()

            # Create stream if it doesn't exist
            await self.create_stream()

            logger.info(f"Connected to NATS server(s): {self._servers}")

        except (NoServersError, TimeoutError, OSError) as e:
            logger.error(f"Failed to connect to NATS: {e}")
            # Allow operation in disconnected mode
            self._nc = None
            self._js = None
            raise

    async def close(self) -> None:
        """Close the NATS connection gracefully."""
        if self._nc is not None:
            try:
                await self._nc.drain()
                await self._nc.close()
                logger.info("NATS connection closed")
            except Exception as e:
                logger.warning(f"Error during NATS close: {e}")
            finally:
                self._nc = None
                self._js = None

    async def publish(
        self,
        subject: str,
        payload: bytes,
        headers: dict[str, str] | None = None,
    ) -> bool:
        """Publish a message to JetStream with acknowledgment.

        Args:
            subject: Subject to publish to (e.g., "ate.results.test1")
            payload: Message payload as bytes
            headers: Optional message headers

        Returns:
            True if published successfully, False otherwise

        Raises:
            ConnectionClosedError: If not connected and reconnection fails
        """
        # If not connected, try to reconnect
        if not self.is_connected:
            if not await self._try_reconnect():
                logger.error("Cannot publish: not connected to NATS")
                return False

        try:
            if self._js is None:
                logger.error("JetStream context not available")
                return False

            # Publish to JetStream and wait for acknowledgment
            ack = await self._js.publish(subject, payload, headers=headers)
            logger.debug(f"Published to {subject}, seq={ack.seq}")
            return True

        except (ConnectionClosedError, NoServersError, TimeoutError, OSError) as e:
            logger.error(f"Failed to publish to {subject}: {e}")
            # Mark for reconnection
            self._reconnect_attempt = 0
            return False

    async def create_stream(self) -> None:
        """Create the JetStream stream if it doesn't exist.

        The stream is configured for:
            - All subjects matching "ate.>"
            - File storage for persistence
            - 7-day retention
        """
        if self._js is None:
            logger.warning("Cannot create stream: JetStream not available")
            return

        try:
            # Check if stream exists
            try:
                await self._js.stream_info(self._stream_name)
                logger.debug(f"Stream '{self._stream_name}' already exists")
            except Exception:
                # Stream doesn't exist, create it
                await self._js.add_stream(
                    name=self._stream_name,
                    subjects=["ate.>"],
                    config={"retention": "limits", "max_age": 7 * 24 * 3600},  # 7 days
                )
                logger.info(f"Created JetStream stream '{self._stream_name}'")
        except Exception as e:
            logger.error(f"Failed to create stream: {e}")

    async def _try_reconnect(self) -> bool:
        """Attempt to reconnect using exponential backoff.

        Returns:
            True if reconnected successfully, False otherwise
        """
        if self._is_reconnecting:
            return False

        self._is_reconnecting = True
        try:
            for backoff in self._reconnect_backoff:
                self._reconnect_attempt += 1
                logger.info(
                    f"Attempting reconnection ({self._reconnect_attempt}), "
                    f"waiting {backoff}s..."
                )
                await asyncio.sleep(backoff)

                try:
                    await self.connect()
                    self._reconnect_attempt = 0
                    return True
                except Exception as e:
                    logger.warning(f"Reconnection attempt failed: {e}")

            logger.error("All reconnection attempts failed")
            return False

        finally:
            self._is_reconnecting = False

    async def _error_callback(self, e: Exception) -> None:
        """Handle NATS errors."""
        logger.error(f"NATS error: {e}")

    async def _disconnected_callback(self) -> None:
        """Handle NATS disconnection."""
        logger.warning("Disconnected from NATS server")

    async def _reconnected_callback(self) -> None:
        """Handle NATS reconnection."""
        logger.info("Reconnected to NATS server")
        # Re-create JetStream context after reconnection
        if self._nc is not None:
            self._js = self._nc.jetstream()

    async def _closed_callback(self) -> None:
        """Handle NATS connection closed."""
        logger.info("NATS connection closed")

    async def __aenter__(self) -> "NATSPublisher":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Async context manager exit."""
        await self.close()
