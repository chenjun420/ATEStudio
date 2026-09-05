"""Pydantic request/response schemas for the knowledge domain (task 10).

Boundary parse layer for the models in ``models/knowledge.py``. The CRUD
routers themselves are built later (task 12 ingestion, task 13 FMEA API,
task 15 diagnosis persistence); these schemas only define the wire shape:

- FMEA ratings are constrained to 1-10 at the boundary and ``rpn`` is NOT an
  accepted input — it is always derived server-side.
- Response schemas add id/timestamps and the computed ``rpn``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

#: FMEA rating bounds — mirrors models/knowledge.py RATING_MIN/MAX.
RATING_MIN = 1
RATING_MAX = 10

RequirementSource = Literal["dsl", "atml", "manual"]


# ── TestRequirement ─────────────────────────────────────────────────────────


class TestRequirementCreate(BaseModel):
    """Create payload for a test requirement."""

    product_code: str = Field(..., min_length=1, max_length=100)
    requirement_code: str = Field(..., min_length=1, max_length=100)
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    source: RequirementSource = "manual"
    atml_ref: str | None = Field(None, max_length=255)


class TestRequirementResponse(TestRequirementCreate):
    """A persisted test requirement."""

    model_config = {"from_attributes": True}

    id: str
    created_at: datetime
    updated_at: datetime


# ── TestCase ────────────────────────────────────────────────────────────────


class TestCaseCreate(BaseModel):
    """Create payload for a test case.

    ``requirement_id`` is nullable so cases can be ingested before their
    requirement (task 12 ingestion ordering) and linked later.
    """

    requirement_id: str | None = Field(None, max_length=36)
    case_code: str = Field(..., min_length=1, max_length=100)
    title: str = Field(..., min_length=1, max_length=255)
    sequence_id: str | None = Field(None, max_length=36)
    step_id: str = Field("", max_length=255)
    atml_ref: str | None = Field(None, max_length=255)
    status: str = Field("draft", max_length=20)


class TestCaseResponse(TestCaseCreate):
    """A persisted test case."""

    model_config = {"from_attributes": True}

    id: str
    created_at: datetime
    updated_at: datetime


# ── Read APIs: paged lists, traceability, graph browse (frontend tasks 25/26) ─


class RequirementPage(BaseModel):
    """Paged TestRequirement list envelope (same {items,total} shape as fmea)."""

    items: list[TestRequirementResponse]
    total: int


class CaseResponse(TestCaseResponse):
    """A test case joined to its requirement for the traceability matrix.

    ``product_code`` / ``requirement_code`` are denormalized from the linked
    requirement so the matrix can filter/group without a second round-trip;
    they are ``None`` for an unlinked (orphan) case.
    """

    product_code: str | None = None
    requirement_code: str | None = None


class CasePage(BaseModel):
    """Paged TestCase list envelope."""

    items: list[CaseResponse]
    total: int


class TraceabilityCase(BaseModel):
    """One case row inside the traceability tree (case → DSL step mapping)."""

    model_config = {"from_attributes": True}

    id: str
    case_code: str
    title: str
    sequence_id: str | None = None
    step_id: str = ""
    atml_ref: str | None = None
    status: str = "draft"


class TraceabilityRequirement(BaseModel):
    """A requirement with its verifying cases (requirement → cases → steps)."""

    id: str
    requirement_code: str
    title: str
    source: RequirementSource
    cases: list[TraceabilityCase] = []


class TraceabilityTree(BaseModel):
    """Structured requirement→cases→DSL-step tree for the matrix view.

    ``unlinked_cases`` holds cases whose ``requirement_id`` is unset (ingested
    before their requirement) so traceability gaps stay visible rather than
    being silently dropped.
    """

    product_code: str | None = None
    requirements: list[TraceabilityRequirement] = []
    unlinked_cases: list[TraceabilityCase] = []


class GraphNode(BaseModel):
    """A graph node shaped for a visualization UI.

    ``label`` is the primary node label/type; ``type`` mirrors it for graph
    libraries that key on ``type``; ``properties`` carries the remaining
    node properties.
    """

    id: str
    label: str
    type: str
    name: str = ""
    properties: dict[str, object] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """A directed relationship shaped for a visualization UI."""

    source: str
    target: str
    type: str


class GraphBrowse(BaseModel):
    """{nodes, edges} payload for the knowledge-graph browse view (task 25)."""

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []


# ── FMEA ────────────────────────────────────────────────────────────────────


class FMEABase(BaseModel):
    """Shared FMEA fields. Ratings are 1-10; ``rpn`` is never an input."""

    component_code: str = Field(..., min_length=1, max_length=200)
    function_name: str | None = Field(None, max_length=255)
    fault_code: str | None = Field(None, max_length=100)
    failure_mode: str = Field(..., min_length=1, max_length=500)
    effects: str | None = None
    cause: str | None = None
    severity: int = Field(..., ge=RATING_MIN, le=RATING_MAX)
    occurrence: int = Field(..., ge=RATING_MIN, le=RATING_MAX)
    detection: int = Field(..., ge=RATING_MIN, le=RATING_MAX)
    recommended_action: str | None = None


class FMEACreate(FMEABase):
    """Create payload for an FMEA entry (no rpn — derived server-side)."""


class FMEAUpdate(BaseModel):
    """Partial-update payload for an FMEA entry (PUT/PATCH).

    Every field is optional; supplied ratings are still constrained to 1-10.
    ``rpn`` is deliberately absent — it is always derived server-side; a
    client-supplied ``rpn`` is ignored (extra fields are dropped, as on
    create), never trusted.
    """

    component_code: str | None = Field(None, min_length=1, max_length=200)
    function_name: str | None = Field(None, max_length=255)
    fault_code: str | None = Field(None, max_length=100)
    failure_mode: str | None = Field(None, min_length=1, max_length=500)
    effects: str | None = None
    cause: str | None = None
    severity: int | None = Field(None, ge=RATING_MIN, le=RATING_MAX)
    occurrence: int | None = Field(None, ge=RATING_MIN, le=RATING_MAX)
    detection: int | None = Field(None, ge=RATING_MIN, le=RATING_MAX)
    recommended_action: str | None = None


class FMEAResponse(FMEABase):
    """A persisted FMEA entry with the computed RPN."""

    model_config = {"from_attributes": True}

    id: str
    rpn: int
    created_at: datetime
    updated_at: datetime


# ── Diagnosis ───────────────────────────────────────────────────────────────


class DiagnosisCreate(BaseModel):
    """Persist payload for a diagnosis (task 15). Feedback is captured later."""

    run_id: str | None = Field(None, max_length=36)
    session_id: str | None = Field(None, max_length=64)
    symptom: str = Field(..., min_length=1)
    conclusion: str | None = None
    context_summary: str | None = None
    llm_model: str | None = Field(None, max_length=100)


class DiagnosisFeedback(BaseModel):
    """Operator feedback on a diagnosis (confirm/reject + optional note)."""

    helpful: bool
    feedback_note: str | None = None


class DiagnosisResponse(DiagnosisCreate):
    """A persisted diagnosis, including any operator feedback."""

    id: str
    helpful: bool | None = None
    feedback_note: str | None = None
    created_at: datetime
    updated_at: datetime


# ── ATML TestDescription import (task 11) ───────────────────────────────────


class ATMLImportCounts(BaseModel):
    """Created/updated counters for one entity kind during an import."""

    created: int = 0
    updated: int = 0


class ATMLUnmappedCase(BaseModel):
    """A test case persisted without a DSL step link (traceability gap)."""

    case_code: str
    title: str
    reason: str


class ATMLImportSummary(BaseModel):
    """Result summary returned by POST /api/v1/atml/import-test-description."""

    product_code: str
    requirements: ATMLImportCounts
    cases: ATMLImportCounts
    unmapped: list[ATMLUnmappedCase] = []


# ── Knowledge extraction (task 12) ──────────────────────────────────────────


class KnowledgeExtractRequest(BaseModel):
    """Trigger deterministic extraction from structured server-side sources.

    Paths are server-local filesystem paths (DSL YAML plans and recordings
    JSONL); no file upload — python-multipart is not installed. ATML import
    uses its own raw-XML endpoint.
    """

    product_code: str = Field(..., min_length=1, max_length=100)
    dsl_paths: list[str] = Field(default_factory=list)
    recording_paths: list[str] = Field(default_factory=list)


class SourceExtractCounts(BaseModel):
    """Created/updated counters for one extraction source."""

    created: int = 0
    updated: int = 0


class KnowledgeExtractSummary(BaseModel):
    """Result summary returned by POST /api/v1/knowledge/extract."""

    product_code: str
    requirements: SourceExtractCounts
    cases: SourceExtractCounts
    recordings_read: int = 0
    results_written: int = 0
    recording_events_skipped: int = 0
    recordings_skipped: list[str] = []
    unmatched_steps: list[str] = []
    graph_status: str = "ok"  # "ok" | "degraded" (no graph backend configured)
