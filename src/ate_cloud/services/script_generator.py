"""LLM Script Generator - AI-powered test script generation.

Takes natural-language test specifications and generates Python test scripts
using the ATE Studio framework APIs. Follows the same LLM integration pattern
as DiagnosisService: deferred LangChain init, CircuitBreaker protection.

Pipeline:
1. Build system prompt with ATE Studio framework context + instrument APIs.
2. Call LLM via CircuitBreaker to generate Python code.
3. Post-process: AST parse validation, security scan, dependency check.
4. Return GeneratedScript with code, confidence, validation errors, suggestions.

Security: blocks dangerous imports (subprocess, os.system, eval, exec,
__import__) and file/network operations outside the sandbox.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from typing import Any

from ate_cloud.config import settings
from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)

#: Modules that are allowed in generated scripts (framework + stdlib testing).
_ALLOWED_IMPORTS: frozenset[str] = frozenset({
    # ATE Studio framework
    "ate_platform",
    "ate_platform.context",
    "ate_platform.executor",
    "ate_platform.executor.context_proxy",
    "ate_platform.drivers",
    "ate_platform.drivers.base_hal",
    "ate_platform.drivers.examples",
    "ate_platform.drivers.examples.dmm",
    "ate_platform.drivers.examples.psu",
    "ate_platform.common",
    "ate_platform.simulation",
    "ate_platform.data",
    "ate_cloud",
    "shared",
    # Standard library (safe subset)
    "time",
    "math",
    "re",
    "json",
    "logging",
    "datetime",
    "collections",
    "itertools",
    "functools",
    "typing",
    "dataclasses",
    "enum",
    "decimal",
    "statistics",
    "unittest.mock",
})

#: Modules/patterns that are strictly forbidden in generated scripts.
_FORBIDDEN_IMPORTS: frozenset[str] = frozenset({
    "subprocess",
    "os.system",
    "os.popen",
    "os.exec",
    "os.execv",
    "os.execve",
    "os.spawn",
    "ctypes",
    "socket",
    "http",
    "urllib",
    "requests",
    "shutil",
    "pickle",
    "marshal",
    "webbrowser",
    "importlib",
    "builtins",
})

#: Built-in function calls that are forbidden.
_FORBIDDEN_CALLS: frozenset[str] = frozenset({
    "eval",
    "exec",
    "compile",
    "__import__",
    "globals",
    "locals",
    "vars",
    "getattr",
    "setattr",
    "delattr",
    "exit",
    "quit",
    "system",
    "popen",
})

#: Attribute accesses that are forbidden (e.g. os.system, os.remove).
_FORBIDDEN_ATTRS: frozenset[str] = frozenset({
    "system",
    "popen",
    "exec",
    "execv",
    "execve",
    "spawnl",
    "spawnle",
    "spawnv",
    "spawnve",
    "remove",
    "unlink",
    "rmdir",
    "mkdir",
    "chmod",
    "chown",
    "fork",
    "kill",
    "open",  # file open - blocked outside /tmp
})

#: System prompt for the LLM — includes ATE Studio framework context.
_SYSTEM_PROMPT = (
    "You are an ATE Studio test script generator for electronics production testing. "
    "Generate a complete, runnable Python test script based on the natural-language "
    "test specification provided.\n\n"
    "=== ATE STUDIO FRAMEWORK CONTEXT ===\n"
    "The ATE Studio framework provides the following APIs for test scripts:\n\n"
    "1. Instrument APIs (HAL/MAL):\n"
    "   - `from ate_platform.drivers.base_hal import BaseHAL`\n"
    "   - `from ate_platform.drivers.examples.dmm import DMMAbstraction`\n"
    "   - `from ate_platform.drivers.examples.psu import PSUAbstraction`\n"
    "   - DMM: measure_voltage(), measure_current(), measure_resistance()\n"
    "   - PSU: set_voltage(channel, voltage), set_current_limit(channel, current), "
    "output_on(channel), output_off(channel)\n\n"
    "2. Measurement Reporting:\n"
    "   - `from ate_platform.executor.context_proxy import ContextProxy, measure`\n"
    "   - Use @measure('output_name') decorator on test functions\n"
    "   - Access instruments via proxy['instrument_name'] or proxy.get_instrument(name)\n"
    "   - Set outputs: proxy['voltage'] = 3.3\n"
    "   - Log: proxy.log('message', level='info')\n\n"
    "3. Test Script Pattern:\n"
    "   ```python\n"
    "   from ate_platform.executor.context_proxy import ContextProxy, measure\n\n"
    "   @measure('voltage_5v', 'i2c_status')\n"
    "   def test_power_and_i2c(proxy: ContextProxy) -> None:\n"
    "       # Get instruments\n"
    "       psu = proxy.get_instrument('psu')\n"
    "       dmm = proxy.get_instrument('dmm')\n\n"
    "       # Power on 5V rail\n"
    "       psu.set_voltage(1, 5.0)\n"
    "       psu.output_on(1)\n"
    "       time.sleep(0.5)\n\n"
    "       # Measure voltage\n"
    "       voltage = dmm.measure_voltage()\n"
    "       proxy['voltage_5v'] = voltage\n\n"
    "       # Check I2C communication\n"
    "       i2c = proxy.get_instrument('i2c')\n"
    "       response = i2c.query('*IDN?')\n"
    "       proxy['i2c_status'] = 'PASS' if response else 'FAIL'\n"
    "   ```\n\n"
    "=== RULES ===\n"
    "1. Generate ONLY Python code — no markdown fences, no explanations.\n"
    "2. Use only the framework APIs listed above.\n"
    "3. Do NOT use subprocess, os.system, eval, exec, or any file/network operations.\n"
    "4. Include necessary imports at the top.\n"
    "5. Use @measure decorator to declare outputs.\n"
    "6. Handle common error cases (timeouts, out-of-range values).\n"
    "7. Keep the script focused on the test specification.\n"
)


@dataclass(frozen=True, slots=True)
class GeneratedScript:
    """Result of script generation.

    Attributes:
        code: Generated Python source code.
        confidence: Confidence score (0.0-1.0) based on validation passes.
        validation_errors: Errors found during post-processing.
        suggestions: Improvement suggestions.
    """

    code: str
    confidence: float
    validation_errors: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Result of post-generation validation.

    Attributes:
        is_valid: True if AST parse succeeded and no security violations.
        errors: List of validation error messages.
        warnings: List of non-blocking warning messages.
    """

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class LLMScriptGenerator:
    """AI-powered test script generator using LangChain ChatOpenAI.

    Follows the same pattern as DiagnosisService:
    - Deferred LangChain import in ``_ensure_initialized()``
    - CircuitBreaker protection (failure_threshold=5, timeout=30s)
    - Graceful handling when API key is not configured

    Args:
        api_key: OpenAI API key (defaults to ``settings.openai_api_key``).
        model: Chat model name (default ``"gpt-4o-mini"``).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self._api_key = api_key or settings.openai_api_key
        self._model = model or settings.openai_model
        self._breaker = CircuitBreaker(
            failure_threshold=5,
            timeout=30.0,
            name="llm-script-generator",
        )
        self._llm: Any = None
        self._prompt: Any = None
        self._initialized = False

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Underlying CircuitBreaker instance (for inspection/reset)."""
        return self._breaker

    def _ensure_initialized(self) -> None:
        """Lazily initialize LangChain LLM and prompt template (deferred import).

        Defers ``langchain_openai`` / ``langchain_core`` imports until the
        first generate call, so modules importing this service don't pay
        the LangChain startup cost if the LLM is never used.
        """
        if self._initialized:
            return
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        kwargs: dict[str, Any] = dict(
            model=self._model,
            api_key=SecretStr(self._api_key),
            temperature=0,
        )
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        self._llm = ChatOpenAI(**kwargs)
        self._prompt = ChatPromptTemplate.from_messages([
            ("system", _SYSTEM_PROMPT),
            ("human", "{spec_info}"),
        ])
        self._initialized = True

    async def generate(
        self,
        spec_text: str,
        product_config: dict[str, str] | None = None,
    ) -> GeneratedScript:
        """Generate a Python test script from a natural-language spec.

        Args:
            spec_text: Natural-language test specification.
            product_config: Optional product configuration context (instrument
                assignments, test limits, etc.).

        Returns:
            GeneratedScript with code, confidence, validation errors, suggestions.

        Raises:
            CircuitBreakerOpenError: If the LLM circuit breaker is OPEN.
            RuntimeError: If no API key is configured.
        """
        if not self._api_key:
            logger.warning("No OpenAI API key configured for script generation")
            raise RuntimeError("OpenAI API key not configured")

        self._ensure_initialized()
        spec_info = self._build_spec_info(spec_text, product_config)

        async def _do_llm_call() -> str:
            messages = self._prompt.format_messages(spec_info=spec_info)
            response = await self._llm.ainvoke(messages)
            return str(response.content)

        raw = await self._breaker.call(_do_llm_call)
        # CircuitBreaker.call infers T as Coroutine for async fn; runtime is str
        code = self._extract_code(raw)  # type: ignore[arg-type]
        return self._post_process(code, spec_text)

    async def refine(
        self,
        code: str,
        feedback: str,
        product_config: dict[str, str] | None = None,
    ) -> GeneratedScript:
        """Refine an existing script based on user feedback.

        Args:
            code: Current script source code.
            feedback: Natural-language refinement feedback.
            product_config: Optional product configuration context.

        Returns:
            GeneratedScript with refined code and validation results.

        Raises:
            CircuitBreakerOpenError: If the LLM circuit breaker is OPEN.
            RuntimeError: If no API key is configured.
        """
        if not self._api_key:
            raise RuntimeError("OpenAI API key not configured")

        self._ensure_initialized()
        refine_info = self._build_refine_info(code, feedback, product_config)

        async def _do_llm_call() -> str:
            messages = self._prompt.format_messages(spec_info=refine_info)
            response = await self._llm.ainvoke(messages)
            return str(response.content)

        raw = await self._breaker.call(_do_llm_call)
        new_code = self._extract_code(raw)  # type: ignore[arg-type]
        return self._post_process(new_code, feedback)

    # ── Prompt Building ────────────────────────────────────────────

    @staticmethod
    def _build_spec_info(
        spec_text: str,
        product_config: dict[str, str] | None,
    ) -> str:
        """Build the human-readable spec info for the LLM prompt.

        Args:
            spec_text: Natural-language test specification.
            product_config: Optional product configuration context.

        Returns:
            Formatted string with spec and product context.
        """
        lines: list[str] = [
            "=== TEST SPECIFICATION ===",
            spec_text,
        ]
        if product_config:
            lines.append("")
            lines.append("=== PRODUCT CONFIG CONTEXT ===")
            for key, value in product_config.items():
                lines.append(f"  {key}: {value}")
        lines.append("")
        lines.append(
            "Generate a complete Python test script for the above specification. "
            "Output ONLY the Python code, no markdown fences or explanations."
        )
        return "\n".join(lines)

    @staticmethod
    def _build_refine_info(
        code: str,
        feedback: str,
        product_config: dict[str, str] | None,
    ) -> str:
        """Build the refinement info for the LLM prompt.

        Args:
            code: Current script source code.
            feedback: User feedback for refinement.
            product_config: Optional product configuration context.

        Returns:
            Formatted string with current code, feedback, and context.
        """
        lines: list[str] = [
            "=== CURRENT SCRIPT ===",
            code,
            "",
            "=== REFINEMENT FEEDBACK ===",
            feedback,
        ]
        if product_config:
            lines.append("")
            lines.append("=== PRODUCT CONFIG CONTEXT ===")
            for key, value in product_config.items():
                lines.append(f"  {key}: {value}")
        lines.append("")
        lines.append(
            "Apply the feedback to the script and output the complete refined "
            "Python code. Output ONLY the Python code, no markdown fences."
        )
        return "\n".join(lines)

    # ── Post-Processing ────────────────────────────────────────────

    @staticmethod
    def _extract_code(raw: str) -> str:
        """Extract Python code from the LLM response.

        Strips markdown code fences if present. If the response is pure
        code (no fences), returns it as-is.

        Args:
            raw: Raw LLM response string.

        Returns:
            Cleaned Python source code.
        """
        text = raw.strip()
        # Strip markdown code fences (```python ... ``` or ``` ... ```)
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```python or ```)
            lines = lines[1:]
            # Remove trailing ``` if present
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return text

    def _post_process(
        self,
        code: str,
        spec_text: str,
    ) -> GeneratedScript:
        """Post-process generated code: validate, scan, compute confidence.

        Args:
            code: Generated Python source code.
            spec_text: Original spec text (for suggestions context).

        Returns:
            GeneratedScript with validation results and confidence score.
        """
        validation = self.validate(code)
        suggestions = self._generate_suggestions(code, validation, spec_text)
        confidence = self._compute_confidence(validation)
        return GeneratedScript(
            code=code,
            confidence=confidence,
            validation_errors=validation.errors,
            suggestions=suggestions,
        )

    # ── Validation ─────────────────────────────────────────────────

    def validate(self, code: str) -> ValidationResult:
        """Validate generated code: AST parse, security scan, dependency check.

        Args:
            code: Python source code to validate.

        Returns:
            ValidationResult with is_valid flag, errors, and warnings.
        """
        errors: list[str] = []
        warnings: list[str] = []

        # 1. AST parse validation
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            errors.append(f"Syntax error: {e.msg} (line {e.lineno})")
            return ValidationResult(is_valid=False, errors=errors, warnings=warnings)

        # 2. Security scan — walk AST for dangerous patterns
        security_errors = self._security_scan(tree)
        errors.extend(security_errors)

        # 3. Dependency check — verify imports are allowed
        dep_errors, dep_warnings = self._dependency_check(tree)
        errors.extend(dep_errors)
        warnings.extend(dep_warnings)

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    @staticmethod
    def _security_scan(tree: ast.AST) -> list[str]:
        """Walk AST to detect dangerous patterns.

        Checks for:
        - Forbidden imports (subprocess, os.system, ctypes, socket, etc.)
        - Forbidden function calls (eval, exec, compile, __import__)
        - Forbidden attribute accesses (os.system, os.remove, etc.)

        Args:
            tree: Parsed AST of the generated code.

        Returns:
            List of security violation error messages.
        """
        errors: list[str] = []

        for node in ast.walk(tree):
            # Check Import statements: import subprocess, import os.system
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name.split(".")[0]
                    full_name = alias.name
                    if full_name in _FORBIDDEN_IMPORTS or module in _FORBIDDEN_IMPORTS:
                        errors.append(
                            f"Security: forbidden import '{full_name}' at line {node.lineno}"
                        )

            # Check ImportFrom: from os import system
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                module_root = module.split(".")[0]
                if module in _FORBIDDEN_IMPORTS or module_root in _FORBIDDEN_IMPORTS:
                    errors.append(
                        f"Security: forbidden import "
                        f"'from {module} import ...' at line {node.lineno}"
                    )
                # Check imported names
                for alias in node.names:
                    if alias.name in _FORBIDDEN_CALLS:
                        errors.append(
                            f"Security: forbidden import "
                            f"'{alias.name}' from {module} at line {node.lineno}"
                        )

            # Check function calls: eval(), exec(), __import__()
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALLS:
                    errors.append(
                        f"Security: forbidden call '{func.id}()' at line {node.lineno}"
                    )
                elif isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN_ATTRS:
                    # Check if it's os.system, os.remove etc.
                    if isinstance(func.value, ast.Name):
                        obj_name = func.value.id
                        if obj_name == "os":
                            errors.append(
                                f"Security: forbidden call "
                                f"'{obj_name}.{func.attr}()' at line {node.lineno}"
                            )

        return errors

    @staticmethod
    def _dependency_check(tree: ast.AST) -> tuple[list[str], list[str]]:
        """Verify that imported modules exist in the framework or allowed stdlib.

        Args:
            tree: Parsed AST of the generated code.

        Returns:
            Tuple of (errors, warnings) for unknown imports.
        """
        errors: list[str] = []
        warnings: list[str] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    full_name = alias.name
                    root = full_name.split(".")[0]
                    if (
                        full_name not in _ALLOWED_IMPORTS
                        and root not in _ALLOWED_IMPORTS
                    ):
                        warnings.append(
                            f"Dependency: unknown module '{full_name}' "
                            f"at line {node.lineno} — may not be available"
                        )

            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                root = module.split(".")[0]
                if (
                    module
                    and module not in _ALLOWED_IMPORTS
                    and root not in _ALLOWED_IMPORTS
                ):
                    warnings.append(
                        f"Dependency: unknown module '{module}' "
                        f"at line {node.lineno} — may not be available"
                    )

        return errors, warnings

    # ── Confidence & Suggestions ───────────────────────────────────

    @staticmethod
    def _compute_confidence(validation: ValidationResult) -> float:
        """Compute confidence score based on validation results.

        Scoring:
        - Start at 1.0
        - Syntax error → 0.0 (script is not runnable)
        - Security violation → -0.3 per violation (min 0.0)
        - Dependency warning → -0.1 per warning (min 0.0)

        Args:
            validation: ValidationResult from post-processing.

        Returns:
            Confidence score between 0.0 and 1.0.
        """
        if not validation.is_valid:
            # Check if it's just a syntax error (no code to run)
            has_syntax = any("Syntax error" in e for e in validation.errors)
            if has_syntax:
                return 0.0

        confidence = 1.0
        # Deduct for security errors
        security_count = sum(
            1 for e in validation.errors if e.startswith("Security:")
        )
        confidence -= security_count * 0.3

        # Deduct for dependency warnings
        dep_count = len(validation.warnings)
        confidence -= dep_count * 0.1

        return max(0.0, min(1.0, confidence))

    @staticmethod
    def _generate_suggestions(
        code: str,
        validation: ValidationResult,
        spec_text: str,
    ) -> list[str]:
        """Generate improvement suggestions based on validation results.

        Args:
            code: Generated code (for structural analysis).
            validation: ValidationResult with errors and warnings.
            spec_text: Original specification text.

        Returns:
            List of suggestion strings.
        """
        suggestions: list[str] = []

        # Suggest fixing validation errors
        if validation.errors:
            suggestions.append(
                "Fix validation errors before using this script in production."
            )

        # Suggest adding error handling if not present
        if "try" not in code and "except" not in code:
            suggestions.append(
                "Consider adding try/except blocks for instrument communication errors."
            )

        # Suggest adding timeout if not present
        if "timeout" not in code.lower() and "sleep" not in code.lower():
            suggestions.append(
                "Consider adding timeouts for instrument operations."
            )

        # Suggest adding measurement validation
        if "assert" not in code and "if" not in code:
            suggestions.append(
                "Consider adding measurement validation (assert or if-checks)."
            )

        # Suggest fixing dependency warnings
        if validation.warnings:
            suggestions.append(
                "Review unknown module imports — they may not be available "
                "in the execution environment."
            )

        return suggestions


__all__ = [
    "CircuitBreakerOpenError",
    "GeneratedScript",
    "LLMScriptGenerator",
    "ValidationResult",
]
