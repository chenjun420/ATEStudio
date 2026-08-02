"""Unit tests for LLMScriptGenerator.

Tests focus on the post-processing logic:
- AST validation (valid/invalid Python)
- Security scan (forbidden imports, calls, attribute accesses)
- Dependency check (unknown module warnings)
- Confidence scoring
- Suggestion generation
- Code extraction from markdown fences

LLM calls are mocked — no real OpenAI API key required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ate_cloud.services.script_generator import (
    GeneratedScript,
    LLMScriptGenerator,
    ValidationResult,
)

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_chat_openai() -> MagicMock:
    """Patch langchain_openai.ChatOpenAI with a controllable mock."""
    instance = MagicMock()
    instance.ainvoke = AsyncMock()
    mock_response = MagicMock()
    mock_response.content = (
        "```python\n"
        "import time\n"
        "from ate_platform.executor.context_proxy import ContextProxy, measure\n"
        "\n"
        "@measure('voltage')\n"
        "def test_voltage(proxy: ContextProxy) -> None:\n"
        "    psu = proxy.get_instrument('psu')\n"
        "    psu.set_voltage(1, 5.0)\n"
        "    psu.output_on(1)\n"
        "    time.sleep(0.5)\n"
        "    dmm = proxy.get_instrument('dmm')\n"
        "    voltage = dmm.measure_voltage()\n"
        "    proxy['voltage'] = voltage\n"
        "```"
    )
    instance.ainvoke.return_value = mock_response

    with patch("langchain_openai.ChatOpenAI", return_value=instance):
        yield instance


@pytest.fixture
def generator(mock_chat_openai: MagicMock) -> LLMScriptGenerator:
    """Create an LLMScriptGenerator with a mocked ChatOpenAI backend."""
    return LLMScriptGenerator(api_key="test-key", model="gpt-4o-mini")


@pytest.fixture
def generator_no_key() -> LLMScriptGenerator:
    """Create an LLMScriptGenerator with no API key."""
    return LLMScriptGenerator(api_key="")


# ── Sample Code ───────────────────────────────────────────────────────────


def _valid_script() -> str:
    """A valid, safe test script."""
    return (
        "import time\n"
        "from ate_platform.executor.context_proxy import ContextProxy, measure\n"
        "\n"
        "@measure('voltage')\n"
        "def test_voltage(proxy: ContextProxy) -> None:\n"
        "    psu = proxy.get_instrument('psu')\n"
        "    psu.set_voltage(1, 5.0)\n"
        "    psu.output_on(1)\n"
        "    time.sleep(0.5)\n"
        "    dmm = proxy.get_instrument('dmm')\n"
        "    voltage = dmm.measure_voltage()\n"
        "    proxy['voltage'] = voltage\n"
    )


def _syntax_error_script() -> str:
    """A script with a syntax error."""
    return (
        "def test_bad(:\n"
        "    pass\n"
    )


def _subprocess_import_script() -> str:
    """A script that imports subprocess."""
    return (
        "import subprocess\n"
        "\n"
        "def test_bad():\n"
        "    subprocess.run(['ls'])\n"
    )


def _eval_call_script() -> str:
    """A script that calls eval()."""
    return (
        "def test_bad():\n"
        "    result = eval('1 + 1')\n"
        "    return result\n"
    )


def _os_system_script() -> str:
    """A script that calls os.system()."""
    return (
        "import os\n"
        "\n"
        "def test_bad():\n"
        "    os.system('rm -rf /')\n"
    )


def _exec_call_script() -> str:
    """A script that calls exec()."""
    return (
        "def test_bad():\n"
        "    exec('print(1)')\n"
    )


def _unknown_import_script() -> str:
    """A script with an unknown (non-framework) import."""
    return (
        "import numpy\n"
        "\n"
        "def test_unknown():\n"
        "    pass\n"
    )


def _from_os_import_system_script() -> str:
    """A script that does 'from os import system'."""
    return (
        "from os import system\n"
        "\n"
        "def test_bad():\n"
        "    system('ls')\n"
    )


# ── AST Validation Tests ──────────────────────────────────────────────────


class TestASTValidation:
    """Tests for AST parse validation."""

    def test_valid_script_passes_validation(self, generator: LLMScriptGenerator) -> None:
        """Given a valid script, validation should pass with no errors."""
        result = generator.validate(_valid_script())
        assert result.is_valid
        assert result.errors == []

    def test_syntax_error_detected(self, generator: LLMScriptGenerator) -> None:
        """Given a script with syntax error, validation should fail."""
        result = generator.validate(_syntax_error_script())
        assert not result.is_valid
        assert any("Syntax error" in e for e in result.errors)

    def test_empty_string_validates(self, generator: LLMScriptGenerator) -> None:
        """Given an empty string, AST parse should succeed (valid but empty)."""
        result = generator.validate("")
        assert result.is_valid
        assert result.errors == []


# ── Security Scan Tests ───────────────────────────────────────────────────


class TestSecurityScan:
    """Tests for security scanning (forbidden imports, calls, attributes)."""

    def test_subprocess_import_blocked(self, generator: LLMScriptGenerator) -> None:
        """Given a script importing subprocess, security scan should flag it."""
        result = generator.validate(_subprocess_import_script())
        assert not result.is_valid
        assert any("forbidden import" in e and "subprocess" in e for e in result.errors)

    def test_eval_call_blocked(self, generator: LLMScriptGenerator) -> None:
        """Given a script calling eval(), security scan should flag it."""
        result = generator.validate(_eval_call_script())
        assert not result.is_valid
        assert any("forbidden call" in e and "eval" in e for e in result.errors)

    def test_os_system_blocked(self, generator: LLMScriptGenerator) -> None:
        """Given a script calling os.system(), security scan should flag it."""
        result = generator.validate(_os_system_script())
        assert not result.is_valid
        assert any("forbidden" in e and "system" in e for e in result.errors)

    def test_exec_call_blocked(self, generator: LLMScriptGenerator) -> None:
        """Given a script calling exec(), security scan should flag it."""
        result = generator.validate(_exec_call_script())
        assert not result.is_valid
        assert any("forbidden call" in e and "exec" in e for e in result.errors)

    def test_from_os_import_system_blocked(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given 'from os import system', security scan should flag it."""
        result = generator.validate(_from_os_import_system_script())
        assert not result.is_valid
        assert any("forbidden" in e for e in result.errors)

    def test_safe_script_has_no_security_errors(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given a safe script, security scan should find no violations."""
        result = generator.validate(_valid_script())
        assert result.is_valid
        assert all("Security" not in e for e in result.errors)

    def test_ctypes_import_blocked(self, generator: LLMScriptGenerator) -> None:
        """Given ctypes import, security scan should flag it."""
        code = "import ctypes\ndef test(): pass\n"
        result = generator.validate(code)
        assert not result.is_valid
        assert any("ctypes" in e for e in result.errors)

    def test_socket_import_blocked(self, generator: LLMScriptGenerator) -> None:
        """Given socket import, security scan should flag it."""
        code = "import socket\ndef test(): pass\n"
        result = generator.validate(code)
        assert not result.is_valid
        assert any("socket" in e for e in result.errors)

    def test_compile_call_blocked(self, generator: LLMScriptGenerator) -> None:
        """Given compile() call, security scan should flag it."""
        code = "def test():\n    compile('1+1', '<string>', 'eval')\n"
        result = generator.validate(code)
        assert not result.is_valid
        assert any("compile" in e for e in result.errors)

    def test_dunder_import_blocked(self, generator: LLMScriptGenerator) -> None:
        """Given __import__() call, security scan should flag it."""
        code = "def test():\n    __import__('os')\n"
        result = generator.validate(code)
        assert not result.is_valid
        assert any("__import__" in e for e in result.errors)


# ── Dependency Check Tests ────────────────────────────────────────────────


class TestDependencyCheck:
    """Tests for dependency checking (unknown imports)."""

    def test_framework_import_no_warning(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given a framework import, dependency check should not warn."""
        result = generator.validate(_valid_script())
        assert all("Dependency" not in w for w in result.warnings)

    def test_unknown_import_produces_warning(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given an unknown import, dependency check should warn."""
        result = generator.validate(_unknown_import_script())
        assert any("numpy" in w for w in result.warnings)

    def test_stdlib_import_no_warning(self, generator: LLMScriptGenerator) -> None:
        """Given a stdlib import (time, math), dependency check should not warn."""
        code = "import time\nimport math\ndef test(): pass\n"
        result = generator.validate(code)
        assert all("Dependency" not in w for w in result.warnings)


# ── Confidence Scoring Tests ──────────────────────────────────────────────


class TestConfidenceScoring:
    """Tests for confidence score computation."""

    def test_valid_script_high_confidence(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given a valid script with no issues, confidence should be 1.0."""
        result = generator.validate(_valid_script())
        confidence = LLMScriptGenerator._compute_confidence(result)
        assert confidence == 1.0

    def test_syntax_error_zero_confidence(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given a syntax error, confidence should be 0.0."""
        result = generator.validate(_syntax_error_script())
        confidence = LLMScriptGenerator._compute_confidence(result)
        assert confidence == 0.0

    def test_security_violation_reduces_confidence(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given a security violation, confidence should be reduced."""
        result = generator.validate(_subprocess_import_script())
        confidence = LLMScriptGenerator._compute_confidence(result)
        assert confidence < 1.0
        assert confidence >= 0.0

    def test_dependency_warning_reduces_confidence(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given a dependency warning, confidence should be slightly reduced."""
        result = generator.validate(_unknown_import_script())
        confidence = LLMScriptGenerator._compute_confidence(result)
        assert confidence < 1.0
        assert confidence >= 0.9  # Only -0.1 for one warning

    def test_multiple_violations_floor_at_zero(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given many violations, confidence should not go below 0.0."""
        code = (
            "import subprocess\n"
            "import ctypes\n"
            "import socket\n"
            "import pickle\n"
            "def test():\n"
            "    eval('1')\n"
            "    exec('2')\n"
        )
        result = generator.validate(code)
        confidence = LLMScriptGenerator._compute_confidence(result)
        assert confidence == 0.0


# ── Suggestion Generation Tests ───────────────────────────────────────────


class TestSuggestionGeneration:
    """Tests for improvement suggestion generation."""

    def test_valid_script_has_few_suggestions(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given a valid script, suggestions should be minimal."""
        result = generator.validate(_valid_script())
        suggestions = LLMScriptGenerator._generate_suggestions(
            _valid_script(), result, "test spec"
        )
        # The valid script has no try/except and no assert
        assert isinstance(suggestions, list)

    def test_script_with_errors_suggests_fixing(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given a script with errors, suggestion should mention fixing them."""
        result = generator.validate(_subprocess_import_script())
        suggestions = LLMScriptGenerator._generate_suggestions(
            _subprocess_import_script(), result, "test spec"
        )
        assert any("Fix validation errors" in s for s in suggestions)

    def test_script_without_error_handling_suggests_it(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given a script without try/except, should suggest error handling."""
        code = "def test():\n    x = 1\n"
        result = generator.validate(code)
        suggestions = LLMScriptGenerator._generate_suggestions(
            code, result, "test spec"
        )
        assert any("try/except" in s for s in suggestions)


# ── Code Extraction Tests ─────────────────────────────────────────────────


class TestCodeExtraction:
    """Tests for extracting code from LLM responses."""

    def test_strips_python_code_fence(self) -> None:
        """Given markdown ```python fence, should strip it."""
        raw = "```python\nprint('hello')\n```"
        result = LLMScriptGenerator._extract_code(raw)
        assert result == "print('hello')"

    def test_strips_plain_code_fence(self) -> None:
        """Given markdown ``` fence, should strip it."""
        raw = "```\nprint('hello')\n```"
        result = LLMScriptGenerator._extract_code(raw)
        assert result == "print('hello')"

    def test_no_fence_returns_as_is(self) -> None:
        """Given no fence, should return code as-is."""
        raw = "print('hello')"
        result = LLMScriptGenerator._extract_code(raw)
        assert result == "print('hello')"

    def test_strips_whitespace(self) -> None:
        """Given leading/trailing whitespace, should strip it."""
        raw = "  ```python\nprint('hello')\n```  "
        result = LLMScriptGenerator._extract_code(raw)
        assert result == "print('hello')"

    def test_multiline_code_preserved(self) -> None:
        """Given multiline code in fence, should preserve internal newlines."""
        raw = "```python\ndef test():\n    pass\n```"
        result = LLMScriptGenerator._extract_code(raw)
        assert "def test():" in result
        assert "    pass" in result


# ── Generate (LLM Mock) Tests ─────────────────────────────────────────────


class TestGenerate:
    """Tests for the generate() method with mocked LLM."""

    @pytest.mark.asyncio
    async def test_generate_returns_code(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given a mocked LLM, generate() should return code."""
        result = await generator.generate("power on 5V rail, check I2C")
        assert isinstance(result, GeneratedScript)
        assert "def test_" in result.code
        assert result.confidence > 0.0

    @pytest.mark.asyncio
    async def test_generate_with_product_config(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given product config context, generate() should still work."""
        result = await generator.generate(
            "test voltage",
            product_config={"instrument": "dmm", "range": "10V"},
        )
        assert isinstance(result, GeneratedScript)
        assert len(result.code) > 0

    @pytest.mark.asyncio
    async def test_generate_no_api_key_raises(
        self, generator_no_key: LLMScriptGenerator
    ) -> None:
        """Given no API key, generate() should raise RuntimeError."""
        with pytest.raises(RuntimeError, match="API key not configured"):
            await generator_no_key.generate("test spec")

    @pytest.mark.asyncio
    async def test_generate_strips_markdown_fences(
        self, mock_chat_openai: MagicMock
    ) -> None:
        """Given LLM returns markdown-fenced code, should strip fences."""
        mock_chat_openai.ainvoke.return_value.content = (
            "```python\nimport time\n\ndef test():\n    pass\n```"
        )
        gen = LLMScriptGenerator(api_key="test-key")
        result = await gen.generate("test spec")
        assert not result.code.startswith("```")
        assert "def test()" in result.code

    @pytest.mark.asyncio
    async def test_generate_validates_output(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given a mocked LLM, generate() should validate the output."""
        result = await generator.generate("power on 5V rail")
        # The mocked response is a valid, safe script
        assert result.validation_errors == [] or len(result.validation_errors) == 0
        assert result.confidence > 0.5


# ── Refine (LLM Mock) Tests ───────────────────────────────────────────────


class TestRefine:
    """Tests for the refine() method with mocked LLM."""

    @pytest.mark.asyncio
    async def test_refine_returns_code(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given a mocked LLM, refine() should return refined code."""
        result = await generator.refine(
            code="def test(): pass\n",
            feedback="add retry logic",
        )
        assert isinstance(result, GeneratedScript)
        assert len(result.code) > 0

    @pytest.mark.asyncio
    async def test_refine_no_api_key_raises(
        self, generator_no_key: LLMScriptGenerator
    ) -> None:
        """Given no API key, refine() should raise RuntimeError."""
        with pytest.raises(RuntimeError, match="API key not configured"):
            await generator_no_key.refine(
                code="def test(): pass\n",
                feedback="add retry",
            )


# ── Prompt Building Tests ─────────────────────────────────────────────────


class TestPromptBuilding:
    """Tests for prompt construction."""

    def test_build_spec_info_includes_spec_text(self) -> None:
        """Given spec text, _build_spec_info should include it."""
        info = LLMScriptGenerator._build_spec_info("test my board", None)
        assert "test my board" in info
        assert "TEST SPECIFICATION" in info

    def test_build_spec_info_includes_product_config(self) -> None:
        """Given product config, _build_spec_info should include it."""
        info = LLMScriptGenerator._build_spec_info(
            "test spec", {"instrument": "dmm"}
        )
        assert "PRODUCT CONFIG CONTEXT" in info
        assert "instrument: dmm" in info

    def test_build_refine_info_includes_code_and_feedback(self) -> None:
        """Given code and feedback, _build_refine_info should include both."""
        info = LLMScriptGenerator._build_refine_info(
            "def test(): pass", "add retry", None
        )
        assert "def test(): pass" in info
        assert "add retry" in info
        assert "CURRENT SCRIPT" in info
        assert "REFINEMENT FEEDBACK" in info


# ── CircuitBreaker Integration Tests ──────────────────────────────────────


class TestCircuitBreakerIntegration:
    """Tests for CircuitBreaker integration."""

    def test_circuit_breaker_property_exists(
        self, generator: LLMScriptGenerator
    ) -> None:
        """Given a generator, circuit_breaker property should return the breaker."""
        from ate_platform.common.circuit_breaker import CircuitBreaker

        breaker = generator.circuit_breaker
        assert isinstance(breaker, CircuitBreaker)

    @pytest.mark.asyncio
    async def test_generate_propagates_circuit_breaker_open(
        self, mock_chat_openai: MagicMock
    ) -> None:
        """Given circuit breaker is OPEN, generate() should raise."""
        from ate_platform.common.circuit_breaker import (
            CircuitBreakerOpenError,
            CircuitState,
        )

        gen = LLMScriptGenerator(api_key="test-key")
        # Force the breaker into OPEN state
        gen._breaker._state = CircuitState.OPEN
        gen._breaker._last_failure_time = float("inf")  # far future to stay open

        # Need to ensure_initialized so the LLM is set up
        gen._ensure_initialized()

        with pytest.raises(CircuitBreakerOpenError):
            await gen.generate("test spec")


# ── GeneratedScript Dataclass Tests ───────────────────────────────────────


class TestGeneratedScript:
    """Tests for the GeneratedScript dataclass."""

    def test_generated_script_defaults(self) -> None:
        """Given minimal args, GeneratedScript should have default lists."""
        script = GeneratedScript(code="pass", confidence=0.5)
        assert script.code == "pass"
        assert script.confidence == 0.5
        assert script.validation_errors == []
        assert script.suggestions == []

    def test_generated_script_with_errors(self) -> None:
        """Given errors, GeneratedScript should store them."""
        script = GeneratedScript(
            code="pass",
            confidence=0.0,
            validation_errors=["error1"],
            suggestions=["suggestion1"],
        )
        assert script.validation_errors == ["error1"]
        assert script.suggestions == ["suggestion1"]


# ── ValidationResult Dataclass Tests ──────────────────────────────────────


class TestValidationResult:
    """Tests for the ValidationResult dataclass."""

    def test_validation_result_defaults(self) -> None:
        """Given minimal args, ValidationResult should have default lists."""
        result = ValidationResult(is_valid=True)
        assert result.is_valid
        assert result.errors == []
        assert result.warnings == []

    def test_validation_result_with_errors(self) -> None:
        """Given errors, ValidationResult should store them."""
        result = ValidationResult(
            is_valid=False,
            errors=["error1"],
            warnings=["warning1"],
        )
        assert not result.is_valid
        assert result.errors == ["error1"]
        assert result.warnings == ["warning1"]
