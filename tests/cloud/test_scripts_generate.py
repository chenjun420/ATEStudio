"""API integration tests for script generation endpoints.

Tests the POST /api/v1/scripts/generate and POST /api/v1/scripts/refine
endpoints with mocked LLM (no real OpenAI API key required).

The autouse ``_dev_mode_bypass`` fixture from conftest.py bypasses auth.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ate_cloud.api.v1.scripts_generate import _get_script_generator
from ate_cloud.services.script_generator import LLMScriptGenerator

# ── Fixtures ──────────────────────────────────────────────────────────────


def _mock_llm_response() -> str:
    """Return a mock LLM response (valid Python with markdown fence)."""
    return (
        "```python\n"
        "import time\n"
        "from ate_platform.executor.context_proxy import ContextProxy, measure\n"
        "\n"
        "@measure('voltage_5v', 'i2c_status')\n"
        "def test_power_and_i2c(proxy: ContextProxy) -> None:\n"
        "    psu = proxy.get_instrument('psu')\n"
        "    psu.set_voltage(1, 5.0)\n"
        "    psu.output_on(1)\n"
        "    time.sleep(0.5)\n"
        "    dmm = proxy.get_instrument('dmm')\n"
        "    voltage = dmm.measure_voltage()\n"
        "    proxy['voltage_5v'] = voltage\n"
        "    i2c = proxy.get_instrument('i2c')\n"
        "    response = i2c.query('*IDN?')\n"
        "    proxy['i2c_status'] = 'PASS' if response else 'FAIL'\n"
        "```"
    )


@pytest.fixture
def mock_chat_openai() -> MagicMock:
    """Patch langchain_openai.ChatOpenAI with a controllable mock."""
    instance = MagicMock()
    instance.ainvoke = AsyncMock()
    mock_response = MagicMock()
    mock_response.content = _mock_llm_response()
    instance.ainvoke.return_value = mock_response

    with patch("langchain_openai.ChatOpenAI", return_value=instance):
        yield instance


@pytest.fixture
def script_generator(mock_chat_openai: MagicMock) -> LLMScriptGenerator:
    """Create an LLMScriptGenerator with a mocked ChatOpenAI backend."""
    return LLMScriptGenerator(api_key="test-key", model="gpt-4o-mini")


@pytest.fixture
async def app_with_scripts(
    script_generator: LLMScriptGenerator,
) -> AsyncGenerator[FastAPI, None]:
    """Create a FastAPI app with script_generator and mocked dependencies."""
    from ate_cloud.main import create_app

    app = create_app()
    app.dependency_overrides[_get_script_generator] = lambda: script_generator
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def client(
    app_with_scripts: FastAPI,
) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client targeting the scripts generate API."""
    transport = ASGITransport(app=app_with_scripts)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Generate Endpoint Tests ───────────────────────────────────────────────


class TestGenerateEndpoint:
    """Tests for POST /api/v1/scripts/generate."""

    @pytest.mark.asyncio
    async def test_generate_success(self, client: AsyncClient) -> None:
        """Given valid request, should return 200 with generated code."""
        response = await client.post(
            "/api/v1/scripts/generate",
            json={
                "spec_text": "power on 5V rail, check I2C communication",
                "product_type": "COMM-DEV-001",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "code" in data
        assert "confidence" in data
        assert "validation_errors" in data
        assert "suggestions" in data
        assert "def test_" in data["code"]
        assert data["confidence"] > 0.0

    @pytest.mark.asyncio
    async def test_generate_with_context(self, client: AsyncClient) -> None:
        """Given request with context, should return 200."""
        response = await client.post(
            "/api/v1/scripts/generate",
            json={
                "spec_text": "test voltage on 3.3V rail",
                "product_type": "COMM-DEV-001",
                "context": {
                    "instrument_dmm": "keithley-2000",
                    "instrument_psu": "keysight-e3631",
                },
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["code"]) > 0

    @pytest.mark.asyncio
    async def test_generate_empty_spec_rejected(self, client: AsyncClient) -> None:
        """Given empty spec_text, should return 422 (validation error)."""
        response = await client.post(
            "/api/v1/scripts/generate",
            json={
                "spec_text": "",
                "product_type": "COMM-DEV-001",
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_generate_missing_product_type(
        self, client: AsyncClient
    ) -> None:
        """Given missing product_type, should return 422."""
        response = await client.post(
            "/api/v1/scripts/generate",
            json={
                "spec_text": "test voltage",
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_generate_missing_spec_text(
        self, client: AsyncClient
    ) -> None:
        """Given missing spec_text, should return 422."""
        response = await client.post(
            "/api/v1/scripts/generate",
            json={
                "product_type": "COMM-DEV-001",
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_generate_validation_errors_returned(
        self, client: AsyncClient
    ) -> None:
        """Given LLM returns safe code, validation_errors should be empty."""
        response = await client.post(
            "/api/v1/scripts/generate",
            json={
                "spec_text": "test power and I2C",
                "product_type": "COMM-DEV-001",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["validation_errors"] == []

    @pytest.mark.asyncio
    async def test_generate_confidence_in_range(
        self, client: AsyncClient
    ) -> None:
        """Given successful generation, confidence should be 0.0-1.0."""
        response = await client.post(
            "/api/v1/scripts/generate",
            json={
                "spec_text": "test power",
                "product_type": "COMM-DEV-001",
            },
        )
        data = response.json()
        assert 0.0 <= data["confidence"] <= 1.0

    @pytest.mark.asyncio
    async def test_generate_suggestions_returned(
        self, client: AsyncClient
    ) -> None:
        """Given successful generation, suggestions should be a list."""
        response = await client.post(
            "/api/v1/scripts/generate",
            json={
                "spec_text": "test power",
                "product_type": "COMM-DEV-001",
            },
        )
        data = response.json()
        assert isinstance(data["suggestions"], list)


# ── Refine Endpoint Tests ─────────────────────────────────────────────────


class TestRefineEndpoint:
    """Tests for POST /api/v1/scripts/refine."""

    @pytest.mark.asyncio
    async def test_refine_success(self, client: AsyncClient) -> None:
        """Given valid refine request, should return 200 with refined code."""
        response = await client.post(
            "/api/v1/scripts/refine",
            json={
                "code": "def test():\n    pass\n",
                "feedback": "add retry logic for I2C communication",
                "product_type": "COMM-DEV-001",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "code" in data
        assert "confidence" in data
        assert len(data["code"]) > 0

    @pytest.mark.asyncio
    async def test_refine_empty_code_rejected(
        self, client: AsyncClient
    ) -> None:
        """Given empty code, should return 422."""
        response = await client.post(
            "/api/v1/scripts/refine",
            json={
                "code": "",
                "feedback": "add retry",
                "product_type": "COMM-DEV-001",
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_refine_empty_feedback_rejected(
        self, client: AsyncClient
    ) -> None:
        """Given empty feedback, should return 422."""
        response = await client.post(
            "/api/v1/scripts/refine",
            json={
                "code": "def test(): pass",
                "feedback": "",
                "product_type": "COMM-DEV-001",
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_refine_missing_product_type(
        self, client: AsyncClient
    ) -> None:
        """Given missing product_type, should return 422."""
        response = await client.post(
            "/api/v1/scripts/refine",
            json={
                "code": "def test(): pass",
                "feedback": "add retry",
            },
        )
        assert response.status_code == 422


# ── Error Handling Tests ──────────────────────────────────────────────────


class TestErrorHandling:
    """Tests for error handling in the API endpoints."""

    @pytest.mark.asyncio
    async def test_generate_no_api_key_returns_503(
        self, app_with_scripts: FastAPI
    ) -> None:
        """Given no API key, should return 503."""
        from ate_cloud.services.script_generator import LLMScriptGenerator

        no_key_gen = LLMScriptGenerator(api_key="")
        app_with_scripts.dependency_overrides[_get_script_generator] = lambda: no_key_gen

        transport = ASGITransport(app=app_with_scripts)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/api/v1/scripts/generate",
                json={
                    "spec_text": "test spec",
                    "product_type": "COMM-DEV-001",
                },
            )
        assert response.status_code == 503
        assert "API key" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_generate_circuit_breaker_open_returns_503(
        self, app_with_scripts: FastAPI
    ) -> None:
        """Given circuit breaker is OPEN, should return 503."""
        from ate_platform.common.circuit_breaker import CircuitState

        gen = LLMScriptGenerator(api_key="test-key")
        gen._breaker._state = CircuitState.OPEN
        gen._breaker._last_failure_time = float("inf")
        gen._ensure_initialized()
        app_with_scripts.dependency_overrides[_get_script_generator] = lambda: gen

        transport = ASGITransport(app=app_with_scripts)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/api/v1/scripts/generate",
                json={
                    "spec_text": "test spec",
                    "product_type": "COMM-DEV-001",
                },
            )
        assert response.status_code == 503
        assert "circuit breaker" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_refine_no_api_key_returns_503(
        self, app_with_scripts: FastAPI
    ) -> None:
        """Given no API key on refine, should return 503."""
        from ate_cloud.services.script_generator import LLMScriptGenerator

        no_key_gen = LLMScriptGenerator(api_key="")
        app_with_scripts.dependency_overrides[_get_script_generator] = lambda: no_key_gen

        transport = ASGITransport(app=app_with_scripts)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/api/v1/scripts/refine",
                json={
                    "code": "def test(): pass",
                    "feedback": "add retry",
                    "product_type": "COMM-DEV-001",
                },
            )
        assert response.status_code == 503


# ── Spec Validation Example Tests ─────────────────────────────────────────


class TestSpecExample:
    """Test the specific verification example from the task spec."""

    @pytest.mark.asyncio
    async def test_power_5v_i2c_spec_generates_valid_script(
        self, client: AsyncClient
    ) -> None:
        """Given 'power on 5V rail, check I2C communication' spec,
        should generate a valid script with voltage and I2C test."""
        response = await client.post(
            "/api/v1/scripts/generate",
            json={
                "spec_text": "上电测试 5V 轨，检查 I2C 通信",
                "product_type": "COMM-DEV-001",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "def test_" in data["code"]
        assert data["confidence"] > 0.0
        # The mock LLM response includes voltage and I2C checks
        assert "voltage" in data["code"].lower()
        assert "i2c" in data["code"].lower()
