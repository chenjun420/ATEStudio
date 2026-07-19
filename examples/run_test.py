"""Test script runner for ATE Platform examples.

This script allows running example test scripts standalone for testing
and development purposes.

Usage:
    python -m examples.run_test <script_name> [--mock]
    
Examples:
    python -m examples.run_test voltage_test --mock
    python -m examples.run_test current_test --mock
    python -m examples.run_test power_on_test --mock
"""

import argparse
import importlib.util
import sys
from pathlib import Path

from ate_platform.executor.context_proxy import ContextProxy
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.variable_space import VariableSpace
from ate_platform.types import StepResult, StepStatus


def run_test(script_name: str, mock: bool = True) -> StepResult:
    """Run a test script and return the result.
    
    Args:
        script_name: Name of the script (without .py extension).
        mock: Whether to run in mock mode (default: True).
        
    Returns:
        StepResult from the script execution.
        
    Raises:
        FileNotFoundError: If script doesn't exist.
        ImportError: If script can't be imported.
    """
    # Resolve script path
    scripts_dir = Path(__file__).parent / "scripts"
    script_path = scripts_dir / f"{script_name}.py"
    
    if not script_path.exists():
        available = [f.stem for f in scripts_dir.glob("*.py") if f.name != "__init__.py"]
        msg = f"Script '{script_name}' not found. Available scripts: {available}"
        raise FileNotFoundError(msg)
    
    # Load script as module
    spec = importlib.util.spec_from_file_location(script_name, script_path)
    if spec is None or spec.loader is None:
        msg = f"Could not load script: {script_path}"
        raise ImportError(msg)
    
    module = importlib.util.module_from_spec(spec)
    sys.modules[script_name] = module
    spec.loader.exec_module(module)
    
    # Check for main function
    if not hasattr(module, "main"):
        msg = f"Script '{script_name}' has no main() function"
        raise AttributeError(msg)
    
    # Create execution context
    variable_space = VariableSpace()
    resource_manager = ResourceManager()
    
    # Set mock mode flag (global variables are read-only from set(), use set_global)
    variable_space.set_global("mock_mode", mock)
    
    context = ContextProxy(
        _variable_space=variable_space,
        _resource_manager=resource_manager,
        _step_id=script_name
    )
    
    # Execute test
    print(f"\n{'='*60}")
    print(f"Running: {script_name}")
    print(f"Mock mode: {mock}")
    print(f"{'='*60}\n")
    
    try:
        result = module.main(context)
        
        if isinstance(result, StepResult):
            return result
        
        # If function returns None but didn't raise, assume pass
        return StepResult(
            status=StepStatus.PASSED,
            outputs=context.get_outputs()
        )
        
    except Exception as e:
        return StepResult(
            status=StepStatus.ERROR,
            outputs=context.get_outputs(),
            error=str(e)
        )


def main() -> None:
    """Main entry point for test runner."""
    parser = argparse.ArgumentParser(
        description="Run ATE Platform example test scripts"
    )
    parser.add_argument(
        "script",
        help="Name of the test script to run (e.g., voltage_test)"
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        default=True,
        help="Run in mock mode (default: True)"
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="Run with real hardware (overrides --mock)"
    )
    
    args = parser.parse_args()
    mock_mode = not args.real
    
    try:
        result = run_test(args.script, mock=mock_mode)
        
        # Print results
        print(f"\n{'='*60}")
        print(f"RESULT: {result.status.value}")
        print(f"{'='*60}")
        
        if result.outputs:
            print("\nOutputs:")
            for key, value in result.outputs.items():
                if isinstance(value, float):
                    print(f"  {key}: {value:.6f}")
                else:
                    print(f"  {key}: {value}")
        
        if result.error:
            print(f"\nError: {result.error}")
        
        # Exit with appropriate code
        sys.exit(0 if result.status == StepStatus.PASSED else 1)
        
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(2)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(3)


if __name__ == "__main__":
    main()