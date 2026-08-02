"""Conftest for OpenHTF integration tests.

Ensures the project root is on ``sys.path`` so that
``OpenHTFStepExecutor`` can import the fixtures module via
``importlib.import_module`` with a dotted module path
(``tests.integration.openhtf.fixtures.sample_test_module``).

pytest 9 adds the ``tests/`` directory itself to ``sys.path`` rather than
the project root, so ``import tests...`` fails without this adjustment.
The conftest runs before any test in this directory is collected.
"""

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
