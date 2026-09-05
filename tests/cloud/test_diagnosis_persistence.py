"""Task 15 — DiagnosisService sharing + Diagnosis persistence/feedback tests.

Covers the task-15 contract with fakes only (no live FalkorDB/Qdrant/OpenAI):

* ONE shared ``DiagnosisService`` is lazily built and cached on
  ``app.state`` — two requests reuse the same instance (no per-request
  construction).
* ``POST /api/v1/diagnose`` persists a ``Diagnosis`` ORM row
  (symptom/conclusion/session_id/llm_model) and links it to the run when
  ``run_id`` is given.
* ``POST /api/v1/diagnose/{id}/feedback`` updates ``helpful``/
  ``feedback_note`` on that row (confirmed -> True, rejected -> False) and
  404s an unknown id.
* No LLM key -> retrieval-only diagnosis is still returned AND persisted.
* Graph branch down -> retrieval degrades (200, possibly empty) and the
  diagnosis is still persisted (task-14 graceful degrade unchanged).

The conftest ``client`` fixture supplies an in-memory SQLite session via
the ``get_db`` override; retrieval collaborators are overridden here with
in-memory fakes so the REAL lazy factory chain (graph/embedding/qdrant ->
HybridRetriever -> DiagnosisService caching) executes.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from ate_cloud.api.v1 import diagnose as diag_module
from ate_cloud.config import settings
from ate_cloud.models.execution import Execution
from ate_cloud.models.knowledge import Diagnosis

# ── Retrieval fakes (in-memory; no live services) ──────────────────────────


class _Point:
    def __init__(self, point_id: str, score: float, payload: dict[str, Any]) -> None:
        self.id = point_id
        self.score = score
        self.payload = payload


class FakeQdrant:
    """Qdrant stand-in returning scripted points; can fail to simulate outage."""

    def __init__(self, points: list[_Point] | None = None) -> None:
        self._points = points or []
        self.fail: Exception | None = None

    def search(self, **kwargs: Any) -> list[_Point]:
        if self.fail is not None:
            raise self.fail
        return list(self._points)


class FakeEmbedding:
    """Deterministic embeddings for query + rerank (no network)."""

    async def embed(self, text: str) -> list[float]:
        return [0.1] * 8

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 8 for _ in texts]


def _qdrant_points() -> list[_Point]:
    return [
        _Point(
            "p-001",
            0.91,
            {
                "error_code": "I2C_TIMEOUT",
                "symptom": "I2C communication timeout on bus 1",
                "failed_step_name": "test_i2c_comm",
            },
        )
    ]


def _wire_retrieval_fakes(
    app: Any,
    *,
    qdrant: FakeQdrant | None = None,
    graph: Any | None = None,
) -> None:
    """Override the three external collaborators; keep the REAL factories.

    HybridRetriever and DiagnosisService are deliberately NOT overridden so
    the lazy get_or_create caching on app.state is exercised end to end.
    """
    from .ontology_graph_fake import OntologyGraphFake

    graph_fake = graph if graph is not None else OntologyGraphFake().seed_ontology()
    qdrant_fake = qdrant if qdrant is not None else FakeQdrant(_qdrant_points())

    app.dependency_overrides[diag_module._get_graph_service] = lambda: graph_fake
    app.dependency_overrides[diag_module._get_embedding_service] = lambda: FakeEmbedding()
    app.dependency_overrides[diag_module._get_qdrant_client] = lambda: qdrant_fake


def _diagnose_payload(**extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "product_type": "COMM-DEV-001",
        "failed_test": "test_i2c_comm",
        "error_code": "ERR_I2C_TIMEOUT",
        "log_snippet": "TimeoutError: I2C read failed",
    }
    payload.update(extra)
    return payload


async def _fetch_diagnosis(db_session: Any, diagnosis_id: str) -> Diagnosis:
    row = (
        await db_session.execute(select(Diagnosis).where(Diagnosis.id == diagnosis_id))
    ).scalar_one()
    return row


@pytest.fixture(autouse=True)
def _no_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default these tests to retrieval-only (no real LLM network call).

    The single LLM-path test re-enables a fake key and stubs the model.
    """
    monkeypatch.setattr(settings, "openai_api_key", "")


# ── Shared service instance ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_requests_share_one_diagnosis_service(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given the real lazy factory, two POSTs construct DiagnosisService once."""
    monkeypatch.setattr(settings, "openai_api_key", "")
    _wire_retrieval_fakes(client.app)

    real_cls = diag_module.DiagnosisService
    constructions = {"n": 0}

    class CountingService(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            constructions["n"] += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(diag_module, "DiagnosisService", CountingService)

    resp1 = await client.post("/api/v1/diagnose", json=_diagnose_payload())
    resp2 = await client.post("/api/v1/diagnose", json=_diagnose_payload())
    assert resp1.status_code == 200, resp1.text
    assert resp2.status_code == 200, resp2.text

    assert constructions["n"] == 1
    assert client.app.state.diagnosis_service is not None


# ── Persistence ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_diagnose_persists_row_linked_to_run(
    client: Any, db_session: Any
) -> None:
    """POST diagnose creates a Diagnosis row linked to the given run/session."""
    _wire_retrieval_fakes(client.app)

    run_id = str(uuid.uuid4())
    db_session.add(Execution(id=run_id, status="COMPLETED"))
    await db_session.flush()

    resp = await client.post(
        "/api/v1/diagnose",
        json=_diagnose_payload(run_id=run_id, session_id="edge-session-7"),
    )
    assert resp.status_code == 200, resp.text
    diagnosis_id = resp.json()["diagnosis_id"]

    row = await _fetch_diagnosis(db_session, diagnosis_id)
    assert row.run_id == run_id
    assert row.session_id == "edge-session-7"
    assert "test_i2c_comm" in row.symptom
    assert row.helpful is None  # no feedback yet
    assert row.feedback_note is None
    assert row.context_summary  # retrieved context summarized


@pytest.mark.asyncio
async def test_feedback_confirmed_and_rejected_update_row(
    client: Any, db_session: Any
) -> None:
    """Feedback flips helpful (True/False) and stores the correction note."""
    _wire_retrieval_fakes(client.app)

    resp = await client.post("/api/v1/diagnose", json=_diagnose_payload())
    diagnosis_id = resp.json()["diagnosis_id"]

    ok = await client.post(
        f"/api/v1/diagnose/{diagnosis_id}/feedback",
        json={"feedback": "confirmed"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["recorded"] is True

    row = await _fetch_diagnosis(db_session, diagnosis_id)
    assert row.helpful is True
    assert row.feedback_note is None

    rejected = await client.post(
        f"/api/v1/diagnose/{diagnosis_id}/feedback",
        json={"feedback": "rejected", "correction": "Actual cause: cold solder J5"},
    )
    assert rejected.status_code == 200, rejected.text
    row = await _fetch_diagnosis(db_session, diagnosis_id)
    assert row.helpful is False
    assert row.feedback_note == "Actual cause: cold solder J5"


@pytest.mark.asyncio
async def test_feedback_unknown_diagnosis_returns_404(client: Any) -> None:
    """Feedback for a diagnosis id that was never persisted -> 404."""
    _wire_retrieval_fakes(client.app)
    resp = await client.post(
        "/api/v1/diagnose/does-not-exist/feedback",
        json={"feedback": "confirmed"},
    )
    assert resp.status_code == 404, resp.text


# ── Graceful degrade ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_key_retrieval_only_is_persisted(
    client: Any, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an LLM key, diagnosis is retrieval-only AND still persisted."""
    monkeypatch.setattr(settings, "openai_api_key", "")
    _wire_retrieval_fakes(client.app)

    resp = await client.post("/api/v1/diagnose", json=_diagnose_payload())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["root_cause"] == ""
    assert data["confidence"] == 0.0
    assert len(data["retrieved_cases"]) >= 1

    row = await _fetch_diagnosis(db_session, data["diagnosis_id"])
    assert row.conclusion is None  # no LLM conclusion
    assert row.llm_model is None
    assert "test_i2c_comm" in row.symptom


@pytest.mark.asyncio
async def test_graph_down_degrades_and_still_persists(
    client: Any, db_session: Any
) -> None:
    """Graph branch raising degrades to surviving retrieval and still persists."""
    from .ontology_graph_fake import OntologyGraphFake

    broken_graph = OntologyGraphFake().seed_ontology()
    broken_graph.fail_with = RuntimeError("graph down")
    _wire_retrieval_fakes(client.app, graph=broken_graph)

    resp = await client.post("/api/v1/diagnose", json=_diagnose_payload())
    assert resp.status_code == 200, resp.text  # never a 500
    diagnosis_id = resp.json()["diagnosis_id"]

    row = await _fetch_diagnosis(db_session, diagnosis_id)
    assert row.id == diagnosis_id
    assert "test_i2c_comm" in row.symptom
