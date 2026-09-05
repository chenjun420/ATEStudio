"""conftest for end-to-end tests.

The ``openhtf/`` subdirectory contains OpenHTF E2E tests that require the
optional ``openhtf`` extra (``uv sync --extra openhtf``). That subpackage
hard-imports ``openhtf`` at collection time, so when the extra is not
installed it would ERROR collection of the whole e2e suite (and the default
``pytest`` gate) rather than skip cleanly.

Mirror the gate used in ``tests/integration/conftest.py`` (DEBT-9): skip the
subpackage at collection time when the extra is absent. The tests still
collect and run normally wherever ``--extra openhtf`` is installed.
"""

from __future__ import annotations

from importlib.util import find_spec

# When the optional openhtf extra is not installed, do not collect its e2e
# subpackage, so a default `pytest` run never errors on a missing optional
# dependency. With the extra present, everything collects normally.
collect_ignore: list[str] = []
if find_spec("openhtf") is None:
    collect_ignore = ["openhtf"]
