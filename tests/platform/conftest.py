"""Conftest for platform tests.

Ensures the project root is on ``sys.path`` so test helpers can be imported
via dotted paths (``from tests.platform.helpers_mock_tcp import ...``).

pytest 9 adds the ``tests/`` directory itself to ``sys.path`` rather than
the project root, so ``import tests...`` fails without this adjustment.
"""

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
