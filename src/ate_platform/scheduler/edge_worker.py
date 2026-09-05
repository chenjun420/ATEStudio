"""Edge worker entry point — env-configured JetStreamWorker for a station node.

Thin process entry around :class:`~ate_platform.scheduler.jetstream_worker.
JetStreamWorker` so a 工位/execution node can be launched with environment
variables alone (task 33):

    python -m ate_platform.scheduler.edge_worker

Configuration is parsed ONCE at the process boundary (:func:`resolve_config`):

- ``ATE_PLATFORM_NATS_URL`` — NATS JetStream URL of the cloud/debug server
  (e.g. ``nats://192.168.5.24:4222``). Defaults to
  ``nats://localhost:4222`` — the same default JetStreamWorker uses, so
  behaviour is unchanged when the variable is absent.
- ``ATE_PLATFORM_DATA_DIR`` — edge-local state directory (persisted worker
  id, and home for the SQLite offline caches / crash snapshots). Defaults
  to ``~/.ate_platform``.
- ``ATE_SIMULATION_MODE`` — ``true`` for virtual/mock instruments (no real
  hardware). The worker process itself does not touch hardware; test
  scripts/drivers inherit the variable and choose mock drivers
  (MockDriverFactory). It is parsed here so the operating mode is logged
  honestly at startup.

NATS reachability is fail-soft: a failed connection (server down, network
blip) is logged and retried forever — the station process never exits on a
connect error. This mirrors the edge offline-autonomy design (§10.5).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import nats

from .jetstream_worker import JetStreamWorker

logger = logging.getLogger(__name__)

#: Env override for the NATS URL the edge worker connects to (task 33).
ENV_NATS_URL = "ATE_PLATFORM_NATS_URL"

#: Env override for the edge-local state/data directory.
ENV_DATA_DIR = "ATE_PLATFORM_DATA_DIR"

#: Simulation flag (shared with the cloud config and docker-compose dev
#: profile): true ⇒ mock instruments, no hardware access.
ENV_SIMULATION_MODE = "ATE_SIMULATION_MODE"

#: Default NATS URL — identical to JetStreamWorker's own constructor default.
DEFAULT_NATS_URL = "nats://localhost:4222"

#: Default edge data dir — matches JetStreamWorker's ~/.ate_platform/worker_id.
DEFAULT_DATA_DIR = str(Path.home() / ".ate_platform")

_FETCH_TIMEOUT_SECONDS = 30.0
_RECONNECT_DELAY_SECONDS = 5.0
#: Hard outer timeout for the initial connect. nats-py's own connect_timeout
#: does not always fire on silent packet loss (see offline/event_buffer.py),
#: so wait_for is the backstop that keeps a dead server from stalling startup.
_CONNECT_HARD_TIMEOUT_SECONDS = 10.0
#: Infinite background auto-reconnect once connected (same settings as
#: LeafNodeRunner): transient WAN drops heal inside nats-py; the error_cb is
#: silenced so retries log one clean line instead of a traceback storm.
_MAX_RECONNECT_ATTEMPTS = -1


async def _silent_nats_error(exc: Exception) -> None:
    """nats-py error callback: silence the default per-retry traceback dump.

    The reconnect/monitor path logs clean lines; nats-py's default error_cb
    would print a full traceback on every retry and fill the station log
    (same rationale as offline/event_buffer.py).
    """
    logger.debug("nats-py connection error (auto-reconnecting): %s", exc)


async def _on_disconnected() -> None:
    logger.warning("NATS disconnected — nats-py is auto-reconnecting in the background")


async def _on_reconnected() -> None:
    logger.info("NATS reconnected")


async def connect_nats(url: str) -> Any:
    """Open a fail-soft NATS connection (short hard timeout, silent retries).

    Raises on initial-connect failure so :func:`run` can log one warning and
    retry — never a cascade of tracebacks. Once connected, nats-py retries
    dropped connections in the background forever.
    """
    return await asyncio.wait_for(
        nats.connect(
            url,
            connect_timeout=_CONNECT_HARD_TIMEOUT_SECONDS,
            allow_reconnect=True,
            max_reconnect_attempts=_MAX_RECONNECT_ATTEMPTS,
            reconnect_time_wait=_RECONNECT_DELAY_SECONDS,
            error_cb=_silent_nats_error,
            disconnected_cb=_on_disconnected,
            reconnected_cb=_on_reconnected,
        ),
        timeout=_CONNECT_HARD_TIMEOUT_SECONDS,
    )


@dataclass(frozen=True, slots=True)
class EdgeWorkerConfig:
    """Parsed edge-node configuration (boundary: environment → typed value)."""

    nats_url: str
    data_dir: str
    simulation: bool


def _env_flag(raw: str | None, default: bool) -> bool:
    """Parse a boolean env value (1/true/yes/on ⇒ true); blank ⇒ default."""
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def resolve_config(env: Mapping[str, str] | None = None) -> EdgeWorkerConfig:
    """Parse edge-worker configuration from the environment (single boundary).

    Blank/empty values fall back to defaults so an accidentally exported
    empty variable never points the worker at ``nats://`` or ``""``.

    Args:
        env: Environment mapping to read (defaults to :data:`os.environ`);
            injected by tests for hermeticity.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    nats_url = (source.get(ENV_NATS_URL) or "").strip() or DEFAULT_NATS_URL
    data_dir = (source.get(ENV_DATA_DIR) or "").strip() or DEFAULT_DATA_DIR
    return EdgeWorkerConfig(
        nats_url=nats_url,
        data_dir=data_dir,
        simulation=_env_flag(source.get(ENV_SIMULATION_MODE), default=False),
    )


def build_worker(config: EdgeWorkerConfig | None = None) -> JetStreamWorker:
    """Construct the edge worker from parsed configuration.

    Only the NATS URL and persisted-worker-id path are derived here; crash
    snapshots stay opt-in via the existing ``ATE_PLATFORM_SNAPSHOT_DIR``
    variable (JetStreamWorker reads it itself), so default behaviour is
    unchanged when nothing is configured.
    """
    cfg = config or resolve_config()
    return JetStreamWorker(
        nats_url=cfg.nats_url,
        worker_id_path=str(Path(cfg.data_dir) / "worker_id"),
    )


async def run(
    worker: JetStreamWorker | None = None,
    *,
    connector: Callable[[str], Awaitable[Any]] | None = None,
) -> None:
    """Run the edge worker forever, retrying NATS connectivity without crashing.

    The NATS connection is owned here (not by JetStreamWorker) so a failed
    connect (server down, network blip) logs ONE clean warning and retries
    after a short delay instead of exiting or spraying tracebacks — the
    station process stays up across server/network outages. Errors during the
    pull loop likewise trigger a clean stop + reconnect cycle.

    Args:
        worker: Worker to drive (defaults to one built from the environment).
        connector: NATS connector seam (defaults to :func:`connect_nats`;
            tests inject a fake).
    """
    cfg = resolve_config()
    connect = connector or connect_nats
    worker = worker or build_worker(cfg)

    while True:
        try:
            nc = await connect(cfg.nats_url)
        except Exception as exc:  # process boundary: unreachable ⇒ retry, never exit
            logger.warning(
                "Edge worker NATS connect to %s failed (%s) — retrying in %.0fs",
                cfg.nats_url,
                exc or "connect timeout",
                _RECONNECT_DELAY_SECONDS,
            )
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
            continue

        try:
            await worker.start(nc=nc)
        except Exception as exc:  # start must not kill the process — reconnect
            logger.warning("Edge worker start failed (%s) — retrying", exc)
            try:
                await nc.close()
            except Exception:
                pass
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
            continue

        logger.info("Edge worker %s connected to %s; waiting for tasks", worker.worker_id, cfg.nats_url)
        try:
            while True:
                await worker.pull_and_process_one(timeout=_FETCH_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            await worker.stop()
            raise
        except Exception as exc:  # process boundary: mid-run drop ⇒ reconnect loop
            logger.warning("Edge worker loop error: %s — reconnecting", exc)
            try:
                await worker.stop()
            except Exception as stop_exc:  # stop is best-effort teardown
                logger.warning("Edge worker stop during reconnect failed: %s", stop_exc)
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)


def main() -> None:
    """Console-script entry: log config and run until cancelled."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = resolve_config()
    logger.info(
        "Starting edge worker (nats_url=%s, data_dir=%s, simulation=%s)",
        config.nats_url,
        config.data_dir,
        config.simulation,
    )
    asyncio.run(run(build_worker(config)))


if __name__ == "__main__":
    main()
