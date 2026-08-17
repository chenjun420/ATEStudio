"""NATS subscriber module for consuming messages from JetStream."""

import asyncio
import json
import logging
from typing import Any

import nats
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import StreamConfig

logger = logging.getLogger(__name__)


class NATSSubscriber:
    """NATS JetStream subscriber using pull consumer pattern.

    Subscribes to messages from JetStream and handles them asynchronously.
    Gracefully degrades when NATS is unavailable.
    """

    def __init__(
        self,
        nc: NatsClient,
        stream: str = "ate_results",
        subject: str = "ate.>",
    ) -> None:
        """Initialize the subscriber.

        Args:
            nc: Connected NATS client
            stream: JetStream stream name
            subject: Subject pattern to subscribe to
        """
        self._nc = nc
        self._stream = stream
        self._subject = subject
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._psub: JetStreamContext.PullSubscription | None = None

    async def start(self) -> None:
        """Start the background subscription task.

        Uses pull consumer mode. Creates stream if it doesn't exist.
        Logs warning if NATS is unavailable but does not crash.
        """
        if self._running:
            logger.warning("Subscriber is already running")
            return

        try:
            js: JetStreamContext = self._nc.jetstream()

            # Create stream if it doesn't exist
            try:
                config = StreamConfig(
                    name=self._stream,
                    subjects=[self._subject],
                )
                await js.add_stream(config)
                logger.info(f"Created stream '{self._stream}'")
            except Exception as e:
                # Stream already exists or other error
                if "stream name already in use" in str(e).lower():
                    logger.debug(f"Stream '{self._stream}' already exists")
                else:
                    raise

            # Create durable pull subscription
            self._psub = await js.pull_subscribe(self._subject, durable="ate_consumer")

            self._running = True
            self._task = asyncio.create_task(self._consume_loop())
            logger.info(f"Subscriber started for subject '{self._subject}'")

        except Exception as e:
            logger.warning(f"Failed to start subscriber: {e}")
            self._running = False

    async def stop(self) -> None:
        """Stop the subscription and cancel background task."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None
        if self._psub is not None:
            try:
                await self._psub.unsubscribe()
            except Exception:
                pass
            finally:
                self._psub = None
        logger.info("Subscriber stopped")

    async def _consume_loop(self) -> None:
        """Consumer loop that pulls messages from JetStream."""
        while self._running and self._psub is not None:
            try:
                # Pull messages in batches of 1
                msgs = await self._psub.fetch(batch=1, timeout=1.0)
                for msg in msgs:
                    await self._handle_message(msg)
            except asyncio.TimeoutError:
                # No messages available, continue polling
                continue
            except asyncio.CancelledError:
                logger.debug("Consume loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in consume loop: {e}")
                await asyncio.sleep(1.0)  # Backoff on error

    async def _handle_message(self, msg: Msg) -> None:
        """Handle incoming message.

        解析消息体 JSON
        根据 subject 路由消息
        记录日志
        成功 ACK，失败 NAK

        Args:
            msg: The NATS message to handle
        """
        try:
            data = json.loads(msg.data.decode())

            # 根据 subject 路由
            if msg.subject.startswith("ate.results."):
                await self._handle_result(msg.subject, data)
            else:
                logger.debug(f"Unknown subject: {msg.subject}")

            await msg.ack()
            logger.info(f"Processed message on {msg.subject}")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {e}")
            await msg.nak()  # NAK on parse error
        except Exception as e:
            logger.error(f"Error handling message: {e}")
            await msg.nak()

    async def _handle_result(self, subject: str, data: dict[str, Any]) -> None:
        """Handle result message from edge.

        Args:
            subject: NATS subject
            data: Parsed message data
        """
        # 解析 StepStatus
        status = data.get("status")
        step_id = data.get("step_id")

        logger.info(f"Result received: step_id={step_id}, status={status}")

        # TODO: 持久化到存储 (Round 3)