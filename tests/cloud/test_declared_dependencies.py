"""A module-level third-party import must be a declared dependency.

The invariant, precisely
------------------------
If `src/` imports a package at module level — not inside a function, not inside
a try/except — then the absence of that package stops the application from
importing at all. So that package has to be in `pyproject.toml`, not merely
present in the venv.

`state_snapshot.py` violated this. It did `import structlog` at module level,
`structlog` was never declared, and it reached the venv only because `semantica`
happened to depend on it. Removing the knowledge-graph subsystem removed
semantica, and the deployed service on 192.168.5.24 stopped importing — the
traceback pointed at structlog and said nothing about the dependency change
that caused it.

No test could have seen it: the local environment already had structlog, so
every test passed. Only `uv sync` against the new lock, on a machine that had
not already installed it, produced the failure. Hence this is a static check
against pyproject rather than an import test.

A quieter version of the same bug
--------------------------------
`report_exporter.py` imports `pyarrow` inside a try/except to offer Parquet
export and falls back to CSV. That guard is correct, and it is why the Parquet
path went from working to silently not working when pyarrow disappeared with
semantica: the fallback caught the ImportError, no test failed, and a caller
asking for Parquet received CSV. So guarded imports of a package the project
actually wants are also declared — see DECLARED_OPTIONAL.

Deliberately not declared
-------------------------
`ortools` is imported inside try/except with an explicit availability sentinel
and a documented degradation to heuristic scheduling. The guard is the
contract, so it stays undeclared, and OPTIONAL_BY_DESIGN keeps the
distinction honest by failing if such an import ever becomes unguarded.
"""
from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: The project's own top-level packages. `shared` is a fourth one alongside
#: ate_cloud / ate_platform, and forgetting it produces a false positive that
#: reads exactly like a real finding.
OWN_PACKAGES = {"ate_cloud", "ate_platform", "shared"}

#: Imported inside try/except with an explicit degradation path, so absence is
#: a supported mode rather than a break. Keyed to the reason it stays
#: undeclared, so the list is reviewable rather than a place to hide things.
OPTIONAL_BY_DESIGN = {
    "ortools": "guarded by _ORTOOLS_AVAILABLE with a documented heuristic fallback",
}

#: Imported at module level but not declared, and nothing has broken because of
#: it. Recorded with the reason it is currently safe, so that "currently safe"
#: is a claim someone can re-check rather than an assumption.
#:
#: `structlog` and `pyarrow` are deliberately NOT here — they were on this list
#: in spirit until the knowledge-graph removal proved it, and they are declared
#: now with the history in the module docstring.
TRANSITIVE_BY_DESIGN = {
    "pydantic": "guaranteed by pydantic-settings>=2.0.0 and fastapi>=0.110.0, "
                "both of which require it; removing either is a much larger change",
    "numpy": "pulled in by the langchain/scientific stack the project already "
             "depends on",
    "grpcio": "the gRPC instrument driver is a separate deployment concern; no "
              "startup path imports it",
    "protobuf": "same gRPC layer; generated stubs only",
    "google": "namespace package from protobuf, same gRPC layer",
    "opentelemetry": "observability/telemetry.py imports it unguarded, but "
                     "nothing imports that module — the telemetry setup has "
                     "never been wired into the app. A separate "
                     "'built but never enabled' finding, not a dependency one",
}

#: Guarded imports, but the project wants the capability, so it is declared.
DECLARED_OPTIONAL = {"pyarrow"}


def _declared_dependencies() -> set[str]:
    """Normalised distribution names from [project].dependencies."""
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    names: set[str] = set()
    for spec in pyproject["project"]["dependencies"]:
        name = spec.split("[", 1)[0]
        for separator in (">=", "<=", "==", "~=", "!=", ">", "<", " "):
            name = name.split(separator, 1)[0]
        if name:
            names.add(name.strip().lower().replace("-", "_"))
    return names


def _imported_modules(*, guarded_only: bool) -> dict[str, list[str]]:
    """Top-level third-party modules -> "file:line" sites.

    With ``guarded_only``, returns only imports nested inside a ``try`` block.
    """
    import sys

    stdlib = set(sys.stdlib_module_names)
    found: dict[str, list[str]] = {}

    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        rel = path.relative_to(SRC).as_posix()

        guarded_nodes: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for stmt in node.body:
                    guarded_nodes.update(id(inner) for inner in ast.walk(stmt))

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if guarded_only and id(node) not in guarded_nodes:
                continue
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif node.level == 0 and node.module:
                roots = [node.module.split(".")[0]]
            else:
                continue
            for root in roots:
                if root in stdlib or root.startswith("_") or root in OWN_PACKAGES:
                    continue
                found.setdefault(root, []).append(f"{rel}:{node.lineno}")

    return found


def test_unguarded_third_party_imports_are_declared() -> None:
    """Module-level imports must be declared, or startup is one `uv lock` away
    from failing. Reports every offender at once: finding these one per lock
    refresh is the entire cost this test exists to remove."""
    declared = _declared_dependencies()
    imported = _imported_modules(guarded_only=False)

    # Distribution name for a module that differs from it, and the cases where
    # the module name is the import name rather than a distribution.
    # Import name -> distribution name, where they differ. The distribution side
    # is compared after normalising dashes to underscores, so "nats-py" has to
    # appear here as "nats_py".
    distribution_of = {
        "jwt": "pyjwt",
        "yaml": "pyyaml",
        "grpc": "grpcio",
        "google": "protobuf",
        "nats": "nats_py",
        "qdrant_client": "qdrant_client",
        "sse_starlette": "sse_starlette",
        "git": "gitpython",
        "langchain_core": "langchain_core",
        "langchain_openai": "langchain_openai",
        "langchain_mcp_adapters": "langchain_mcp_adapters",
        "pydantic_settings": "pydantic_settings",
        "structlog": "structlog",
        "pyarrow": "pyarrow",
        "numpy": "numpy",
        "pydantic": "pydantic",
        "opentelemetry": "opentelemetry",
    }

    undeclared: dict[str, list[str]] = {}
    for module, sites in imported.items():
        distribution = distribution_of.get(module, module)
        if distribution in declared:
            continue
        if distribution in TRANSITIVE_BY_DESIGN or module in OPTIONAL_BY_DESIGN:
            continue
        undeclared.setdefault(distribution, []).extend(sites)

    assert not undeclared, (
        "These packages are imported at module level by src/ but are not in "
        "pyproject.toml, so they are only installed as someone else's "
        "transitive dependency. Removing that dependency breaks startup:\n"
        + "\n".join(
            f"  {name:26} <- {', '.join(sorted(set(sites))[:3])}"
            for name, sites in sorted(undeclared.items())
        )
    )


def test_structlog_is_declared() -> None:
    """The specific one that broke a deployed service.

    Named separately so the regression has a named home. If this ever fails
    again, the history is right here: it was imported at module level by
    state_snapshot.py, never declared, and present only because semantica
    depended on it.
    """
    assert "structlog" in _declared_dependencies(), (
        "structlog is imported at module level by 18 files under src/ and must "
        "be a direct dependency, not a transitive one"
    )


def test_pyarrow_is_declared_so_parquet_export_keeps_working() -> None:
    """Guarded, so nothing breaks — but the capability silently disappears.

    The try/except in report_exporter.py falls back to CSV, so a missing
    pyarrow turns Parquet export into CSV with only a log line to show for it.
    """
    assert "pyarrow" in _declared_dependencies(), (
        "pyarrow backs Parquet export; undeclared it silently degrades to CSV"
    )


def test_optional_by_design_imports_stay_guarded() -> None:
    """The allowlist must keep describing reality.

    A package claimed to degrade gracefully may be imported two ways and not a
    third: inside a try/except, or lazily inside a function whose caller has
    already checked availability. `fault_penalty.py` does the latter — it
    imports cp_model at call time, and it is only ever called from the CP-SAT
    path that `cpsat.py` already gated on _ORTOOLS_AVAILABLE.

    What must not happen is a module-level unguarded import, because that turns
    "optional" into "required, and the failure is a startup traceback". So
    every import of an allowlisted package is checked to be either guarded or
    nested inside a function.
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        rel = path.relative_to(SRC).as_posix()

        guarded: set[int] = set()
        nested: set[int] = set()
        for outer in ast.walk(tree):
            if isinstance(outer, ast.Try):
                for stmt in outer.body:
                    guarded.update(id(inner) for inner in ast.walk(stmt))
            elif isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for stmt in outer.body:
                    nested.update(id(inner) for inner in ast.walk(stmt))

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            imported = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            names = {name.split(".")[0] for name in imported}
            hit = names & set(OPTIONAL_BY_DESIGN)
            if not hit:
                continue
            if id(node) not in guarded and id(node) not in nested:
                offenders.append(
                    f"{rel}:{node.lineno} {sorted(hit)[0]} — module level, "
                    "no try/except"
                )

    assert not offenders, (
        "OPTIONAL_BY_DESIGN claims these degrade gracefully, so they cannot be "
        "imported at module level without a guard — that makes them required, "
        "and the failure mode is a startup traceback rather than a fallback:\n"
        + "\n".join(f"  {o}" for o in offenders)
    )


def test_removed_graph_dependencies_are_not_still_declared() -> None:
    """A dependency nothing imports is a promise that a capability exists.

    falkordb and semantica were hard requirements for a subsystem removed on
    2026-09-30. Leaving them declared would invite the next reader to treat the
    knowledge graph as merely switched off rather than gone.
    """
    stripped = "\n".join(
        line
        for line in PYPROJECT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    for name in ("falkordb", "semantica"):
        assert name not in stripped, (
            f"{name} is still a declared dependency, but the subsystem that used "
            "it was removed"
        )


def test_nothing_imports_the_removed_subsystem() -> None:
    """Deleting a package is not the same as nobody importing it.

    Checked through the AST rather than by grepping source text, because the
    surviving modules carry prose that names the removed ones — and prose
    mentioning a deleted module is a note to the reader, not a broken import.
    """
    removed = (
        "falkordb_graph_service",
        "graph_service",
        "graph_browse",
        "kg_evolution",
        "kg_retrieval",
        "kg_pipeline",
        "kg_seeder",
        "kg_seed_data",
        "kg_seed_facts",
        "kg_seed_writer",
        "failure_evolution",
        "ontology",
        "knowledge_extraction",
        "fault_symptom_vector_store",
    )
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = [node.module]
            else:
                continue
            for name in roots:
                if any(name == f"ate_cloud.services.{mod}" or name == f"ate_cloud.api.v1.{mod}"
                       for mod in removed):
                    offenders.append(f"{path.relative_to(SRC).as_posix()}:{node.lineno} -> {name}")

    assert not offenders, (
        "Code still imports a module removed with the knowledge-graph subsystem:\n"
        + "\n".join(f"  {o}" for o in offenders)
    )


def test_removed_modules_are_actually_gone() -> None:
    """A guard that passes on a leftover file is worse than no guard."""
    services = SRC / "ate_cloud" / "services"
    for module in (
        "falkordb_graph_service",
        "graph_service",
        "graph_browse",
        "kg_evolution",
        "kg_retrieval",
        "kg_seeder",
        "kg_seed_data",
        "kg_seed_facts",
        "kg_seed_writer",
        "failure_evolution",
        "fault_symptom_vector_store",
    ):
        assert not (services / f"{module}.py").exists(), f"{module}.py is still in src/"

    for package in ("kg_pipeline", "ontology", "knowledge_extraction"):
        assert not (services / package).exists(), f"{package}/ is still in src/"

    api = SRC / "ate_cloud" / "api" / "v1"
    for module in ("faults", "knowledge"):
        assert not (api / f"{module}.py").exists(), f"api/v1/{module}.py is still in src/"
