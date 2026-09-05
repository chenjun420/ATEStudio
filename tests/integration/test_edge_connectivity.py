"""Real edge-node → cloud connectivity integration tests.

Verifies the edge worker (``ate_platform.scheduler.edge_worker``) can be
configured purely from environment (per config/edge-node.env.example /
scripts/deploy/run_edge_worker.sh) and reach the cloud services over the
network — NO secrets are written to any file, and NO cloud credentials
are needed (NATS/Qdrant on .24 are no-auth by default).

* ``resolve_config`` parses ``ATE_PLATFORM_NATS_URL`` into an
  :class:`EdgeWorkerConfig` (the edge's single env boundary).
* A REAL nats-py connect using the edge-resolved NATS URL proves the
  edge → cloud JetStream path the worker depends on.
* A TCP probe to the cloud Qdrant HTTP port proves the edge network path
  to the vector backend (Qdrant is consumed cloud-side; the edge shares
  the same network route).

Skipped by default (gate in conftest); skipped per-service when the
cloud host is unreachable from this machine.
"""

from __future__ import annotations

import nats
import pytest

from ate_platform.scheduler.edge_worker import resolve_config

from .conftest import tcp_reachable

pytestmark = pytest.mark.integration


async def test_edge_config_resolves_nats_url_from_env(
    nats_target, monkeypatch
) -> None:
    """Given the edge env contract, resolve_config points the worker at cloud NATS.

    Pure boundary parsing (no socket) — validates the env contract the edge
    node uses; runs whenever the integration gate is open.
    """
    # Mirror config/edge-node.env.example: edge node sets ATE_PLATFORM_NATS_URL.
    monkeypatch.setenv("ATE_PLATFORM_NATS_URL", nats_target.url)
    monkeypatch.setenv("ATE_SIMULATION_MODE", "true")

    config = resolve_config()
    assert config.nats_url == nats_target.url
    assert config.simulation is True


async def test_edge_worker_reaches_cloud_nats(require_nats, nats_target, monkeypatch) -> None:
    """Given edge env pointing at cloud NATS, a real connect + JetStream round-trip works."""
    monkeypatch.setenv("ATE_PLATFORM_NATS_URL", nats_target.url)
    config = resolve_config()

    nc = await nats.connect(config.nats_url, connect_timeout=5)
    try:
        js = nc.jetstream()
        # JetStream must answer — this is the fabric the edge worker pulls from.
        assert await js.account_info() is not None
    finally:
        await nc.close()


async def test_edge_network_path_to_cloud_qdrant(qdrant_target) -> None:
    """Given the cloud host, the edge network route to Qdrant HTTP (6333) is open.

    Skips (not fails) when the port is closed — e.g. before the cloud
    backend is reachable from this network.
    """
    if not await tcp_reachable(qdrant_target.host, qdrant_target.port):
        pytest.skip(
            f"edge → cloud Qdrant route {qdrant_target.host}:{qdrant_target.port} "
            "unreachable from this host"
        )
    # Reachable ⇒ the network path the edge node uses to reach the cloud
    # vector backend is open from this machine (same route the edge env
    # config points at via ATE_PLATFORM_NATS_URL / the shared cloud host).
