"""Task 21 — retirement of the debugpy line-debugger stub and dead chain.

The non-functional debugpy line debugger (``ate_platform.debug``) and the
orphan cloud debug-CRUD router (``ate_cloud.api.v1.debug``) were deleted.
The LIVE breakpoint subsystems stay intact:

- T39 typed sim breakpoints + T40 step mode (``executions`` router CRUD,
  ``ate_cloud.services.breakpoint_registry``)
- task 18 DSL ``type: breakpoint`` step + task 20 edge breakpoints
- the persisted ``breakpoints`` table / ``models/breakpoint.py``
- ``ate_cloud.services.traceback_analyzer.DebugProcessExecutor`` (an
  unrelated traceback-capture wrapper that merely shares the class name)

These tests lock the removal: importing the retired modules must fail, the
frontend API client must be gone, and the live surface must still import.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

RETIRED_MODULES = [
    "ate_platform.debug",
    "ate_platform.debug.debug_executor",
    "ate_platform.debug.breakpoint_manager",
    "ate_cloud.api.v1.debug",
]


@pytest.mark.parametrize("module_name", RETIRED_MODULES)
def test_retired_debug_modules_cannot_be_imported(module_name: str) -> None:
    """Given a retired debug module name, importing it raises ModuleNotFoundError."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


def test_frontend_debug_api_client_removed() -> None:
    """The orphan frontend debug API client file no longer exists."""
    assert not (REPO_ROOT / "frontend" / "src" / "api" / "debug.ts").exists()


def test_debug_router_not_mounted() -> None:
    """The v1 router aggregation no longer references a debug router."""
    router_source = (
        REPO_ROOT / "src" / "ate_cloud" / "api" / "v1" / "router.py"
    ).read_text(encoding="utf-8")
    assert "debug_router" not in router_source
    assert "api.v1.debug" not in router_source


def test_live_breakpoint_surface_still_importable() -> None:
    """T39/T40 + edge breakpoint modules survive the retirement."""
    importlib.import_module("ate_cloud.models.breakpoint")
    importlib.import_module("ate_cloud.services.breakpoint_registry")
    importlib.import_module("ate_cloud.api.v1.executions")
    importlib.import_module("ate_platform.scheduler.edge_breakpoints")
    importlib.import_module("ate_platform.executor.v32_dispatcher")
    # The unrelated traceback-capture wrapper is NOT the retired debugpy class.
    traceback_mod = importlib.import_module("ate_cloud.services.traceback_analyzer")
    assert hasattr(traceback_mod, "DebugProcessExecutor")
