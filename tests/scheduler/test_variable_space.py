"""Unit tests for VariableSpace.

Tests cover:
- Three-level scope hierarchy (scope/steps/global)
- Thread-safe read/write operations
- Variable resolution with ${scope.xxx} syntax
- Whitelist validation for write operations
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ate_platform.scheduler.variable_space import VariableSpace


class TestVariableSpaceScopeHierarchy:
    """Test the three-level scope hierarchy."""

    def test_scope_level_variable(self) -> None:
        """Test sequence-level variable storage and retrieval."""
        vs = VariableSpace()
        vs.set("scope.voltage", 3.3)
        assert vs.get("scope.voltage") == 3.3

    def test_steps_level_variable(self) -> None:
        """Test step-level variable storage and retrieval."""
        vs = VariableSpace()
        vs.set("steps.step1.result", "pass")
        assert vs.get("steps.step1.result") == "pass"

    def test_global_level_variable(self) -> None:
        """Test global variable retrieval (read-only)."""
        vs = VariableSpace()
        vs.set_global("test_mode", "production")
        assert vs.get("global.test_mode") == "production"

    def test_multiple_steps_independent(self) -> None:
        """Test that different steps have independent variable storage."""
        vs = VariableSpace()
        vs.set("steps.step1.result", "pass")
        vs.set("steps.step2.result", "fail")
        assert vs.get("steps.step1.result") == "pass"
        assert vs.get("steps.step2.result") == "fail"

    def test_nonexistent_variable_returns_default(self) -> None:
        """Test that missing variables return the default value."""
        vs = VariableSpace()
        assert vs.get("scope.nonexistent", "default") == "default"
        assert vs.get("steps.step1.missing", None) is None

    def test_invalid_scope_prefix(self) -> None:
        """Test that invalid scope prefixes return default."""
        vs = VariableSpace()
        assert vs.get("invalid.var", "default") == "default"


class TestVariableSpaceWriteValidation:
    """Test write validation with whitelist."""

    def test_cannot_write_global_scope(self) -> None:
        """Test that global scope is read-only."""
        vs = VariableSpace()
        with pytest.raises(ValueError, match="Cannot write to 'global' scope"):
            vs.set("global.test", "value")

    def test_invalid_variable_name_format(self) -> None:
        """Test that invalid variable names raise errors."""
        vs = VariableSpace()
        with pytest.raises(ValueError, match="Invalid variable name"):
            vs.set("invalid_name", "value")

    def test_invalid_steps_variable_name(self) -> None:
        """Test that steps variables require step_id and key."""
        vs = VariableSpace()
        with pytest.raises(ValueError, match="Invalid steps variable name"):
            vs.set("steps.step1", "value")  # Missing key


class TestVariableSpaceResolution:
    """Test variable resolution in expressions."""

    def test_resolve_single_variable(self) -> None:
        """Test resolving a single variable in expression."""
        vs = VariableSpace()
        vs.set("scope.voltage", 3.3)
        assert vs.resolve("${scope.voltage}") == "3.3"

    def test_resolve_multiple_variables(self) -> None:
        """Test resolving multiple variables in one expression."""
        vs = VariableSpace()
        vs.set("scope.voltage", 3.3)
        vs.set("scope.current", 1.5)
        result = vs.resolve("Voltage: ${scope.voltage}V, Current: ${scope.current}A")
        assert result == "Voltage: 3.3V, Current: 1.5A"

    def test_resolve_steps_variable(self) -> None:
        """Test resolving step-level variables."""
        vs = VariableSpace()
        vs.set("steps.step1.result", "pass")
        assert vs.resolve("${steps.step1.result}") == "pass"

    def test_resolve_nonexistent_variable(self) -> None:
        """Test that unresolved variables remain unchanged."""
        vs = VariableSpace()
        result = vs.resolve("${scope.nonexistent}")
        assert result == "${scope.nonexistent}"

    def test_resolve_mixed_text(self) -> None:
        """Test resolution with surrounding text."""
        vs = VariableSpace()
        vs.set("scope.status", "PASS")
        result = vs.resolve("Test result: ${scope.status}")
        assert result == "Test result: PASS"


class TestVariableSpaceThreadSafety:
    """Test thread-safe operations."""

    def test_concurrent_writes(self) -> None:
        """Test that concurrent writes are thread-safe."""
        vs = VariableSpace()
        num_threads = 10
        num_writes = 100

        def write_values(thread_id: int) -> None:
            for i in range(num_writes):
                vs.set(f"scope.thread_{thread_id}_var_{i}", i)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(write_values, i) for i in range(num_threads)]
            for future in futures:
                future.result()

        # Verify all values were written
        for thread_id in range(num_threads):
            for i in range(num_writes):
                assert vs.get(f"scope.thread_{thread_id}_var_{i}") == i

    def test_concurrent_read_write(self) -> None:
        """Test concurrent read and write operations."""
        vs = VariableSpace()
        vs.set("scope.counter", 0)
        num_iterations = 100

        def writer() -> None:
            for i in range(num_iterations):
                current = vs.get("scope.counter")
                vs.set("scope.counter", current + 1)

        def reader(results: list) -> None:
            for _ in range(num_iterations):
                results.append(vs.get("scope.counter"))

        read_results: list = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            write_future = executor.submit(writer)
            read_futures = [executor.submit(reader, read_results) for _ in range(3)]
            write_future.result()
            for f in read_futures:
                f.result()

        # The counter should be incremented exactly num_iterations times
        assert vs.get("scope.counter") == num_iterations

    def test_step_vars_thread_safety(self) -> None:
        """Test thread-safe operations on step-level variables."""
        vs = VariableSpace()
        num_threads = 10

        def write_step_vars(step_id: int) -> None:
            for i in range(50):
                vs.set(f"steps.step{step_id}.var{i}", i)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(write_step_vars, i) for i in range(num_threads)]
            for future in futures:
                future.result()

        # Verify all step variables
        for step_id in range(num_threads):
            for i in range(50):
                assert vs.get(f"steps.step{step_id}.var{i}") == i


class TestVariableSpaceHelperMethods:
    """Test helper methods for variable management."""

    def test_set_global_internal(self) -> None:
        """Test internal global variable setting."""
        vs = VariableSpace()
        vs.set_global("config_key", "config_value")
        assert vs.get("global.config_key") == "config_value"

    def test_clear_scope(self) -> None:
        """Test clearing all sequence-level variables."""
        vs = VariableSpace()
        vs.set("scope.v1", 1)
        vs.set("scope.v2", 2)
        vs.clear_scope()
        assert vs.get("scope.v1") is None
        assert vs.get("scope.v2") is None

    def test_clear_steps(self) -> None:
        """Test clearing all step-level variables."""
        vs = VariableSpace()
        vs.set("steps.step1.var", 1)
        vs.set("steps.step2.var", 2)
        vs.clear_steps()
        assert vs.get("steps.step1.var") is None
        assert vs.get("steps.step2.var") is None

    def test_get_all_scope_vars(self) -> None:
        """Test getting a copy of all scope variables."""
        vs = VariableSpace()
        vs.set("scope.v1", 1)
        vs.set("scope.v2", 2)
        scope_copy = vs.get_all_scope_vars()
        assert scope_copy == {"v1": 1, "v2": 2}

        # Verify it's a copy, not a reference
        scope_copy["v3"] = 3
        assert vs.get("scope.v3") is None

    def test_get_step_vars(self) -> None:
        """Test getting a copy of step variables."""
        vs = VariableSpace()
        vs.set("steps.step1.var1", 1)
        vs.set("steps.step1.var2", 2)
        step_vars = vs.get_step_vars("step1")
        assert step_vars == {"var1": 1, "var2": 2}

        # Test nonexistent step
        empty_vars = vs.get_step_vars("nonexistent")
        assert empty_vars == {}


class TestVariableSpaceQA:
    """Test scenarios from QA requirements."""

    def test_qa_scenario(self) -> None:
        """Test the exact QA scenario from task requirements."""
        vs = VariableSpace()
        # Note: In real usage, we use set() method
        # The task spec shows direct _scope assignment, which works too
        vs._scope["voltage"] = 3.3
        vs._steps["step1"] = {"result": "pass"}

        assert vs.get("scope.voltage") == 3.3
        assert vs.get("steps.step1.result") == "pass"
        assert vs.resolve("${scope.voltage}") == "3.3"

    def test_three_scope_hierarchy(self) -> None:
        """Test verification criteria: scope/steps/global three-level hierarchy."""
        vs = VariableSpace()
        vs.set("scope.sequence_var", "seq_value")
        vs.set("steps.test_step.step_var", "step_value")
        vs.set_global("global_var", "global_value")

        assert vs.get("scope.sequence_var") == "seq_value"
        assert vs.get("steps.test_step.step_var") == "step_value"
        assert vs.get("global.global_var") == "global_value"

    def test_thread_safe_read_write(self) -> None:
        """Test verification criteria: thread-safe read/write."""
        vs = VariableSpace()
        results: list = []

        def concurrent_operations(thread_id: int) -> None:
            # Write
            vs.set(f"scope.thread_var_{thread_id}", thread_id)
            # Read
            value = vs.get(f"scope.thread_var_{thread_id}")
            results.append((thread_id, value))

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(concurrent_operations, i) for i in range(5)]
            for future in futures:
                future.result()

        # Verify all operations completed correctly
        for thread_id, value in results:
            assert value == thread_id

    def test_variable_resolution_correctness(self) -> None:
        """Test verification criteria: variable resolution correct."""
        vs = VariableSpace()
        vs.set("scope.voltage", 3.3)
        vs.set("scope.current", 1.5)
        vs.set("steps.measurement.result", "PASS")

        # Test single resolution
        assert vs.resolve("${scope.voltage}") == "3.3"

        # Test multiple resolutions
        result = vs.resolve("V=${scope.voltage}V, I=${scope.current}A")
        assert result == "V=3.3V, I=1.5A"

        # Test step resolution
        assert vs.resolve("${steps.measurement.result}") == "PASS"