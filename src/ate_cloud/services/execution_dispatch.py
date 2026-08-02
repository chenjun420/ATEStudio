"""Execution dispatch service — publishes YamlPlan to NATS JetStream.

Serializes a YamlPlan (produced by ExecutionPlanMaterializer) to JSON and
publishes it to the subject ``ate.tasks.{execution_id}`` on the ATE_TASKS
JetStream stream. The publish is fire-and-forget: JetStream durability
guarantees delivery to the worker consumer, so the caller (e.g. an HTTP
handler) is not blocked waiting for execution to start.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from enum import Enum
from typing import Any

from nats.aio.client import Client as NatsClient

from shared.dsl import YamlPlan

logger = logging.getLogger(__name__)


class ExecutionDispatchError(Exception):
    """Raised when dispatching an execution plan to JetStream fails."""


def _enum_to_value(o: Any) -> Any:
    """JSON ``default`` hook — convert Enum members to their ``.value``.

    ``dataclasses.asdict`` deep-copies non-dataclass field values rather than
    coercing them, so Enum fields (``LoopType``, ``ExecutionMode``) remain as
    Enum instances in the resulting dict. ``json.dumps`` needs this hook to
    serialize them to their value strings.
    """
    if isinstance(o, Enum):
        return o.value
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


class ExecutionDispatchService:
    """Publishes execution plans to the ATE_TASKS JetStream stream.

    The service is fire-and-forget: ``dispatch`` publishes the serialized plan
    to ``ate.tasks.{execution_id}`` and returns as soon as JetStream
    acknowledges durable storage (a ``PubAck``). It does NOT wait for a worker
    consumer to process the message — JetStream durability guarantees delivery.
    """

    def __init__(self, nats_client: NatsClient) -> None:
        self._nc = nats_client

    async def dispatch(self, execution_id: str, plan: YamlPlan) -> None:
        """Serialize ``plan`` to JSON and publish it to JetStream.

        Args:
            execution_id: The execution run ID. Becomes part of the subject
                ``ate.tasks.{execution_id}`` and is echoed in the headers.
            plan: The YamlPlan to dispatch.

        Raises:
            ExecutionDispatchError: If the JetStream publish fails.
        """
        subject = f"ate.tasks.{execution_id}"
        payload = json.dumps(asdict(plan), default=_enum_to_value).encode()
        headers: dict[str, str] = {"execution_id": execution_id}

        js = self._nc.jetstream()
        try:
            await js.publish(subject, payload, headers=headers)
        except Exception as e:
            raise ExecutionDispatchError(
                f"Failed to dispatch execution {execution_id}: {e}"
            ) from e
        logger.debug("Dispatched execution %s to %s", execution_id, subject)
