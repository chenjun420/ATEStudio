"""Test-condition schemas (P5 front end).

The review UI is where ATERag's unapproved proposals get signed off, so these
schemas exist to serve one workflow: *show a human the conditions, and record
their decision*. Two design consequences:

- ``status`` is a first-class field, not a decoration. The red line is that an
  unapproved condition must not become a production criterion, so the UI has to
  be able to filter on it and bulk-approve by requirement.

- The approve call takes ``by`` (who signed). An approval with no name on it
  cannot be audited, and an untraceable sign-off is indistinguishable from no
  sign-off at all — which is the state everything starts in.

Only ``approved`` conditions are eligible for bulk approval. Approving a draft
is a human decision; letting the API do it automatically would recreate the
red-line violation the workflow exists to prevent.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

#: Review states a condition can be in. ``draft`` is the state ATERag's
#: industry-method proposals arrive in; ``approved`` is what the line accepts.
CONDITION_STATUSES = ("draft", "approved")


class TestConditionResponse(BaseModel):
    """One test condition clause as the review UI renders it."""

    model_config = {"from_attributes": True}

    id: str
    owner_type: str
    owner_id: str
    side: str = Field(..., description="input (须设置) | output (须判定)")
    kind: str
    text: str
    value: dict[str, object] | None = None
    source: str = Field(..., description="notes | limits | industry_method | ...")
    confidence: str
    status: str
    method_ref: str | None = None
    cond_fingerprint: str
    created_at: datetime
    updated_at: datetime
    # Denormalized for display so the table needs no second request per row.
    requirement_code: str | None = None
    requirement_title: str | None = None
    section_path: str | None = None
    #: The spec clause the condition was extracted from, and the spec note
    #: attached to it. A reviewer confirms that the clause says what the
    #: condition claims; that check is impossible without the text on screen,
    #: so a signature taken without it is a signature on a blank form.
    requirement_description: str | None = None
    requirement_notes: str | None = None
    #: Extraction signals for the owning requirement. ``annotation_draft`` in
    #: ``flags`` marks conditions a person wrote rather than the rules cut —
    #: unapproved criteria that must not become production bounds.
    requirement_flags: list[str] = Field(default_factory=list)
    requirement_assessment: dict[str, str] = Field(default_factory=dict)


class ConditionPage(BaseModel):
    """Paged condition list ({items,total}), matching the knowledge list shape."""

    items: list[TestConditionResponse]
    total: int


class ConditionReviewRequest(BaseModel):
    """Approve a batch of conditions belonging to one requirement.

    Scoped to a requirement on purpose: approving "everything currently in
    draft" would sweep in conditions extracted for a different spec clause by
    someone who had not opened it. The review screen is organised by
    requirement, and the API follows that.
    """

    requirement_id: str = Field(..., min_length=1)
    by: str = Field(
        ...,
        min_length=1,
        max_length=120,
        description="签字人姓名或工号 —— 无署名��批准无法审计, 与未签字不可区分",
    )
    #: Optional narrowing: approve only these condition ids within the
    #: requirement. Absent means "all drafts under this requirement".
    condition_ids: list[str] | None = None
    note: str = Field("", max_length=500)

    @field_validator("by")
    @classmethod
    def _by_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("签字人不能为空白")
        return v.strip()


class ConditionReviewResult(BaseModel):
    """Outcome of one approve call."""

    requirement_id: str
    approved: int
    skipped: int
    by: str
    message: str
