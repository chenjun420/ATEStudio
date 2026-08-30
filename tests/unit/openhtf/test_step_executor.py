"""Unit tests for OpenHTFStepExecutor.

Tests cover:
- StepExecutor Protocol compliance (all required methods present)
- execute_async signature matching the Protocol
- script_path mapping to importlib.import_module call
- openhtf presence in pyproject.toml [project] dependencies
"""

import inspect
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from ate_platform.executor.step_executor import StepExecutor
from ate_platform.openhtf.step_executor import OpenHTFStepExecutor


def _find_pyproject() -> Path:
    """Search upward from this file to find pyproject.toml."""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        candidate = current / "pyproject.toml"
        if candidate.exists():
            return candidate
        current = current.parent
    raise FileNotFoundError("pyproject.toml not found above test directory")


class TestOpenHTFStepExecutorProtocol:
    """Verify OpenHTFStepExecutor implements the StepExecutor Protocol."""

    def test_implements_step_executor_protocol(self) -> None:
        """OpenHTFStepExecutor should have all StepExecutor Protocol methods."""
        executor = OpenHTFStepExecutor()
        assert isinstance(executor, StepExecutor)

        # Verify each protocol method is callable.
        for method_name in ("execute", "execute_async", "execute_batch", "pool_stats"):
            assert hasattr(executor, method_name), f"Missing method: {method_name}"
            assert callable(getattr(executor, method_name)), f"Not callable: {method_name}"

    def test_execute_async_accepts_script_path_and_params(self) -> None:
        """execute_async signature must match the Protocol definition."""
        sig = inspect.signature(OpenHTFStepExecutor.execute_async)

        expected_params = {"self", "script_path", "params", "step_id", "timeout", "run_id"}
        actual_params = set(sig.parameters.keys())
        assert actual_params == expected_params

        # Optional parameters must default to None.
        assert sig.parameters["step_id"].default is None
        assert sig.parameters["timeout"].default is None
        assert sig.parameters["run_id"].default is None

        # script_path and params must be positional/required.
        assert sig.parameters["script_path"].default is inspect.Parameter.empty
        assert sig.parameters["params"].default is inspect.Parameter.empty

    def test_script_path_maps_to_test_module(self) -> None:
        """importlib.import_module must be called with script_path."""
        executor = OpenHTFStepExecutor()

        # Build a mock module with a module-level 'test' attribute.
        mock_module = MagicMock()
        mock_module.test = MagicMock()
        mock_module.test.execute.return_value = None

        with patch(
            "ate_platform.openhtf.step_executor.import_module",
            return_value=mock_module,
        ) as mock_import:
            executor.execute("tests.openhtf.my_test_module", {})

        mock_import.assert_called_once_with("tests.openhtf.my_test_module")


class TestOpenHTFDependency:
    """Verify openhtf is declared as a production dependency."""

    def test_openhtf_in_pyproject_deps(self) -> None:
        """openhtf must appear in [project.optional-dependencies] (openhtf extra)."""
        pyproject_path = _find_pyproject()
        with open(pyproject_path, "rb") as f:
            data: dict[str, Any] = tomllib.load(f)

        optional: dict[str, list[str]] = data["project"]["optional-dependencies"]
        all_optional: list[str] = [
            dep for deps in optional.values() for dep in deps
        ]
        assert any("openhtf" in dep.lower() for dep in all_optional), (
            "openhtf not found in [project.optional-dependencies]"
        )
