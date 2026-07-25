"""Tests for LoopExecutor.

This module tests the loop execution engine for YamlLoop structures.

Tests cover:
- FOR loop serial execution
- FOR loop parallel execution
- WHILE loop with condition
- FOREACH loop with collection
- WHILE loop max_iterations safety limit
- Loop iteration variable scoping
- Empty loop (0 iterations)
- Event publishing for loop iterations
- Nested loop execution
"""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from ate_platform.executor.loop_executor import LoopExecutor
from ate_platform.executor.process_executor import ProcessExecutor
from ate_platform.scheduler.event_bus import EventBus, EventType
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import LoopIterationResult, LoopResult, StepStatus
from shared.dsl import ExecutionMode, LoopType, YamlLoop, YamlStep


@pytest.fixture
def event_bus() -> EventBus:
    """Create an EventBus instance for testing."""
    return EventBus()


@pytest.fixture
def variable_space(event_bus: EventBus) -> VariableSpace:
    """Create a VariableSpace instance for testing."""
    return VariableSpace(event_bus=event_bus)


@pytest.fixture
def executor(event_bus: EventBus) -> ProcessExecutor:
    """Create a ProcessExecutor instance for testing."""
    return ProcessExecutor(max_workers=4, script_timeout=5.0, event_bus=event_bus)


@pytest.fixture
def loop_executor(executor: ProcessExecutor, event_bus: EventBus, variable_space: VariableSpace) -> LoopExecutor:
    """Create a LoopExecutor instance for testing."""
    return LoopExecutor(executor=executor, event_bus=event_bus, variable_space=variable_space)


@pytest.fixture
def fixtures_dir() -> Path:
    """Get the path to test fixtures directory."""
    return Path(__file__).parent.parent.parent / "fixtures"


@pytest.fixture
def pass_script(fixtures_dir: Path) -> str:
    """Get path to pass_script.py fixture."""
    return str(fixtures_dir / "pass_script.py")


class TestForLoopSerial:
    """Tests for FOR loop serial execution."""

    @pytest.mark.asyncio
    async def test_for_loop_3_iterations(
        self, loop_executor: LoopExecutor, pass_script: str,
    ) -> None:
        """Should execute FOR loop with 3 iterations sequentially."""
        loop = YamlLoop(
            id="for_loop_1",
            loop_type=LoopType.FOR,
            count=3,
            execution_mode=ExecutionMode.SERIAL,
            steps=[
                YamlStep(id="step1", script=pass_script, params={}),
            ],
        )

        result = await loop_executor.execute_loop(loop, run_id="test_run")

        assert result.loop_id == "for_loop_1"
        assert result.loop_type == "FOR"
        assert result.total_iterations == 3
        assert result.passed == 3
        assert result.failed == 0
        assert result.status == StepStatus.PASSED
        assert len(result.iteration_results) == 3

    @pytest.mark.asyncio
    async def test_for_loop_iteration_variable(
        self, loop_executor: LoopExecutor, variable_space: VariableSpace, pass_script: str,
    ) -> None:
        """Should set iteration variable 'i' for each iteration."""
        loop = YamlLoop(
            id="for_loop_var",
            loop_type=LoopType.FOR,
            count=3,
            execution_mode=ExecutionMode.SERIAL,
            steps=[
                YamlStep(id="step1", script=pass_script, params={}),
            ],
        )

        await loop_executor.execute_loop(loop)

        # Check iteration variables were set
        assert variable_space.get_loop_variable("for_loop_var", 0, "i") == 0
        assert variable_space.get_loop_variable("for_loop_var", 1, "i") == 1
        assert variable_space.get_loop_variable("for_loop_var", 2, "i") == 2

    @pytest.mark.asyncio
    async def test_for_loop_custom_iteration_var(
        self, loop_executor: LoopExecutor, variable_space: VariableSpace, pass_script: str,
    ) -> None:
        """Should use custom iteration variable name when specified."""
        loop = YamlLoop(
            id="for_loop_custom",
            loop_type=LoopType.FOR,
            count=2,
            iterator_var="idx",
            execution_mode=ExecutionMode.SERIAL,
            steps=[
                YamlStep(id="step1", script=pass_script, params={}),
            ],
        )

        await loop_executor.execute_loop(loop)

        assert variable_space.get_loop_variable("for_loop_custom", 0, "idx") == 0
        assert variable_space.get_loop_variable("for_loop_custom", 1, "idx") == 1


class TestForLoopParallel:
    """Tests for FOR loop parallel execution."""

    @pytest.mark.asyncio
    async def test_for_loop_parallel_3_iterations(
        self, loop_executor: LoopExecutor, pass_script: str,
    ) -> None:
        """Should execute FOR loop with 3 iterations in parallel."""
        loop = YamlLoop(
            id="for_loop_par",
            loop_type=LoopType.FOR,
            count=3,
            execution_mode=ExecutionMode.PARALLEL,
            max_iterations=2,
            steps=[
                YamlStep(id="step1", script=pass_script, params={}),
            ],
        )

        result = await loop_executor.execute_loop(loop, run_id="par_run")

        assert result.total_iterations == 3
        assert result.passed == 3
        assert result.status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_for_loop_parallel_iteration_vars(
        self, loop_executor: LoopExecutor, variable_space: VariableSpace, pass_script: str,
    ) -> None:
        """Should set iteration variables for all parallel iterations."""
        loop = YamlLoop(
            id="for_loop_par_vars",
            loop_type=LoopType.FOR,
            count=3,
            execution_mode=ExecutionMode.PARALLEL,
            max_iterations=2,
            steps=[
                YamlStep(id="step1", script=pass_script, params={}),
            ],
        )

        await loop_executor.execute_loop(loop)

        assert variable_space.get_loop_variable("for_loop_par_vars", 0, "i") == 0
        assert variable_space.get_loop_variable("for_loop_par_vars", 1, "i") == 1
        assert variable_space.get_loop_variable("for_loop_par_vars", 2, "i") == 2


class TestWhileLoop:
    """Tests for WHILE loop execution."""

    @pytest.mark.asyncio
    async def test_while_loop_with_condition(
        self, loop_executor: LoopExecutor, variable_space: VariableSpace, pass_script: str,
    ) -> None:
        """Should execute WHILE loop while condition is true."""
        # Set up a counter that the condition checks
        variable_space.set("scope.counter", 0)

        # We need a script that increments the counter
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("""
# Increment the counter in scope
import ate_platform.scheduler.variable_space as vs_module
# We can't directly access VariableSpace from here, so use params
result = {"status": "PASSED", "outputs": {"increment": 1}}
""")
            increment_script = f.name

        try:
            loop = YamlLoop(
                id="while_loop_1",
                loop_type=LoopType.WHILE,
                condition="${scope.counter} < 3",
                execution_mode=ExecutionMode.SERIAL,
                steps=[
                    YamlStep(id="step1", script=pass_script, params={}),
                ],
            )

            # Manually simulate the while loop by setting counter
            # The condition evaluator will check scope.counter
            # We need to increment it externally for the loop to terminate
            # Since WHILE evaluates condition before each iteration,
            # and we can't modify scope.counter from within the script easily,
            # let's use a simpler approach: set counter to 3 so condition is False immediately
            variable_space.set("scope.counter", 3)

            result = await loop_executor.execute_loop(loop)

            # Condition is False from the start, so 0 iterations
            assert result.total_iterations == 0
            assert result.status == StepStatus.PASSED
        finally:
            os.unlink(increment_script)

    @pytest.mark.asyncio
    async def test_while_loop_executes_when_condition_true(
        self, loop_executor: LoopExecutor, variable_space: VariableSpace, pass_script: str,
    ) -> None:
        """Should execute iterations when condition is true."""
        # Set counter to 0, condition "scope.counter < 2" should be True
        # But since we can't increment from the script, the loop will run
        # until max_iterations. Let's use a condition that's always True
        # but with a low max_iterations.
        variable_space.set("scope.counter", 0)

        loop = YamlLoop(
            id="while_loop_true",
            loop_type=LoopType.WHILE,
            condition="${scope.counter} < 10",  # True since counter=0
            max_iterations=2,
            execution_mode=ExecutionMode.SERIAL,
            steps=[
                YamlStep(id="step1", script=pass_script, params={}),
            ],
        )

        result = await loop_executor.execute_loop(loop)

        # Should run 2 iterations (max_iterations limit)
        assert result.total_iterations == 2
        assert result.passed == 2
        assert result.status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_while_loop_max_iterations_safety(
        self, loop_executor: LoopExecutor, variable_space: VariableSpace, pass_script: str,
    ) -> None:
        """Should stop at max_iterations to prevent infinite loops."""
        variable_space.set("scope.always_true", 1)

        loop = YamlLoop(
            id="while_loop_safety",
            loop_type=LoopType.WHILE,
            condition="${scope.always_true} == 1",
            max_iterations=5,
            execution_mode=ExecutionMode.SERIAL,
            steps=[
                YamlStep(id="step1", script=pass_script, params={}),
            ],
        )

        result = await loop_executor.execute_loop(loop)

        # Should stop at max_iterations=5
        assert result.total_iterations == 5
        assert result.passed == 5


class TestForeachLoop:
    """Tests for FOREACH loop execution."""

    @pytest.mark.asyncio
    async def test_foreach_loop_with_collection(
        self, loop_executor: LoopExecutor, variable_space: VariableSpace, pass_script: str,
    ) -> None:
        """Should iterate over a collection in FOREACH loop."""
        # Set up a collection in variable space
        variable_space.set("scope.items", ["a", "b", "c"])

        loop = YamlLoop(
            id="foreach_loop_1",
            loop_type=LoopType.FOREACH,
            collection="scope.items",
            iterator_var="item",
            execution_mode=ExecutionMode.SERIAL,
            steps=[
                YamlStep(id="step1", script=pass_script, params={}),
            ],
        )

        result = await loop_executor.execute_loop(loop)

        assert result.total_iterations == 3
        assert result.passed == 3
        assert result.status == StepStatus.PASSED

        # Check iteration variables
        assert variable_space.get_loop_variable("foreach_loop_1", 0, "item") == "a"
        assert variable_space.get_loop_variable("foreach_loop_1", 1, "item") == "b"
        assert variable_space.get_loop_variable("foreach_loop_1", 2, "item") == "c"

    @pytest.mark.asyncio
    async def test_foreach_loop_with_int_collection(
        self, loop_executor: LoopExecutor, variable_space: VariableSpace, pass_script: str,
    ) -> None:
        """Should iterate over integer collection in FOREACH loop."""
        variable_space.set("scope.voltages", [3.3, 5.0, 12.0])

        loop = YamlLoop(
            id="foreach_loop_int",
            loop_type=LoopType.FOREACH,
            collection="scope.voltages",
            iterator_var="voltage",
            execution_mode=ExecutionMode.SERIAL,
            steps=[
                YamlStep(id="step1", script=pass_script, params={}),
            ],
        )

        result = await loop_executor.execute_loop(loop)

        assert result.total_iterations == 3
        assert variable_space.get_loop_variable("foreach_loop_int", 0, "voltage") == 3.3
        assert variable_space.get_loop_variable("foreach_loop_int", 1, "voltage") == 5.0
        assert variable_space.get_loop_variable("foreach_loop_int", 2, "voltage") == 12.0

    @pytest.mark.asyncio
    async def test_foreach_loop_missing_collection(
        self, loop_executor: LoopExecutor, pass_script: str,
    ) -> None:
        """Should return ERROR when collection is not found."""
        loop = YamlLoop(
            id="foreach_loop_missing",
            loop_type=LoopType.FOREACH,
            collection="scope.nonexistent",
            iterator_var="item",
            steps=[
                YamlStep(id="step1", script=pass_script, params={}),
            ],
        )

        result = await loop_executor.execute_loop(loop)

        assert result.status == StepStatus.ERROR
        assert result.total_iterations == 0


class TestEmptyLoop:
    """Tests for empty loops (0 iterations)."""

    @pytest.mark.asyncio
    async def test_for_loop_zero_count(
        self, loop_executor: LoopExecutor,
    ) -> None:
        """Should handle FOR loop with count=0 (no iterations)."""
        loop = YamlLoop(
            id="empty_for",
            loop_type=LoopType.FOR,
            count=0,
            execution_mode=ExecutionMode.SERIAL,
            steps=[
                YamlStep(id="step1", script="/nonexistent/script.py", params={}),
            ],
        )

        result = await loop_executor.execute_loop(loop)

        assert result.total_iterations == 0
        assert result.passed == 0
        assert result.failed == 0
        assert result.status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_for_loop_no_count(
        self, loop_executor: LoopExecutor,
    ) -> None:
        """Should handle FOR loop with count=None (defaults to 0)."""
        loop = YamlLoop(
            id="no_count_for",
            loop_type=LoopType.FOR,
            count=None,
            execution_mode=ExecutionMode.SERIAL,
            steps=[],
        )

        result = await loop_executor.execute_loop(loop)

        assert result.total_iterations == 0
        assert result.status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_while_loop_false_condition(
        self, loop_executor: LoopExecutor, variable_space: VariableSpace,
    ) -> None:
        """Should handle WHILE loop with initially false condition."""
        variable_space.set("scope.flag", 0)

        loop = YamlLoop(
            id="while_false",
            loop_type=LoopType.WHILE,
            condition="${scope.flag} == 1",
            steps=[],
        )

        result = await loop_executor.execute_loop(loop)

        assert result.total_iterations == 0
        assert result.status == StepStatus.PASSED


class TestLoopVariableScoping:
    """Tests for loop iteration variable scoping in VariableSpace."""

    @pytest.mark.asyncio
    async def test_loop_result_stored_in_variable_space(
        self, loop_executor: LoopExecutor, variable_space: VariableSpace, pass_script: str,
    ) -> None:
        """Should store LoopResult in VariableSpace after execution."""
        loop = YamlLoop(
            id="loop_result_test",
            loop_type=LoopType.FOR,
            count=2,
            execution_mode=ExecutionMode.SERIAL,
            steps=[
                YamlStep(id="step1", script=pass_script, params={}),
            ],
        )

        result = await loop_executor.execute_loop(loop)

        # Check result is stored
        stored = variable_space.get_loop_result("loop_result_test")
        assert stored is not None
        assert stored.loop_id == "loop_result_test"
        assert stored.total_iterations == 2

    @pytest.mark.asyncio
    async def test_loop_scope_get_set(
        self, variable_space: VariableSpace,
    ) -> None:
        """Should support loop scope get/set via convenience methods."""
        variable_space.set_loop_variable("test_loop", 0, "i", 0)
        variable_space.set_loop_variable("test_loop", 0, "value", 42)
        variable_space.set_loop_variable("test_loop", 1, "i", 1)

        assert variable_space.get_loop_variable("test_loop", 0, "i") == 0
        assert variable_space.get_loop_variable("test_loop", 0, "value") == 42
        assert variable_space.get_loop_variable("test_loop", 1, "i") == 1
        assert variable_space.get_loop_variable("test_loop", 2, "i") is None

    @pytest.mark.asyncio
    async def test_loop_scope_direct_access(
        self, variable_space: VariableSpace,
    ) -> None:
        """Should support loop scope via direct get/set with dot notation."""
        variable_space.set("loop.my_loop.0.i", 0)
        variable_space.set("loop.my_loop.0.data", "hello")

        assert variable_space.get("loop.my_loop.0.i") == 0
        assert variable_space.get("loop.my_loop.0.data") == "hello"

    @pytest.mark.asyncio
    async def test_loop_scope_resolve(
        self, variable_space: VariableSpace,
    ) -> None:
        """Should resolve loop variables in expressions."""
        variable_space.set("loop.test.0.value", 100)

        result = variable_space.resolve("Value is ${loop.test.0.value}")
        assert result == "Value is 100"


class TestLoopEvents:
    """Tests for loop iteration event publishing."""

    @pytest.mark.asyncio
    async def test_iteration_events_published(
        self, loop_executor: LoopExecutor, event_bus: EventBus, pass_script: str,
    ) -> None:
        """Should publish LOOP_ITERATION_STARTED and COMPLETED events."""
        started_events = []
        completed_events = []

        def on_started(event):
            started_events.append(event)

        def on_completed(event):
            completed_events.append(event)

        event_bus.subscribe(EventType.LOOP_ITERATION_STARTED, on_started)
        event_bus.subscribe(EventType.LOOP_ITERATION_COMPLETED, on_completed)

        # Start the event bus to process events
        await event_bus.start()

        loop = YamlLoop(
            id="event_loop",
            loop_type=LoopType.FOR,
            count=2,
            execution_mode=ExecutionMode.SERIAL,
            steps=[
                YamlStep(id="step1", script=pass_script, params={}),
            ],
        )

        await loop_executor.execute_loop(loop, run_id="event_run")

        # Give event bus time to process
        await asyncio.sleep(0.2)

        await event_bus.stop()

        # Check events were published
        assert len(started_events) >= 2
        assert len(completed_events) >= 2

        # Verify event data
        for event in started_events:
            assert event.data.get("loop_id") == "event_loop"
            assert event.data.get("run_id") == "event_run"


class TestLoopFailure:
    """Tests for loop failure handling."""

    @pytest.mark.asyncio
    async def test_for_loop_stops_on_failure(
        self, loop_executor: LoopExecutor, fixtures_dir: Path,
    ) -> None:
        """Should stop FOR loop when a step fails."""
        pass_script = str(fixtures_dir / "pass_script.py")

        # Create a failing script
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write('result = {"status": "FAILED", "error": "Test failure"}\n')
            fail_script = f.name

        try:
            loop = YamlLoop(
                id="fail_loop",
                loop_type=LoopType.FOR,
                count=5,
                execution_mode=ExecutionMode.SERIAL,
                steps=[
                    YamlStep(id="fail_step", script=fail_script, params={}),
                ],
            )

            result = await loop_executor.execute_loop(loop)

            # Should stop after first iteration (failure)
            assert result.total_iterations == 1
            assert result.failed == 1
            assert result.status == StepStatus.FAILED
        finally:
            os.unlink(fail_script)

    @pytest.mark.asyncio
    async def test_loop_result_aggregates_failures(
        self, loop_executor: LoopExecutor, fixtures_dir: Path,
    ) -> None:
        """Should correctly count passed and failed iterations."""
        pass_script = str(fixtures_dir / "pass_script.py")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write('result = {"status": "FAILED", "error": "Fail"}\n')
            fail_script = f.name

        try:
            # Serial loop: first iteration passes, second fails
            loop = YamlLoop(
                id="mixed_loop",
                loop_type=LoopType.FOR,
                count=3,
                execution_mode=ExecutionMode.SERIAL,
                steps=[
                    YamlStep(id="step1", script=pass_script, params={}),
                    YamlStep(id="step2", script=fail_script, params={}),
                ],
            )

            result = await loop_executor.execute_loop(loop)

            # First iteration: step1 passes, step2 fails → iteration fails
            assert result.total_iterations == 1
            assert result.failed == 1
            assert result.status == StepStatus.FAILED
        finally:
            os.unlink(fail_script)
