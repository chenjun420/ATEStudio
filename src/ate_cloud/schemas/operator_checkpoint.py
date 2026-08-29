"""Pydantic schemas for the operator checkpoint API.

Defines request/response models for the operator checkpoint endpoints:
- ``OperatorCheckpointRequest`` - request body for submitting a response
- ``OperatorCheckpointResponse`` - response describing a pending checkpoint
- ``OperatorInteractionEvent`` - SSE event payload emitted to the operator UI

The cloud endpoints store pending checkpoints on ``app.state.checkpoints``
(keyed by ``run_id``) -- mirroring the ``app.state.recorders`` pattern
used by the recordings API. The :class:`CheckpointHandler` on the
platform side blocks the executor until the cloud publishes a response
via NATS (or the handler is cancelled by timeout).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shared.operator_checkpoint import OperatorCheckpoint, OperatorInteractionType

__all__ = [
    "OperatorCheckpointRequest",
    "OperatorCheckpointResponse",
    "OperatorCheckpointAckRequest",
    "OperatorCheckpointAckResponse",
    "OperatorCheckpointIdAckRequest",
    "OperatorInteractionEvent",
    "OperatorInteractionType",
]


class OperatorInteractionEvent(BaseModel):
    """SSE event payload describing a pending operator checkpoint.

    Emitted on the SSE bridge under event type ``OPERATOR_CHECKPOINT``
    so the operator UI can open the modal dialog. The frontend
    :component:`OperatorInteraction` listens for these events and
    renders the appropriate input based on ``checkpoint.type``.

    Attributes:
        run_id: Execution run identifier.
        step_id: Step identifier that triggered the checkpoint.
        checkpoint: The full checkpoint definition (type, prompt, ...).
        created_at: UTC timestamp when the checkpoint was created.
        checkpoint_id: Stable uuid assigned by the cloud API when it first
            observes the pending checkpoint (RH-6). Present only while a
            checkpoint is pending; used by the alias ack path
            ``POST /api/v1/checkpoints/{checkpoint_id}/ack``.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    step_id: str
    checkpoint: OperatorCheckpoint
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    checkpoint_id: str | None = None


class OperatorCheckpointResponse(BaseModel):
    """Response describing a pending (or resolved) checkpoint for a run.

    Returned by ``GET /api/v1/executions/{run_id}/checkpoint/pending``.
    When no checkpoint is pending, ``pending`` is ``False`` and the
    other fields are absent.

    Attributes:
        run_id: Execution run identifier.
        pending: Whether a checkpoint is currently awaiting a response.
        step_id: Step identifier that triggered the checkpoint (None if
            no checkpoint pending).
        checkpoint: The full checkpoint definition (None if not pending).
        created_at: UTC timestamp when the pending checkpoint was created.
    """

    run_id: str
    pending: bool
    step_id: str | None = None
    checkpoint: OperatorCheckpoint | None = None
    created_at: datetime | None = None
    checkpoint_id: str | None = None


class OperatorCheckpointRequest(BaseModel):
    """Request body for submitting an operator's response to a checkpoint.

    Submitted via ``POST /api/v1/executions/{run_id}/checkpoint``. The
    ``response`` field carries the operator input:

    - ``scan`` / ``manual_input``: the text the operator entered
    - ``visual_check``: ``"pass"`` or ``"fail"``
    - ``confirm``: ``"ok"`` (or any non-empty acknowledgement)

    For ``visual_check`` with ``response == "fail"`` the executor will
    fail the step with the optional ``reason`` as the error message.

    Attributes:
        step_id: The step identifier whose checkpoint is being answered.
        response: The operator's response payload (text/acknowledgement).
        reason: Optional reason (e.g. visual_check fail reason).
        extra: Optional metadata bag for future extension.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(..., min_length=1, description="Step identifier being answered")
    response: str = Field(..., min_length=1, description="Operator response payload")
    reason: str | None = Field(default=None, description="Optional reason (e.g. fail reason)")
    extra: dict[str, Any] = Field(default_factory=dict, description="Optional metadata bag")


class OperatorCheckpointAckRequest(BaseModel):
    """Request body for the operator-console acknowledgement endpoint.

    Submitted via ``POST /api/v1/executions/{run_id}/checkpoint/ack``
    (T42 operator checkpoint flow). Unlike the raw
    :class:`OperatorCheckpointRequest`, this records *who* acknowledged
    and an optional free-form note; the underlying checkpoint always
    receives ``response="ok"``.

    Attributes:
        step_id: The step identifier whose checkpoint is being acked.
        operator: Operator name/ID performing the acknowledgement.
        note: Optional free-form note recorded with the acknowledgement.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(..., min_length=1, description="Step identifier being acked")
    operator: str = Field(
        ...,
        min_length=1,
        description="操作员姓名/工号（必须记录签署人）",
    )
    note: str | None = Field(default=None, description="Optional acknowledgement note")


class OperatorCheckpointIdAckRequest(BaseModel):
    """Request body for the checkpoint-id ack alias endpoint (RH-6).

    Submitted via ``POST /api/v1/checkpoints/{checkpoint_id}/ack`` -- the
    design-doc path alias. Unlike :class:`OperatorCheckpointAckRequest` the
    caller does not supply ``step_id`` (or ``run_id``): the cloud-side
    registry resolves them from ``checkpoint_id``. Only the signing
    operator identity and optional note are carried.

    Attributes:
        operator: Operator name/ID performing the acknowledgement.
        note: Optional free-form note recorded with the acknowledgement.
    """

    model_config = ConfigDict(extra="forbid")

    operator: str = Field(
        ...,
        min_length=1,
        description="操作员姓名/工号（必须记录签署人）",
    )
    note: str | None = Field(default=None, description="Optional acknowledgement note")


class OperatorCheckpointAckResponse(BaseModel):
    """Response describing a completed operator acknowledgement.

    Returned by ``POST /api/v1/executions/{run_id}/checkpoint/ack``.

    Attributes:
        run_id: Execution run identifier.
        step_id: Step identifier that was acknowledged.
        operator: Operator name/ID echoed back for UI confirmation.
        note: Optional note echoed back.
        acknowledged_at: UTC timestamp of the acknowledgement.
        pending: Always ``False`` — the checkpoint is resolved.
    """

    run_id: str
    step_id: str
    operator: str
    note: str | None = None
    acknowledged_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    pending: bool = False
