"""Diagnosis Pusher — publishes AI diagnosis results to operator UI via Core NATS.

Publishes diagnosis events to ``ate.diagnosis.{execution_id}`` NATS subject
using Core NATS publish (``nc.publish``) — not JetStream — for fire-and-forget
real-time delivery to SSE subscribers.

Per AGENTS.md §7: if the NATS client is configured but not connected, the
push raises ``RuntimeError`` — no silent degradation.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nats.aio.client import Client as NatsClient

logger = logging.getLogger(__name__)

#: Subject prefix for diagnosis events — full subject is ``ate.diagnosis.{execution_id}``.
_DIAGNOSIS_SUBJECT_PREFIX = "ate.diagnosis"


class DiagnosisPusher:
    """Publishes AI diagnosis results to operator UI via Core NATS.

    Uses Core NATS publish (``nc.publish``) — not JetStream — for
    fire-and-forget real-time delivery. The payload is a UTF-8 JSON
    encoding of the diagnosis dict (``root_cause``, ``confidence``,
    ``suggested_fix``, ``explanation``).

    Args:
        nc: Connected NATS client. If ``None``, push() raises RuntimeError.
    """

    def __init__(self, nc: NatsClient | None = None) -> None:
        self._nc = nc

    @property
    def nats_available(self) -> bool:
        """Whether the NATS client is connected and ready for publish."""
        return self._nc is not None and self._nc.is_connected

    async def push(
        self,
        execution_id: str,
        diagnosis: dict[str, Any],
    ) -> None:
        """Publish a diagnosis result to ``ate.diagnosis.{execution_id}``.

        Args:
            execution_id: The execution/run ID for subject routing.
            diagnosis: Diagnosis dict with ``root_cause``, ``confidence``,
                ``suggested_fix``, ``explanation``.

        Raises:
            RuntimeError: If NATS client is not connected (no silent
                degradation per AGENTS.md §7).
        """
        if not self.nats_available:
            raise RuntimeError(
                "DiagnosisPusher: NATS client not connected — cannot push diagnosis "
                "(no silent degradation per AGENTS.md §7)"
            )

        assert self._nc is not None  # Narrow type after nats_available check

        subject = f"{_DIAGNOSIS_SUBJECT_PREFIX}.{execution_id}"
        payload = json.dumps(diagnosis, ensure_ascii=False).encode("utf-8")

        await self._nc.publish(subject, payload)
        logger.info(
            "Pushed diagnosis to %s (execution_id=%s)",
            subject,
            execution_id,
        )


__all__ = ["DiagnosisPusher"]
