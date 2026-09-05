"""Knowledge-domain SQLAlchemy models (task 10, ontology-driven persistence).

Deterministic relational layer for the ontology entities defined in
``services/ontology/core.py`` (TestRequirement / TestCase / FMEA) plus the
persisted Diagnosis that task 15 links to an execution run. No LLM, no graph
DB — plain tables that run on SQLite (dev) and PostgreSQL (prod).

- ``test_requirements`` 1—N ``test_cases`` (soft FK; cases may be ingested
  before their requirement, so ``requirement_id`` is nullable).
- ``fmeas`` carries severity/occurrence/detection as integers in [1, 10]
  (DISTINCT from the 3-level fixture ``Severity`` enum). ``rpn`` is a stored
  column DERIVED server-side as S*O*D — a client-supplied ``rpn`` is ignored,
  and the range is enforced both in the validator and a DB CHECK constraint.
- ``diagnoses`` links a diagnosis (symptom/conclusion/retrieved context) to
  an execution run, with nullable operator feedback (helpful bool + note).

Vocabulary columns (``source``, ``fault_code``, ``status``) store the stable
canonical ID strings from ``services/ontology/vocab.py`` (FaultKind etc.).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from . import Base

#: FMEA rating bounds (ontology core.py _FMEA_MIN/_FMEA_MAX; max RPN = 1000).
RATING_MIN = 1
RATING_MAX = 10

#: TestRequirement provenance.
SOURCE_DSL = "dsl"
SOURCE_ATML = "atml"
SOURCE_MANUAL = "manual"


def _require_rating(name: str, value: Any) -> int:
    """Validate an FMEA rating is an int in [1, 10] (bool is not an int)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"FMEA {name} must be an integer in [1, 10], got {value!r}")
    if not RATING_MIN <= value <= RATING_MAX:
        raise ValueError(
            f"FMEA {name} must be in [{RATING_MIN}, {RATING_MAX}], got {value}"
        )
    return value


class TestRequirement(Base):
    """A verifiable test requirement from a product spec / ATML TestDescription.

    Attributes:
        id: UUID (string form).
        product_code: Product / UUT reference (stable code, indexed).
        requirement_code: Human requirement identifier (e.g. REQ-PSU-001).
        title: Short requirement title.
        description: Full requirement text.
        source: Provenance: ``dsl`` | ``atml`` | ``manual``.
        atml_ref: Optional IEEE 1671 TestDescription reference.
        created_at / updated_at: Timestamps.
    """

    # Not a pytest test class — the name starts with "Test" so tell pytest not
    # to collect it (suppresses PytestCollectionWarning "cannot collect").
    __test__ = False

    __tablename__ = "test_requirements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    requirement_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default=SOURCE_MANUAL)
    atml_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TestCase(Base):
    """A test case implementing one or more requirements, mapped to DSL steps.

    Attributes:
        id: UUID (string form).
        requirement_id: FK to test_requirements.id; NULLABLE so cases can be
            ingested before their requirement (ingestion ordering, task 12).
        case_code: Human test-case identifier (e.g. TC-VOLT-001).
        title: Short case title.
        sequence_id: DSL sequence reference (sequences.id), nullable.
        step_id: DSL step reference within the sequence (YamlStep id), "".
        atml_ref: Optional IEEE 1671 TestItem/TestStep reference.
        status: Lifecycle status (draft/active/retired; default draft).
        created_at / updated_at: Timestamps.
    """

    # Not a pytest test class — the name starts with "Test" so tell pytest not
    # to collect it (suppresses PytestCollectionWarning "cannot collect").
    __test__ = False

    __tablename__ = "test_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requirement_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("test_requirements.id"), nullable=True, index=True
    )
    case_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    sequence_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    step_id: Mapped[str] = mapped_column(String(255), nullable=False, default="", index=True)
    atml_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class FMEA(Base):
    """FMEA analysis entry with 1-10 ratings and a server-derived RPN.

    Attributes:
        id: UUID (string form).
        component_code: Component/function this entry analyzes (ontology
            Component stable code / free-text component id, indexed).
        function_name: Optional function of the component being analyzed.
        fault_code: Canonical FaultKind id from services/ontology/vocab.py
            (nullable — entries may predate vocab resolution).
        failure_mode: Failure mode description (required).
        effects: Failure effect(s) description.
        cause: Failure cause description (Symptom->Cause chain).
        severity / occurrence / detection: Integer ratings in [1, 10].
        rpn: Risk priority number = S*O*D, DERIVED (client value ignored).
        recommended_action: Recommended mitigation / repair action.
        created_at / updated_at: Timestamps.
    """

    __tablename__ = "fmeas"
    __table_args__ = (
        CheckConstraint(
            f"severity BETWEEN {RATING_MIN} AND {RATING_MAX}",
            name="ck_fmeas_severity_range",
        ),
        CheckConstraint(
            f"occurrence BETWEEN {RATING_MIN} AND {RATING_MAX}",
            name="ck_fmeas_occurrence_range",
        ),
        CheckConstraint(
            f"detection BETWEEN {RATING_MIN} AND {RATING_MAX}",
            name="ck_fmeas_detection_range",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    component_code: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    function_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fault_code: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    failure_mode: Mapped[str] = mapped_column(String(500), nullable=False)
    effects: Mapped[str | None] = mapped_column(Text, nullable=True)
    cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[int] = mapped_column(Integer, nullable=False)
    occurrence: Mapped[int] = mapped_column(Integer, nullable=False)
    detection: Mapped[int] = mapped_column(Integer, nullable=False)
    rpn: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recommended_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @validates("severity", "occurrence", "detection")
    def _validate_rating(self, key: str, value: Any) -> int:
        """Reject out-of-range ratings and eagerly re-derive rpn from S*O*D."""
        rating = _require_rating(key, value)
        sev = getattr(self, "severity", None)
        occ = getattr(self, "occurrence", None)
        det = getattr(self, "detection", None)
        if key == "severity":
            sev = rating
        elif key == "occurrence":
            occ = rating
        else:
            det = rating
        if sev is not None and occ is not None and det is not None:
            self.rpn = sev * occ * det
        return rating


def _derive_rpn(_mapper: Any, _connection: Any, target: FMEA) -> None:
    """Authoritative server-side RPN: S*O*D at flush, client value ignored."""
    target.rpn = target.severity * target.occurrence * target.detection


event.listen(FMEA, "before_insert", _derive_rpn)
event.listen(FMEA, "before_update", _derive_rpn)


class Diagnosis(Base):
    """Persisted AI-diagnosis record linked to an execution run (task 15).

    Attributes:
        id: UUID (string form).
        run_id: FK to executions.id (the diagnosed run); nullable for
            diagnoses not tied to a persisted execution.
        session_id: Optional edge session / NATS session reference.
        symptom: Observed symptom text (required).
        conclusion: Fault conclusion / root-cause answer.
        context_summary: Summary of the retrieved context (RAG/KG hits).
        helpful: Operator feedback: True (confirmed) / False (rejected) /
            None (no feedback yet).
        feedback_note: Optional operator correction / note.
        llm_model: Chat model used for the diagnosis.
        created_at / updated_at: Timestamps.
    """

    __tablename__ = "diagnoses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("executions.id"), nullable=True, index=True
    )
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    symptom: Mapped[str] = mapped_column(Text, nullable=False)
    conclusion: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    helpful: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    feedback_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
