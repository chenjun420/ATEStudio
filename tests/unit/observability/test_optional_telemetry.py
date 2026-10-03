"""The observability package must be importable without the OpenTelemetry SDK.

This is the shape of the defect that was live here until 2026-10-02:
``observability/__init__.py`` imported ``observability/telemetry.py`` at module
scope and ``telemetry.py`` imports ``opentelemetry`` unguarded, so
``import ate_cloud.observability`` raised ModuleNotFoundError on every host.
``opentelemetry`` is declared in neither pyproject.toml nor uv.lock and is
installed in no environment, including the deployed board, so that was every
host. The service was unaffected only because nothing in ``src/`` imports the
package — the one statement that could fail was never executed by startup.

The SDK's absence is the production condition, so these tests *force* it rather
than relying on it. A plain ``import`` test would keep passing if and when the
dependency is ever declared, and would then be asserting nothing at all about
the degradation path it appears to cover. Every subprocess test therefore
installs a meta-path blocker that makes ``import opentelemetry`` fail, which is
what makes the result independent of what happens to be in the venv.
"""

import subprocess
import sys

import pytest

# Makes `import opentelemetry` (and any submodule) raise, in a fresh interpreter.
_BLOCKER = """
import sys


class _BlockOpenTelemetry:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "opentelemetry" or fullname.startswith("opentelemetry."):
            raise ModuleNotFoundError(f"No module named '{fullname}'")
        return None


sys.meta_path.insert(0, _BlockOpenTelemetry())
"""

# Importing ate_cloud prints this; it is not what these tests are checking.
_IMPORT_NOISE = "SPA mounted at"


def run_without_sdk(body: str) -> subprocess.CompletedProcess[str]:
    """Run `body` in a subprocess where the OTel SDK cannot be imported."""
    return subprocess.run(
        [sys.executable, "-c", _BLOCKER + body],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_package_imports_without_the_sdk() -> None:
    """The failure this file exists for: ModuleNotFoundError on plain import."""
    proc = run_without_sdk(
        "import ate_cloud.observability as o\n"
        "assert o.get_logger('x') is not None\n"
        "print('IMPORT_OK')\n"
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "IMPORT_OK" in proc.stdout


def test_structlog_logging_works_without_the_sdk() -> None:
    """The eager half of the package must not need OTel at all.

    `get_logger` is what the rest of the service would actually want, and structlog
    is a declared dependency, so a host with no SDK still gets structured logs.
    """
    proc = run_without_sdk(
        "from ate_cloud.observability import get_logger, setup_structlog\n"
        "setup_structlog()\n"
        "get_logger('probe').info('hello', key='value')\n"
        "print('LOGGER_OK')\n"
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "LOGGER_OK" in proc.stdout
    # The record really was emitted, as JSON, by structlog.
    assert '"event": "hello"' in proc.stdout
    assert '"key": "value"' in proc.stdout
    # And with no SDK there are no trace fields — the degradation is visible in
    # the output rather than silent, so an operator can tell it happened.
    assert "trace_id" not in proc.stdout
    assert "span_id" not in proc.stdout


def test_telemetry_symbol_names_the_missing_package() -> None:
    """Asking for a telemetry function without the SDK must fail loudly.

    Rewriting this into a vaguer error would be the wrong trade: the caller asked
    a specific question and `No module named 'opentelemetry'` is the answer to it.
    A silent fallback here is what produced the original defect.
    """
    proc = run_without_sdk(
        "import ate_cloud.observability as o\n"
        "try:\n"
        "    o.setup_telemetry\n"
        "except ModuleNotFoundError as exc:\n"
        "    assert 'opentelemetry' in str(exc), exc\n"
        "    print('NAMED_OK')\n"
        "else:\n"
        "    raise AssertionError('expected ModuleNotFoundError')\n"
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "NAMED_OK" in proc.stdout


def test_telemetry_module_is_never_imported_by_the_package() -> None:
    """Importing the package must not drag in the OTel integration.

    This is the invariant that makes the two halves separable: if the eager import
    in `__init__.py` ever comes back, this fails even on a host that *does* have
    the SDK installed, which the other tests here cannot detect.
    """
    proc = run_without_sdk(
        "import sys\n"
        "import ate_cloud.observability  # noqa: F401\n"
        "leaked = [m for m in sys.modules if 'opentelemetry' in m]\n"
        "assert not leaked, leaked\n"
        "print('NOT_LEAKED_OK')\n"
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "NOT_LEAKED_OK" in proc.stdout


def test_unknown_attribute_is_not_forwarded_to_the_telemetry_module() -> None:
    """A typo must raise AttributeError about *this* package.

    Without the explicit name set in `_TELEMETRY_EXPORTS`, every miss would be
    forwarded to the telemetry module and reported as a missing attribute of an
    unrelated module, which sends the reader looking in the wrong file.
    """
    proc = run_without_sdk(
        "import ate_cloud.observability as o\n"
        "try:\n"
        "    o.setup_telemetr  # deliberate typo\n"
        "except AttributeError as exc:\n"
        "    assert 'ate_cloud.observability' in str(exc), exc\n"
        "    assert 'telemetry' not in str(exc), exc\n"
        "    print('TYPO_OK')\n"
        "else:\n"
        "    raise AssertionError('expected AttributeError')\n"
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "TYPO_OK" in proc.stdout


@pytest.mark.parametrize(
    "name",
    [
        "setup_telemetry",
        "shutdown_telemetry",
        "instrument_app",
        "instrument_httpx",
        "inject_context",
        "extract_context",
    ],
)
def test_every_telemetry_symbol_is_lazy_and_fails_without_the_sdk(name: str) -> None:
    """All six, not just the one in the docstring.

    A symbol added to `__all__` but missed in `_TELEMETRY_EXPORTS` would raise
    AttributeError instead of the ModuleNotFoundError that tells the caller what
    to install, so this is the per-symbol half of the consistency check below.
    """
    proc = run_without_sdk(
        "import ate_cloud.observability as o\n"
        f"try:\n    o.{name}\n"
        "except ModuleNotFoundError as exc:\n"
        "    assert 'opentelemetry' in str(exc), exc\n"
        "    print('LAZY_OK')\n"
        "else:\n"
        f"    raise AssertionError('{name} resolved without the SDK')\n"
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "LAZY_OK" in proc.stdout


def test_lazy_set_covers_exactly_the_non_eager_public_api() -> None:
    """Keep `__all__` and `_TELEMETRY_EXPORTS` from drifting apart.

    Without this, adding a function to the public API and forgetting the lazy set
    produces an AttributeError at runtime on every host, discovered by whoever
    calls it first.
    """
    import ate_cloud.observability as obs

    eager = {"get_logger", "setup_structlog"}
    assert set(obs.__all__) == eager | obs._TELEMETRY_EXPORTS, (
        f"__all__={sorted(obs.__all__)} lazy={sorted(obs._TELEMETRY_EXPORTS)}"
    )
    # `get_logger` and `setup_structlog` must stay eager: importing the package must
    # not require the SDK, and structlog is a declared dependency.
    for name in eager:
        assert name in vars(obs), f"{name} was moved behind the lazy loader"
