"""Tests for ContextProxy and measure decorator."""

import logging

import pytest

from ate_platform.executor import ContextProxy, measure
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.variable_space import VariableSpace


@pytest.fixture
def variable_space() -> VariableSpace:
    """Create a fresh VariableSpace for testing."""
    vs = VariableSpace()
    vs.set_global("test_config", "default")
    return vs


@pytest.fixture
def resource_manager() -> ResourceManager:
    """Create a fresh ResourceManager for testing."""
    return ResourceManager()


@pytest.fixture
def context_proxy(
    variable_space: VariableSpace, resource_manager: ResourceManager
) -> ContextProxy:
    """Create a ContextProxy instance for testing."""
    return ContextProxy(
        _variable_space=variable_space,
        _resource_manager=resource_manager,
        _step_id="step1",
    )


class TestContextProxyGetItem:
    """Tests for __getitem__ (variable read access)."""

    def test_read_scope_variable(
        self, context_proxy: ContextProxy, variable_space: VariableSpace
    ) -> None:
        """Should read scope-level variables."""
        variable_space.set("scope.voltage", 3.3)
        assert context_proxy["scope.voltage"] == 3.3

    def test_read_steps_variable(
        self, context_proxy: ContextProxy, variable_space: VariableSpace
    ) -> None:
        """Should read step-level variables."""
        variable_space.set("steps.step1.result", 42)
        assert context_proxy["steps.step1.result"] == 42

    def test_read_global_variable(
        self, context_proxy: ContextProxy, variable_space: VariableSpace
    ) -> None:
        """Should read global variables."""
        assert context_proxy["global.test_config"] == "default"

    def test_read_nonexistent_variable_returns_none(
        self, context_proxy: ContextProxy
    ) -> None:
        """Should return None for nonexistent variables."""
        assert context_proxy["scope.nonexistent"] is None

    def test_read_with_default(
        self, context_proxy: ContextProxy, variable_space: VariableSpace
    ) -> None:
        """Should respect default value for missing variables."""
        result = variable_space.get("scope.missing", default="fallback")
        assert result == "fallback"


class TestContextProxySetItem:
    """Tests for __setitem__ (variable write access with whitelist)."""

    def test_write_step_output_simple(self, context_proxy: ContextProxy) -> None:
        """Should allow writing simple step output variables."""
        context_proxy["result"] = 42
        assert context_proxy["steps.step1.result"] == 42
        assert context_proxy.get_outputs()["result"] == 42

    def test_write_step_output_with_prefix(
        self, context_proxy: ContextProxy
    ) -> None:
        """Should allow writing with steps prefix for same step."""
        context_proxy["steps.step1.voltage"] = 3.3
        assert context_proxy["steps.step1.voltage"] == 3.3

    def test_prevent_writing_to_another_step(
        self, context_proxy: ContextProxy
    ) -> None:
        """Should not allow writing to another step's variables."""
        with pytest.raises(ValueError, match="Cannot write to another step"):
            context_proxy["steps.step2.result"] = 10

    def test_prevent_writing_to_global(
        self, context_proxy: ContextProxy
    ) -> None:
        """Should not allow writing to global scope."""
        with pytest.raises(ValueError, match="Cannot write to 'global' scope"):
            context_proxy["global.new_var"] = "value"

    def test_prevent_writing_to_scope(
        self, context_proxy: ContextProxy
    ) -> None:
        """Should not allow writing to scope directly."""
        with pytest.raises(ValueError, match="Cannot write to 'scope' scope"):
            context_proxy["scope.voltage"] = 5.0

    def test_write_with_declared_outputs(self, context_proxy: ContextProxy) -> None:
        """Should allow writing declared outputs."""
        context_proxy.declare_output("voltage")
        context_proxy["voltage"] = 3.3
        assert context_proxy["steps.step1.voltage"] == 3.3

    def test_prevent_undeclared_output_when_declarations_exist(
        self, context_proxy: ContextProxy
    ) -> None:
        """Should prevent writing undeclared outputs when declarations exist."""
        context_proxy.declare_output("voltage")
        with pytest.raises(ValueError, match="Cannot write to undeclared output"):
            context_proxy["current"] = 0.5


class TestContextProxyResource:
    """Tests for resource() method."""

    def test_resource_returns_proxy(
        self, context_proxy: ContextProxy
    ) -> None:
        """Should return a ResourceProxy."""
        proxy = context_proxy.resource("DMM_CH1")
        assert proxy.resource_id == "DMM_CH1"
        assert proxy.owner_id == "step1"

    def test_resource_acquire_and_release(
        self, context_proxy: ContextProxy
    ) -> None:
        """Should acquire and release resource."""
        proxy = context_proxy.resource("DMM_CH1")
        assert proxy.acquire(timeout=1.0) is True
        assert proxy.is_available() is False
        proxy.release()
        assert proxy.is_available() is True

    def test_resource_context_manager(
        self, context_proxy: ContextProxy
    ) -> None:
        """Should work as context manager."""
        proxy = context_proxy.resource("DMM_CH1")
        with proxy:
            assert proxy.is_available() is False
        assert proxy.is_available() is True


class TestContextProxyLog:
    """Tests for log() method."""

    def test_log_info(self, context_proxy: ContextProxy, caplog: pytest.LogCaptureFixture) -> None:
        """Should log info message with step context."""
        with caplog.at_level(logging.INFO):
            context_proxy.log("info", "Test message")
        assert "Test message" in caplog.text
        assert "step1" in caplog.text.lower()

    def test_log_warning(self, context_proxy: ContextProxy, caplog: pytest.LogCaptureFixture) -> None:
        """Should log warning message."""
        with caplog.at_level(logging.WARNING):
            context_proxy.log("warning", "Warning test")
        assert "Warning test" in caplog.text

    def test_log_error(self, context_proxy: ContextProxy, caplog: pytest.LogCaptureFixture) -> None:
        """Should log error message."""
        with caplog.at_level(logging.ERROR):
            context_proxy.log("error", "Error test")
        assert "Error test" in caplog.text


class TestMeasureDecorator:
    """Tests for @measure decorator."""

    def test_measure_declares_outputs(
        self, context_proxy: ContextProxy
    ) -> None:
        """Should declare outputs when decorated function runs."""

        @measure("voltage", "current")
        def test_func(proxy: ContextProxy) -> None:
            proxy["voltage"] = 3.3
            proxy["current"] = 0.5

        test_func(context_proxy)

        assert context_proxy["steps.step1.voltage"] == 3.3
        assert context_proxy["steps.step1.current"] == 0.5

    def test_measure_prevents_undeclared_outputs(
        self, context_proxy: ContextProxy
    ) -> None:
        """Should prevent writing undeclared outputs in decorated function."""

        @measure("voltage")
        def test_func(proxy: ContextProxy) -> None:
            proxy["voltage"] = 3.3
            proxy["current"] = 0.5  # This should fail

        with pytest.raises(ValueError, match="Cannot write to undeclared output"):
            test_func(context_proxy)

    def test_measure_preserves_function_metadata(self) -> None:
        """Should preserve function name and docstring."""

        @measure("result")
        def my_test(proxy: ContextProxy) -> None:
            """Test docstring."""
            pass

        assert my_test.__name__ == "my_test"
        assert my_test.__doc__ == "Test docstring."

    def test_measure_with_no_outputs(
        self, context_proxy: ContextProxy
    ) -> None:
        """Should work with no output declarations."""

        @measure()
        def test_func(proxy: ContextProxy) -> None:
            # When no declarations, any output is allowed
            proxy["result"] = 42

        test_func(context_proxy)
        assert context_proxy["steps.step1.result"] == 42


class TestContextProxyOutputs:
    """Tests for get_outputs() and output tracking."""

    def test_get_outputs_returns_copy(
        self, context_proxy: ContextProxy
    ) -> None:
        """Should return a copy of outputs."""
        context_proxy["result"] = 42
        outputs = context_proxy.get_outputs()
        assert outputs == {"result": 42}
        outputs["modified"] = "value"  # Should not affect proxy
        assert "modified" not in context_proxy.get_outputs()

    def test_outputs_tracked_on_set(self, context_proxy: ContextProxy) -> None:
        """Should track all outputs set via __setitem__."""
        context_proxy["voltage"] = 3.3
        context_proxy["current"] = 0.5
        outputs = context_proxy.get_outputs()
        assert outputs == {"voltage": 3.3, "current": 0.5}


class TestIntegration:
    """Integration tests for ContextProxy with VariableSpace and ResourceManager."""

    def test_full_workflow(
        self, variable_space: VariableSpace, resource_manager: ResourceManager
    ) -> None:
        """Test complete workflow with variable and resource access."""
        # Setup
        variable_space.set("scope.test_voltage", 5.0)

        proxy = ContextProxy(
            _variable_space=variable_space,
            _resource_manager=resource_manager,
            _step_id="integration_test",
        )

        # Read variable
        assert proxy["scope.test_voltage"] == 5.0

        # Write output
        proxy["measured_voltage"] = 5.1

        # Resource access
        resource = proxy.resource("TEST_EQUIPMENT")
        assert resource.acquire(timeout=1.0)
        resource.release()

        # Logging
        proxy.log("info", "Integration test complete")

        # Verify outputs
        outputs = proxy.get_outputs()
        assert outputs == {"measured_voltage": 5.1}
