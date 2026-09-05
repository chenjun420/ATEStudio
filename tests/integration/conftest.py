"""conftest for REAL remote-service integration tests.

These tests exercise the REAL services on the debug server
(192.168.5.24 by default) with real clients (nats-py, qdrant-client,
falkordb, httpx). They are marker-gated and DEFAULT-SKIPPED:

* Every test module in this directory that talks to a remote service
  carries ``pytestmark = pytest.mark.integration``.
* The ``integration`` marker is registered in pyproject.toml
  ``[tool.pytest.ini_options]``.
* Unless ``ATE_RUN_INTEGRATION`` is set to a truthy value
  (1/true/yes/on), every marked test is SKIPPED with a human-readable
  reason — it is never silently deselected and never runs (or fails) in
  the normal fake/default suite.
* Even when the gate is open, each test skips independently when its
  specific service is unreachable/unconfigured, so a partial deployment
  yields skips, not failures.

Environment contract (ALL connection params and credentials come from
ENV — no secrets are ever hardcoded):

    ATE_RUN_INTEGRATION           master gate (default off)
    ATE_INTEGRATION_HOST          target host (default 192.168.5.24)
    ATE_INTEGRATION_NATS_PORT     NATS port      (default 4222)
    ATE_INTEGRATION_NATS_USER     NATS user      (optional; skip cred if unset)
    ATE_INTEGRATION_NATS_PASSWORD NATS password  (optional; skip cred if unset)
    ATE_INTEGRATION_QDRANT_PORT   Qdrant HTTP    (default 6333)
    ATE_INTEGRATION_QDRANT_API_KEY Qdrant API key (optional)
    ATE_INTEGRATION_FALKORDB_PORT RESP/FalkorDB  (default 6379)
    FALKORDB_URL / FALKORDB_PASSWORD  full overrides (match ate_cloud config)
    ATE_INTEGRATION_HTTP_PORT     nginx HTTP     (default 80)

The existing LOCAL integration tests in this directory (full-flow /
simulation) are NOT marked and are therefore unaffected by the gate.
"""

from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import dataclass
from importlib.util import find_spec

import pytest

# The openhtf integration tests live under this directory but require the
# optional `openhtf` extra (`uv sync --extra openhtf`). When that extra is not
# installed, skip the whole openhtf subpackage at collection time so the
# directory collection never ERRORS — the tests still run normally wherever the
# extra is present. This does NOT touch the marker gate below.
collect_ignore: list[str] = []
if find_spec("openhtf") is None:
    collect_ignore = ["openhtf"]

#: Master gate — integration tests run only when this is truthy.
RUN_ENV_VAR = "ATE_RUN_INTEGRATION"
#: Target host for the remote debug server.
HOST_ENV_VAR = "ATE_INTEGRATION_HOST"
DEFAULT_HOST = "192.168.5.24"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
PROBE_TIMEOUT_SECONDS = 3.0


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUTHY


def _env(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    return raw.strip() if raw and raw.strip() else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Marker gate — skip every `integration`-marked test unless the gate is open.
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip marker-gated tests by default; show them as skipped with a reason."""
    if _env_flag(RUN_ENV_VAR):
        return
    gate = pytest.mark.skip(
        reason=(
            f"real remote-service integration test; set {RUN_ENV_VAR}=1 "
            f"(and optionally {HOST_ENV_VAR}) to run against the debug server"
        )
    )
    for item in items:
        if item.get_closest_marker("integration") is not None:
            item.add_marker(gate)


# ---------------------------------------------------------------------------
# Targets — env-driven connection parameters (no secrets in files).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    """A reachable remote service endpoint."""

    host: str
    port: int
    url: str
    label: str

    @property
    def safe_url(self) -> str:
        """URL with any embedded credentials redacted (for logs/evidence)."""
        if "@" not in self.url:
            return self.url
        scheme, _, rest = self.url.partition("://")
        return f"{scheme}://***@{rest}"


async def tcp_reachable(host: str, port: int, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """True when a TCP connection to ``host:port`` completes within ``timeout``."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, TimeoutError, socket.gaierror):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


@pytest.fixture(scope="session")
def integration_host() -> str:
    return _env(HOST_ENV_VAR, DEFAULT_HOST)


@pytest.fixture(scope="session")
def nats_target(integration_host: str) -> Target:
    port = _env_int("ATE_INTEGRATION_NATS_PORT", 4222)
    return Target(
        host=integration_host,
        port=port,
        url=f"nats://{integration_host}:{port}",
        label="NATS JetStream",
    )


@pytest.fixture(scope="session")
def nats_credentials() -> tuple[str | None, str | None]:
    """Optional NATS user/password — absent creds mean no-auth (the .24 default)."""
    user = _env("ATE_INTEGRATION_NATS_USER")
    password = _env("ATE_INTEGRATION_NATS_PASSWORD")
    return (user or None, password or None)


@pytest.fixture(scope="session")
def qdrant_target(integration_host: str) -> Target:
    port = _env_int("ATE_INTEGRATION_QDRANT_PORT", 6333)
    return Target(
        host=integration_host,
        port=port,
        url=f"http://{integration_host}:{port}",
        label="Qdrant vector DB",
    )


@pytest.fixture(scope="session")
def qdrant_api_key() -> str | None:
    key = _env("ATE_INTEGRATION_QDRANT_API_KEY")
    return key or None


@pytest.fixture(scope="session")
def falkordb_target(integration_host: str) -> Target:
    port = _env_int("ATE_INTEGRATION_FALKORDB_PORT", 6379)
    # FALKORDB_URL (the ate_cloud config alias) wins when set; otherwise build
    # a redis:// URL from host/port. FalkorDB is NOT installed on .24 yet
    # (task 31); tests using this fixture skip until 6379 answers.
    url = _env("FALKORDB_URL") or f"redis://{integration_host}:{port}"
    host = integration_host
    fport = port
    if "://" in url:
        _, _, netloc = url.partition("://")
        hostport = netloc.rsplit("@", 1)[-1].split("/", 1)[0]
        if ":" in hostport:
            h, p = hostport.rsplit(":", 1)
            host, fport = h or host, int(p)
    return Target(host=host, port=fport, url=url, label="FalkorDB (RESP/6379)")


@pytest.fixture(scope="session")
def falkordb_password() -> str | None:
    password = _env("FALKORDB_PASSWORD")
    return password or None


@pytest.fixture(scope="session")
def cloud_http_target(integration_host: str) -> Target:
    port = _env_int("ATE_INTEGRATION_HTTP_PORT", 80)
    return Target(
        host=integration_host,
        port=port,
        url=f"http://{integration_host}:{port}",
        label="cloud nginx (ate_cloud via /api)",
    )


# ---------------------------------------------------------------------------
# Per-service skip gates — each test skips cleanly when its service is down.
# ---------------------------------------------------------------------------


async def _require_service(target: Target) -> Target:
    if await tcp_reachable(target.host, target.port):
        return target
    pytest.skip(
        f"{target.label} unreachable at {target.host}:{target.port} "
        f"(service not deployed/started, or no network route from this host)"
    )


@pytest.fixture
async def require_nats(nats_target: Target) -> Target:
    return await _require_service(nats_target)


@pytest.fixture
async def require_qdrant(qdrant_target: Target) -> Target:
    return await _require_service(qdrant_target)


@pytest.fixture
async def require_falkordb(falkordb_target: Target) -> Target:
    return await _require_service(falkordb_target)


@pytest.fixture
async def require_cloud_http(cloud_http_target: Target) -> Target:
    return await _require_service(cloud_http_target)
