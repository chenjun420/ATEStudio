"""Declared dependencies must match what the code, the lock, and the venv agree on.

Three separate claims, each of which has been false in this repository at some
point:

1. Every module-level third-party import is a declared dependency.
   `structlog` was imported by 18 files, declared nowhere, and installed only
   because `semantica` depended on it. Removing semantica left the deployed
   service on 192.168.5.24 unable to import, with a traceback naming structlog
   and nothing about the dependency change that caused it. No test could have
   caught it: the local environment already had the package.

2. The lock file agrees with the installed environment.
   In ATERag, `uv sync --frozen` would have uninstalled `psycopg2-binary` — the
   hard dependency of `semantica.ApacheAgeStore`, and the only driver of
   `scripts/sync_semantica.py`, the script that maintains the real Apache AGE
   rule graph (384 vertices, 468 edges on the board). The package was in the
   venv, in neither pyproject.toml nor uv.lock. It had been there by hand.

3. A declared dependency is either imported, or loaded by name at runtime, or
   deliberately an entry point.
   "No import statement" is not sufficient grounds for removal. Three
   dependencies in ATEStudio have no import anywhere and are all load-bearing:
   `mcp` (langchain-mcp-adapters does `from mcp import ClientSession`),
   `pyvisa-py` (PyVISA's only VISA backend on a host without NI-VISA), and
   `aiomysql` (SQLAlchemy resolves the driver from the `mysql+aiomysql://` DSN
   that config.py builds). Removing any of them on the strength of a grep would
   have broken a capability that only fails when someone tries to use it.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: This repository's own top-level packages. `shared` is a third one alongside
#: ate_cloud / ate_platform, and omitting it produces a false positive that
#: reads exactly like a real finding.
OWN_PACKAGES = {"ate_cloud", "ate_platform", "shared"}

#: Imported inside try/except, or lazily inside a function whose caller has
#: already checked availability. Absence is a supported mode, not a break.
#: Keyed to the reason, so the list is reviewable rather than a hiding place.
OPTIONAL_BY_DESIGN = {
    "ortools": "guarded by _ORTOOLS_AVAILABLE with a documented heuristic fallback",
}

#: Module-level imports whose distribution is not declared, with the reason each
#: is currently safe. Keyed by *distribution* name, because that is what the
#: check compares against — keyed by module name it silently stops matching the
#: moment DISTRIBUTION_OF gains an entry, and the allowlist becomes a place that
#: hides things.
#:
#: Recorded rather than ignored: "currently safe" should be a claim someone can
#: re-check, not an assumption.
#:
#: `pydantic` and `structlog` are deliberately absent — they were on this list
#: in spirit until the knowledge-graph removal proved it, and both are declared
#: now with the history in the module docstring.
TRANSITIVE_BY_DESIGN = {
    "grpcio": "the gRPC instrument driver; no startup path imports it",
    "protobuf": "same gRPC layer; generated stubs only, and `google` maps here too",
    "numpy": "pulled in by the langchain/scientific stack already depended on",
    "opentelemetry": "observability/telemetry.py imports it unguarded, but "
                     "nothing imports that module — the telemetry setup has "
                     "never been wired in. A separate 'built but never "
                     "enabled' finding, not a dependency one",
}

#: Declared but never imported, because they are entry points or runtime-resolved.
#: The test below asserts the reason still holds, so the list cannot rot into a
#: place where genuinely dead dependencies hide.
RESOLVED_AT_RUNTIME = {
    "uvicorn": "the service is started with `uvicorn ate_cloud.main:app`",
    "alembic": "migrations are run with `alembic upgrade head`",
    "aiomysql": "SQLAlchemy resolves the driver from the mysql+aiomysql:// DSN "
                "that config.database_url builds",
    "mcp": "langchain-mcp-adapters does `from mcp import ClientSession`, and "
           "ATEStudio uses MultiServerMCPClient for the ATERag agent face",
    "pyvisa-py": "PyVISA's VISA backend. `ivi` (NI-VISA) is not installed, so "
                 "this is the only working backend on the host",
}

#: Declared, imported nowhere, and with no runtime role found. This is the list
#: that should be empty; anything added here needs a reason.
REMOVED = {
    "falkordb": "knowledge-graph backend, removed 2026-09-30, never deployed",
    "semantica": "knowledge-graph extraction, removed 2026-09-30, never executed",
    "bm25s": "BM25 is provided by PostgreSQL pg_textsearch; never imported",
    "markdown-it-py": "never imported anywhere",
}


#: Import name -> distribution name, where they differ. The distribution side is
#: compared after normalising dashes to underscores, so "nats-py" appears here
#: as "nats_py".
#:
#: A hand-written table is where a wrong answer hides, so it is kept minimal and
#: every entry is one where the import name genuinely differs from the
#: distribution name. `importlib.metadata.packages_distributions()` was tried as
#: the source and rejected: it does not report every mapping, so it would have
#: silently produced false "undeclared" findings.
DISTRIBUTION_OF = {
    "jwt": "pyjwt",
    "yaml": "pyyaml",
    "git": "gitpython",
    "grpc": "grpcio",
    "google": "protobuf",
    "nats": "nats_py",
    "langchain_core": "langchain_core",
    "langchain_openai": "langchain_openai",
    "langchain_mcp_adapters": "langchain_mcp_adapters",
    "pydantic_settings": "pydantic_settings",
    "qdrant_client": "qdrant_client",
    "sse_starlette": "sse_starlette",
    "pytest_asyncio": "pytest_asyncio",
}


def _norm(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(".", "_")


def _declared() -> dict[str, str]:
    """Every declared distribution, across all dependency groups.

    `pytest` lives in the `dev` extra and `openhtf` in its own extra, so reading
    only `dependencies` would report both as undeclared — which is how a test
    that claims to catch undeclared dependencies ends up crying wolf.
    """
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = pyproject["project"]
    specs: list[str] = list(project["dependencies"])
    for group in (project.get("optional-dependencies") or {}).values():
        specs.extend(group)

    out: dict[str, str] = {}
    for spec in specs:
        name = spec.split("[", 1)[0]
        for sep in (">=", "<=", "==", "~=", "!=", ">", "<", " ", ";"):
            name = name.split(sep, 1)[0]
        if name:
            out[_norm(name)] = spec
    return out


def _imports() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """-> (unguarded sites, all sites) for unguarded / all third-party imports."""
    unguarded: dict[str, set[str]] = {}
    everywhere: dict[str, set[str]] = {}
    stdlib = set(sys.stdlib_module_names)

    roots = [SRC]
    for extra in ("scripts", "tests"):
        if (REPO_ROOT / extra).is_dir():
            roots.append(REPO_ROOT / extra)

    for root_dir in roots:
        for path in sorted(root_dir.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()

            guarded: set[int] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Try):
                    for stmt in node.body:
                        guarded.update(id(i) for i in ast.walk(stmt))

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots_ = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots_ = [node.module.split(".")[0]]
                else:
                    continue
                for r in roots_:
                    if r in stdlib or r.startswith("_") or r in OWN_PACKAGES:
                        continue
                    # Local sibling directories that are not packages.
                    if (REPO_ROOT / r).is_dir() and not (REPO_ROOT / r / "__init__.py").exists():
                        continue
                    everywhere.setdefault(r, set()).add(rel)
                    if id(node) not in guarded:
                        unguarded.setdefault(r, set()).add(rel)

    return unguarded, everywhere


def test_unguarded_imports_are_declared() -> None:
    """The invariant that broke a deployed service."""
    declared = set(_declared())
    unguarded, _ = _imports()

    undeclared: dict[str, list[str]] = {}
    for module, sites in unguarded.items():
        dist = _norm(DISTRIBUTION_OF.get(module, module))
        if dist in declared or dist in TRANSITIVE_BY_DESIGN or module in OPTIONAL_BY_DESIGN:
            continue
        undeclared.setdefault(dist, []).extend(sites)

    assert not undeclared, (
        "Imported at module level but not in pyproject.toml, so installed only "
        "as someone else's transitive dependency — removing that dependency "
        "breaks startup:\n"
        + "\n".join(
            f"  {name:26} <- {', '.join(sorted(set(s))[:3])}"
            for name, s in sorted(undeclared.items())
        )
    )


@pytest.mark.parametrize("package", ["structlog", "pyarrow", "pydantic"])
def test_load_bearing_packages_are_declared(package: str) -> None:
    """Named individually so each regression has a named home.

    structlog: broke the deployed service outright.
    pyarrow: guarded, so nothing broke — Parquet export became CSV silently.
    pydantic: 53 module-level imports, was riding on fastapi's requirement.
    """
    assert _norm(package) in _declared(), (
        f"{package} is imported directly by src/ and must be a direct "
        "dependency, not a transitive one"
    )


def test_removed_dependencies_stay_removed() -> None:
    """A dependency nothing imports is a promise that a capability exists."""
    text = PYPROJECT.read_text(encoding="utf-8")
    stripped = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    for name, reason in REMOVED.items():
        assert _norm(name) not in stripped, (
            f"{name} is declared again ({reason})"
        )


def test_runtime_resolved_dependencies_still_have_their_reason() -> None:
    """The allowlist must keep describing reality.

    Each of these is declared without a single import statement. If the
    mechanism that made it load-bearing disappears — langchain-mcp-adapters
    dropping its `from mcp import ...`, NI-VISA becoming the only installed
    VISA backend, config.py dropping the mysql DSN — then it becomes an
    ordinary unused dependency and belongs in the removal path, not here.
    """
    source = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in SRC.rglob("*.py")
        if "__pycache__" not in p.parts
    )
    declared = _declared()

    for package in RESOLVED_AT_RUNTIME:
        assert _norm(package) in declared, (
            f"{package} is listed in RESOLVED_AT_RUNTIME but is not declared"
        )

    # mcp: the adapter that needs it must still be present and used.
    assert "MultiServerMCPClient" in source, (
        "mcp is declared because langchain-mcp-adapters imports it, but "
        "MultiServerMCPClient is no longer used — mcp is now an unused "
        "dependency and should be removed"
    )
    # aiomysql: config.py must still build a mysql+aiomysql:// DSN.
    assert "mysql+aiomysql" in source, (
        "aiomysql is declared because config.database_url builds a "
        "mysql+aiomysql:// DSN, but that string is gone — aiomysql is now an "
        "unused dependency and should be removed"
    )
    # pyvisa-py: pyvisa must still be used for instrument access.
    assert re.search(r"\bpyvisa\b", source), (
        "pyvisa-py is declared as the VISA backend, but pyvisa is no longer "
        "used anywhere — both are now removable"
    )


def test_optional_by_design_imports_stay_guarded() -> None:
    """Optional means guarded or lazy, never module-level and bare.

    fault_penalty.py imports cp_model inside a function; that is fine, because
    its caller already checked _ORTOOLS_AVAILABLE. A module-level unguarded
    import would turn "optional" into "required, with a startup traceback".
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if not any(name in source for name in OPTIONAL_BY_DESIGN):
            continue
        tree = ast.parse(source, filename=str(path))
        rel = path.relative_to(REPO_ROOT).as_posix()

        guarded: set[int] = set()
        nested: set[int] = set()
        for outer in ast.walk(tree):
            if isinstance(outer, ast.Try):
                for stmt in outer.body:
                    guarded.update(id(i) for i in ast.walk(stmt))
            elif isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for stmt in outer.body:
                    nested.update(id(i) for i in ast.walk(stmt))

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            if not any(n.split(".")[0] in OPTIONAL_BY_DESIGN for n in names):
                continue
            if id(node) not in guarded and id(node) not in nested:
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "OPTIONAL_BY_DESIGN packages are now imported at module level without a "
        "guard, which makes them required:\n" + "\n".join(f"  {o}" for o in offenders)
    )


@pytest.mark.slow
def test_lock_file_agrees_with_the_installed_environment() -> None:
    """`uv sync --frozen` must be a no-op.

    In ATERag this check would have failed: the venv held psycopg2-binary,
    which was in neither pyproject.toml nor uv.lock, so `uv sync --frozen` was
    one command away from removing the driver behind the script that maintains
    the Apache AGE rule graph. The drift was invisible because nothing runs
    `uv sync` and then tries that script.

    Skipped when uv is unavailable, and when the dev extra is not installed
    (the dry-run would then want to uninstall pytest and friends).
    """
    uv = None
    for candidate in ("uv", "uv.exe"):
        try:
            subprocess.run([candidate, "--version"], capture_output=True, check=True)
            uv = candidate
            break
        except (OSError, subprocess.CalledProcessError):
            continue
    if uv is None:
        pytest.skip("uv is not on PATH")

    proc = subprocess.run(
        [uv, "sync", "--frozen", "--extra", "dev", "--dry-run"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )
    removals = [
        line.strip()
        for line in proc.stdout.splitlines()
        if re.match(r"^\s*-\s+\S", line) and "aterag" not in line
    ]
    assert not removals, (
        "uv.lock and the installed environment disagree. Running `uv sync` "
        "would remove packages the code depends on:\n"
        + "\n".join(f"  {r}" for r in removals)
    )
