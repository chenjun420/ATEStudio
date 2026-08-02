"""Conftest for OpenHTF E2E tests.

Ensures the project root is on ``sys.path`` so that
``OpenHTFStepExecutor`` can import fixture modules via
``importlib.import_module`` with a dotted module path
(``tests.e2e.openhtf.fixtures.pass_station``).

pytest 9 adds the ``tests/`` directory itself to ``sys.path`` rather than
the project root, so ``import tests...`` fails without this adjustment.
The conftest runs before any test in this directory is collected.

This sys.path fix also propagates to spawned child processes: the spawn
context pickles the parent's ``sys.path`` (via
``multiprocessing.spawn.get_preparation_data``) and restores it in the
child via ``multiprocessing.spawn.prepare``. So the timeout test, which
uses ``use_isolation=True``, can import the fixture module from the
child process.

openhtf preload:
    ``openhtf.__init__`` calls ``signal.signal(signal.SIGINT, ...)`` at
    module load time (line 145 in openhtf 1.6.1). This only works in the
    main thread of the main interpreter. ``execute_async`` runs
    ``execute`` in a worker thread via ``asyncio.to_thread``, and the
    fixture module's ``import openhtf`` would trigger the signal call in
    that worker thread, raising ``ValueError: signal only works in main
    thread of the main interpreter``. Pre-importing openhtf here (in the
    main thread, during conftest collection) ensures ``sys.modules``
    already contains it when the worker thread runs, so the import is a
    no-op in the worker thread.
"""

import sys
from pathlib import Path

import openhtf

_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Touch the attribute so ruff does not flag the import as unused; the
# import itself is the side effect (preloading openhtf in the main thread).
assert openhtf.__version__
