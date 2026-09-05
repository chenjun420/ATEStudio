"""Unit tests for the env-driven edge worker entry (task 33).

Covers config resolution at the process boundary (default vs env override,
blank-value fallback, simulation flag parsing) and the fail-soft run loop
(unreachable NATS → log + retry, never crash).
"""

import asyncio
from pathlib import Path

import pytest

from ate_platform.scheduler import edge_worker
from ate_platform.scheduler.edge_worker import (
    DEFAULT_NATS_URL,
    EdgeWorkerConfig,
    build_worker,
    resolve_config,
    run,
)


def test_resolve_config_defaults_match_worker_default() -> None:
    # Given: an empty environment
    # When: configuration is resolved
    cfg = resolve_config({})
    # Then: the NATS default is identical to JetStreamWorker's own default,
    # simulation is off, and the data dir is the legacy ~/.ate_platform path.
    assert isinstance(cfg, EdgeWorkerConfig)
    assert cfg.nats_url == DEFAULT_NATS_URL == "nats://localhost:4222"
    assert cfg.simulation is False
    assert Path(cfg.data_dir) == Path.home() / ".ate_platform"


def test_resolve_config_env_override_points_at_debug_server() -> None:
    # Given: an environment targeting the 192.168.5.24 debug server
    env = {
        "ATE_PLATFORM_NATS_URL": "nats://192.168.5.24:4222",
        "ATE_PLATFORM_DATA_DIR": "C:/ate/edge-data",
        "ATE_SIMULATION_MODE": "true",
    }
    # When: configuration is resolved
    cfg = resolve_config(env)
    # Then: every value comes from the environment
    assert cfg.nats_url == "nats://192.168.5.24:4222"
    assert cfg.data_dir == "C:/ate/edge-data"
    assert cfg.simulation is True


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_resolve_config_blank_values_fall_back_to_defaults(raw: str) -> None:
    # Given: exported-but-empty variables (common shell accident)
    cfg = resolve_config({"ATE_PLATFORM_NATS_URL": raw, "ATE_PLATFORM_DATA_DIR": raw})
    # Then: blanks never produce nats://"" — defaults win
    assert cfg.nats_url == DEFAULT_NATS_URL
    assert Path(cfg.data_dir) == Path.home() / ".ate_platform"


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on", " true "])
def test_simulation_flag_truthy(truthy: str) -> None:
    assert resolve_config({"ATE_SIMULATION_MODE": truthy}).simulation is True


@pytest.mark.parametrize("falsy", ["0", "false", "no", "off", "garbage"])
def test_simulation_flag_falsy(falsy: str) -> None:
    assert resolve_config({"ATE_SIMULATION_MODE": falsy}).simulation is False


def test_build_worker_wires_nats_url_and_worker_id_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the debug-server environment
    monkeypatch.setenv("ATE_PLATFORM_NATS_URL", "nats://192.168.5.24:4222")
    monkeypatch.setenv("ATE_PLATFORM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ATE_PLATFORM_WORKER_ID_PATH", raising=False)
    # When: the worker is constructed
    worker = build_worker()
    # Then: it connects to .24 and persists its id inside the edge data dir
    assert worker._nats_url == "nats://192.168.5.24:4222"
    assert worker._worker_id_path == str(tmp_path / "worker_id")


def test_build_worker_without_env_keeps_legacy_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: no edge variables set at all
    monkeypatch.delenv("ATE_PLATFORM_NATS_URL", raising=False)
    monkeypatch.delenv("ATE_PLATFORM_DATA_DIR", raising=False)
    monkeypatch.delenv("ATE_PLATFORM_WORKER_ID_PATH", raising=False)
    # When: the worker is constructed
    worker = build_worker()
    # Then: behaviour is unchanged from the pre-task-33 default
    assert worker._nats_url == "nats://localhost:4222"
    assert Path(worker._worker_id_path) == Path.home() / ".ate_platform" / "worker_id"


class _FakeConn:
    """Minimal NATS connection double (worker.start stores it)."""


class _FakeWorker:
    """Test double standing in for JetStreamWorker in the run loop."""

    worker_id = "fake-edge-worker"

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.started_conns: list[object] = []

    async def start(self, nc: object | None = None) -> None:
        self.starts += 1
        self.started_conns.append(nc)

    async def pull_and_process_one(self, timeout: float) -> bool:
        return False

    async def stop(self) -> None:
        self.stops += 1


def _flaky_connector(failures: int):
    """Connector seam that fails `failures` times, then returns a connection."""
    state = {"attempts": 0}

    async def _connect(url: str) -> object:
        state["attempts"] += 1
        if state["attempts"] <= failures:
            raise OSError("nats: connection refused")
        return _FakeConn()

    return _connect


async def test_run_retries_unreachable_nats_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: NATS is unreachable on the first connect attempt, then comes up;
    # the pull loop is immediately cancelled (test shutdown signal).
    monkeypatch.setattr(edge_worker, "_RECONNECT_DELAY_SECONDS", 0.0)

    class _Worker(_FakeWorker):
        async def pull_and_process_one(self, timeout: float) -> bool:
            raise asyncio.CancelledError

    worker = _Worker()
    # When: the edge loop runs until cancelled
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            run(worker, connector=_flaky_connector(failures=1)), timeout=5.0,
        )
    # Then: the failed connect did NOT crash the process — it retried,
    # started with the real connection, and stop() ran on shutdown.
    assert worker.starts == 1
    assert worker.stops == 1
    assert isinstance(worker.started_conns[0], _FakeConn)


async def test_run_reconnects_after_mid_run_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: connect succeeds, but the pull loop drops mid-run once; the
    # second cycle ends with cancellation.
    monkeypatch.setattr(edge_worker, "_RECONNECT_DELAY_SECONDS", 0.0)

    class _Worker(_FakeWorker):
        def __init__(self) -> None:
            super().__init__()
            self._pulls = 0

        async def pull_and_process_one(self, timeout: float) -> bool:
            self._pulls += 1
            if self._pulls == 1:
                raise ConnectionError("nats: connection closed by peer")
            raise asyncio.CancelledError

    worker = _Worker()
    # When: the edge loop runs through a mid-run drop
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            run(worker, connector=_flaky_connector(failures=0)), timeout=5.0,
        )
    # Then: the loop stopped, reconnected (start twice), and stopped again
    # on shutdown — no crash, infinite-retries semantics preserved.
    assert worker.starts == 2
    assert worker.stops == 2
