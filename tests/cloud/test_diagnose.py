"""Tests for DiagnosisService and POST /api/v1/diagnose API endpoints.

Uses mocked HybridRetriever and LLM - no real Qdrant/FalkorDB/OpenAI required.
The autouse ``_dev_mode_bypass`` fixture from conftest.py bypasses auth.

Persistence and feedback-on-row behavior (task 15) lives in
``test_diagnosis_persistence.py``; this file covers the service pipeline and
request/response contracts. Feedback now updates the persisted Diagnosis
row, so the feedback endpoint tests first create a diagnosis.
"""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ate_cloud.api.v1.diagnose import _get_diagnosis_service, _get_hybrid_retriever
from ate_cloud.db import get_db
from ate_cloud.models import Base
from ate_cloud.services.diagnosis_service import DiagnosisRequest, DiagnosisService
from ate_cloud.services.hybrid_retriever import HybridRetriever

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_retriever() -> MagicMock:
    """A MagicMock simulating HybridRetriever with async search()."""
    retriever = MagicMock(spec=HybridRetriever)
    retriever.search = AsyncMock(return_value=[])
    return retriever


@pytest.fixture
def diagnosis_service(mock_retriever: MagicMock) -> DiagnosisService:
    """Create a DiagnosisService with a mocked retriever and fake API key."""
    return DiagnosisService(
        hybrid_retriever=mock_retriever,
        api_key="fake-api-key-for-testing",
    )


@pytest.fixture
def diagnosis_service_no_key(mock_retriever: MagicMock) -> DiagnosisService:
    """Create a DiagnosisService with no API key (retrieval-only mode)."""
    return DiagnosisService(
        hybrid_retriever=mock_retriever,
        api_key="",
    )


@pytest.fixture
async def app_with_diagnose(
    mock_retriever: MagicMock,
    diagnosis_service: DiagnosisService,
) -> AsyncGenerator[FastAPI, None]:
    """Create a FastAPI app with diagnose_router and mocked dependencies.

    A private in-memory SQLite session backs the ``get_db`` dependency so
    diagnosis/feedback persistence (task 15) works without a real database.
    """
    from ate_cloud.main import create_app

    app = create_app()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # Override dependencies to use mocked services
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[_get_hybrid_retriever] = lambda: mock_retriever
    app.dependency_overrides[_get_diagnosis_service] = lambda: diagnosis_service
    try:
        yield app
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.fixture
async def diagnose_client(app_with_diagnose: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client targeting the diagnose API with mocked services."""
    transport = ASGITransport(app=app_with_diagnose)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Sample data ───────────────────────────────────────────────────────────


def _sample_retrieved_cases() -> list[dict[str, Any]]:
    """Sample retrieved failure cases (as HybridRetriever would return)."""
    return [
        {
            "id": "qdrant-001",
            "source": "qdrant",
            "rrf_score": 0.0164,
            "symptom": "I2C communication timeout on bus 1",
            "error_message": "TimeoutError: I2C read failed after 100ms",
            "failed_step_name": "test_i2c_comm",
        },
        {
            "id": "fault:i2c_timeout",
            "source": "graph",
            "rrf_score": 0.0159,
            "symptom": "I2C communication timeout",
            "cause": "Pull-up resistor missing on SDA line",
            "solution": "Add 4.7kΩ pull-up resistor to SDA",
            "component": "I2C bus",
        },
    ]


def _sample_llm_response() -> str:
    """Sample LLM JSON response."""
    return (
        '{"root_cause": "Missing pull-up resistor on I2C SDA line causes '
        'communication timeout", "confidence": 0.92, '
        '"evidence_citations": ["Case qdrant-001: I2C communication timeout", '
        '"Case fault:i2c_timeout: Pull-up resistor missing on SDA line"], '
        '"repair_steps": ["Check SDA line for pull-up resistor", '
        '"Add 4.7kΩ pull-up resistor if missing", "Re-run I2C communication test"]}'
    )


# ── DiagnosisService unit tests ───────────────────────────────────────────


class TestDiagnosisService:
    """Unit tests for DiagnosisService (no HTTP, no real LLM)."""

    @pytest.mark.asyncio
    async def test_diagnose_with_llm(
        self,
        diagnosis_service: DiagnosisService,
        mock_retriever: MagicMock,
    ) -> None:
        """Diagnosis with LLM returns structured result with citations."""
        mock_retriever.search.return_value = _sample_retrieved_cases()

        # Mock the LLM call
        diagnosis_service._ensure_initialized()
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = _sample_llm_response()
        mock_llm.ainvoke.return_value = mock_response
        diagnosis_service._llm = mock_llm

        result = await diagnosis_service.diagnose(
            product_type="COMM-DEV-001",
            failed_test="test_i2c_comm",
            error_code="ERR_I2C_TIMEOUT",
            log_snippet="TimeoutError: I2C read failed",
        )

        assert "diagnosis_id" in result
        assert isinstance(result["diagnosis_id"], str)
        assert result["root_cause"] == (
            "Missing pull-up resistor on I2C SDA line causes communication timeout"
        )
        assert result["confidence"] == pytest.approx(0.92)
        assert len(result["evidence_citations"]) == 2
        assert "qdrant-001" in result["evidence_citations"][0]
        assert len(result["repair_steps"]) == 3
        assert len(result["retrieved_cases"]) == 2

        # Verify retriever was called with the query
        mock_retriever.search.assert_called_once()
        call_args = mock_retriever.search.call_args
        query_text = call_args[0][0] if call_args[0] else call_args[1].get("query", "")
        assert "test_i2c_comm" in query_text
        assert "ERR_I2C_TIMEOUT" in query_text

    @pytest.mark.asyncio
    async def test_diagnose_no_api_key_returns_retrieval_only(
        self,
        diagnosis_service_no_key: DiagnosisService,
        mock_retriever: MagicMock,
    ) -> None:
        """Without API key, returns retrieval-only result (no LLM call)."""
        mock_retriever.search.return_value = _sample_retrieved_cases()

        result = await diagnosis_service_no_key.diagnose(
            product_type="COMM-DEV-001",
            failed_test="test_i2c_comm",
        )

        assert result["root_cause"] == ""
        assert result["confidence"] == 0.0
        assert result["repair_steps"] == []
        assert len(result["evidence_citations"]) == 2
        assert len(result["retrieved_cases"]) == 2
        # LLM was never initialized
        assert diagnosis_service_no_key._initialized is False

    @pytest.mark.asyncio
    async def test_diagnose_empty_retrieval(
        self,
        diagnosis_service: DiagnosisService,
        mock_retriever: MagicMock,
    ) -> None:
        """Diagnosis with no retrieved cases still returns a result."""
        mock_retriever.search.return_value = []

        diagnosis_service._ensure_initialized()
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = (
            '{"root_cause": "Unknown - no similar cases found", '
            '"confidence": 0.1, "evidence_citations": [], "repair_steps": []}'
        )
        mock_llm.ainvoke.return_value = mock_response
        diagnosis_service._llm = mock_llm

        result = await diagnosis_service.diagnose(
            product_type="UNKNOWN-PROD",
            failed_test="unknown_test",
        )

        assert result["root_cause"] == "Unknown - no similar cases found"
        assert result["confidence"] == pytest.approx(0.1)
        assert result["evidence_citations"] == []
        assert result["repair_steps"] == []
        assert result["retrieved_cases"] == []

    @pytest.mark.asyncio
    async def test_diagnose_llm_returns_markdown_fenced_json(
        self,
        diagnosis_service: DiagnosisService,
        mock_retriever: MagicMock,
    ) -> None:
        """LLM response with markdown code fences is parsed correctly."""
        mock_retriever.search.return_value = _sample_retrieved_cases()

        diagnosis_service._ensure_initialized()
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = (
            "```json\n"
            '{"root_cause": "Capacitor failure", "confidence": 0.85, '
            '"evidence_citations": ["case-1"], "repair_steps": ["replace C3"]}\n'
            "```"
        )
        mock_llm.ainvoke.return_value = mock_response
        diagnosis_service._llm = mock_llm

        result = await diagnosis_service.diagnose(
            product_type="PROD-001",
            failed_test="power_test",
        )

        assert result["root_cause"] == "Capacitor failure"
        assert result["confidence"] == pytest.approx(0.85)

    @pytest.mark.asyncio
    async def test_diagnose_llm_returns_invalid_json(
        self,
        diagnosis_service: DiagnosisService,
        mock_retriever: MagicMock,
    ) -> None:
        """Invalid LLM JSON response falls back to raw text in root_cause."""
        mock_retriever.search.return_value = _sample_retrieved_cases()

        diagnosis_service._ensure_initialized()
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "This is not JSON at all"
        mock_llm.ainvoke.return_value = mock_response
        diagnosis_service._llm = mock_llm

        result = await diagnosis_service.diagnose(
            product_type="PROD-001",
            failed_test="power_test",
        )

        assert result["root_cause"] == "This is not JSON at all"
        assert result["confidence"] == 0.0
        assert result["evidence_citations"] == []
        assert result["repair_steps"] == []

    @pytest.mark.asyncio
    async def test_diagnosis_result_carries_model_and_retrieval_flag(
        self,
        diagnosis_service_no_key: DiagnosisService,
        mock_retriever: MagicMock,
    ) -> None:
        """Retrieval-only results are flagged and carry no llm_model."""
        mock_retriever.search.return_value = []
        result = await diagnosis_service_no_key.diagnose(
            product_type="P", failed_test="t"
        )
        assert result["retrieval_only"] is True
        assert result["llm_model"] is None

    def test_diagnosis_request_to_query_text(self) -> None:
        """DiagnosisRequest.to_query_text combines all fields."""
        req = DiagnosisRequest(
            product_type="PROD-001",
            failed_test="i2c_test",
            error_code="ERR_001",
            log_snippet="timeout occurred",
        )
        text = req.to_query_text()
        assert "i2c_test" in text
        assert "ERR_001" in text
        assert "PROD-001" in text
        assert "timeout occurred" in text

    def test_diagnosis_request_to_query_text_minimal(self) -> None:
        """DiagnosisRequest.to_query_text with only failed_test."""
        req = DiagnosisRequest(
            product_type="",
            failed_test="basic_test",
        )
        text = req.to_query_text()
        assert "basic_test" in text

    def test_circuit_breaker_property(
        self,
        diagnosis_service: DiagnosisService,
    ) -> None:
        """circuit_breaker property returns the CircuitBreaker instance."""
        from ate_platform.common.circuit_breaker import CircuitBreaker

        breaker = diagnosis_service.circuit_breaker
        assert isinstance(breaker, CircuitBreaker)
        assert breaker._name == "llm-diagnosis-service"


# ── API endpoint tests ────────────────────────────────────────────────────


class TestDiagnoseAPI:
    """Tests for POST /api/v1/diagnose and POST /api/v1/diagnose/{id}/feedback."""

    @pytest.mark.asyncio
    async def test_diagnose_endpoint_success(
        self,
        diagnose_client: AsyncClient,
        mock_retriever: MagicMock,
        diagnosis_service: DiagnosisService,
    ) -> None:
        """POST /api/v1/diagnose returns structured diagnosis."""
        mock_retriever.search.return_value = _sample_retrieved_cases()

        # Mock the LLM
        diagnosis_service._ensure_initialized()
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = _sample_llm_response()
        mock_llm.ainvoke.return_value = mock_response
        diagnosis_service._llm = mock_llm

        response = await diagnose_client.post(
            "/api/v1/diagnose",
            json={
                "product_type": "COMM-DEV-001",
                "failed_test": "test_i2c_comm",
                "error_code": "ERR_I2C_TIMEOUT",
                "log_snippet": "TimeoutError: I2C read failed",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "diagnosis_id" in data
        assert data["root_cause"] == (
            "Missing pull-up resistor on I2C SDA line causes communication timeout"
        )
        assert data["confidence"] == pytest.approx(0.92)
        assert len(data["evidence_citations"]) == 2
        assert len(data["repair_steps"]) == 3
        assert len(data["retrieved_cases"]) == 2

    @pytest.mark.asyncio
    async def test_diagnose_endpoint_missing_failed_test(
        self,
        diagnose_client: AsyncClient,
    ) -> None:
        """POST /api/v1/diagnose without failed_test returns 422."""
        response = await diagnose_client.post(
            "/api/v1/diagnose",
            json={
                "product_type": "COMM-DEV-001",
                # failed_test missing - required field
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_diagnose_endpoint_missing_product_type(
        self,
        diagnose_client: AsyncClient,
    ) -> None:
        """POST /api/v1/diagnose without product_type returns 422."""
        response = await diagnose_client.post(
            "/api/v1/diagnose",
            json={
                "failed_test": "test_i2c",
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_diagnose_endpoint_empty_body(
        self,
        diagnose_client: AsyncClient,
    ) -> None:
        """POST /api/v1/diagnose with empty body returns 422."""
        response = await diagnose_client.post(
            "/api/v1/diagnose",
            json={},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_diagnose_endpoint_optional_fields(
        self,
        diagnose_client: AsyncClient,
        mock_retriever: MagicMock,
        diagnosis_service: DiagnosisService,
    ) -> None:
        """POST /api/v1/diagnose works with only required fields."""
        mock_retriever.search.return_value = []

        diagnosis_service._ensure_initialized()
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = (
            '{"root_cause": "no data", "confidence": 0.0, '
            '"evidence_citations": [], "repair_steps": []}'
        )
        mock_llm.ainvoke.return_value = mock_response
        diagnosis_service._llm = mock_llm

        response = await diagnose_client.post(
            "/api/v1/diagnose",
            json={
                "product_type": "PROD-001",
                "failed_test": "basic_test",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["diagnosis_id"]
        assert data["root_cause"] == "no data"

    @pytest.mark.asyncio
    async def test_feedback_endpoint_confirmed(
        self,
        diagnose_client: AsyncClient,
        diagnosis_service: DiagnosisService,
        mock_retriever: MagicMock,
    ) -> None:
        """POST /diagnose/{id}/feedback with 'confirmed' updates the row (200)."""
        # Persist a diagnosis first (retrieval-only: no LLM network call).
        diagnosis_service._api_key = ""
        mock_retriever.search.return_value = []
        created = await diagnose_client.post(
            "/api/v1/diagnose",
            json={"product_type": "P", "failed_test": "t"},
        )
        assert created.status_code == 200, created.text
        diagnosis_id = created.json()["diagnosis_id"]

        response = await diagnose_client.post(
            f"/api/v1/diagnose/{diagnosis_id}/feedback",
            json={"feedback": "confirmed"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["diagnosis_id"] == diagnosis_id
        assert data["feedback"] == "confirmed"
        assert data["correction"] == ""
        assert data["recorded"] is True

    @pytest.mark.asyncio
    async def test_feedback_endpoint_rejected_with_correction(
        self,
        diagnose_client: AsyncClient,
        diagnosis_service: DiagnosisService,
        mock_retriever: MagicMock,
    ) -> None:
        """POST feedback 'rejected' + correction on a persisted diagnosis."""
        diagnosis_service._api_key = ""
        mock_retriever.search.return_value = []
        created = await diagnose_client.post(
            "/api/v1/diagnose",
            json={"product_type": "P", "failed_test": "t"},
        )
        assert created.status_code == 200, created.text
        diagnosis_id = created.json()["diagnosis_id"]

        response = await diagnose_client.post(
            f"/api/v1/diagnose/{diagnosis_id}/feedback",
            json={
                "feedback": "rejected",
                "correction": "Actual cause: broken trace on PCB",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["feedback"] == "rejected"
        assert data["correction"] == "Actual cause: broken trace on PCB"

    @pytest.mark.asyncio
    async def test_feedback_endpoint_unknown_diagnosis_returns_404(
        self,
        diagnose_client: AsyncClient,
    ) -> None:
        """Feedback for an id that was never persisted returns 404."""
        response = await diagnose_client.post(
            "/api/v1/diagnose/never-persisted/feedback",
            json={"feedback": "confirmed"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_feedback_endpoint_invalid_feedback(
        self,
        diagnose_client: AsyncClient,
    ) -> None:
        """POST /api/v1/diagnose/{id}/feedback with invalid feedback returns 400."""
        response = await diagnose_client.post(
            "/api/v1/diagnose/diag-789/feedback",
            json={"feedback": "maybe"},
        )
        assert response.status_code == 400
        assert "must be 'confirmed' or 'rejected'" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_feedback_endpoint_missing_feedback(
        self,
        diagnose_client: AsyncClient,
    ) -> None:
        """POST /api/v1/diagnose/{id}/feedback without feedback returns 422."""
        response = await diagnose_client.post(
            "/api/v1/diagnose/diag-789/feedback",
            json={"correction": "some text"},
        )
        assert response.status_code == 422


# ── Router registration test ──────────────────────────────────────────────


class TestDiagnoseRouterRegistration:
    """Verify the diagnose router is properly registered."""

    @pytest.mark.asyncio
    async def test_router_registered(
        self,
        diagnose_client: AsyncClient,
        mock_retriever: MagicMock,
        diagnosis_service: DiagnosisService,
    ) -> None:
        """The /api/v1/diagnose route is accessible (not 404)."""
        mock_retriever.search.return_value = []
        diagnosis_service._ensure_initialized()
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = (
            '{"root_cause": "test", "confidence": 0.5, '
            '"evidence_citations": [], "repair_steps": []}'
        )
        mock_llm.ainvoke.return_value = mock_response
        diagnosis_service._llm = mock_llm

        response = await diagnose_client.post(
            "/api/v1/diagnose",
            json={"product_type": "P", "failed_test": "t"},
        )
        # 200 means the route exists and is wired correctly
        assert response.status_code == 200
