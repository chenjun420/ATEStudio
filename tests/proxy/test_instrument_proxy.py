"""Tests for the InstrumentProxy (仪器代理进程, V3.2 架构核心).

Covers:
- End-to-end IPC: script-side client talks to proxy process (inline mode)
- Per-instrument lock: concurrent access to one instrument is serialized
- Multi-client / multi-instrument concurrency
- Error propagation across the IPC boundary
- Generic method forwarding (call_method / __getattr__)
- Call recording (JSONL log files)

Inline mode runs the proxy logic in a background thread (same process), which
is sufficient to validate the IPC protocol, locks, recording, and dispatch.
The multiprocessing.Process path is validated in test_instrument_proxy_process.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from ate_platform.proxy import ProxyManager

# 每台仪器的模拟配置（simulation=True 走 Mock 驱动）
INSTRUMENT_CONFIG = {
    "instruments": {
        "DMM_CH1": {"type": "DMM"},
        "DMM_CH2": {"type": "DMM"},
        "PSU_1": {"type": "PSU"},
    }
}


@pytest.fixture
def manager(tmp_path: Path) -> ProxyManager:
    """Start an inline ProxyManager in simulation mode for each test."""
    m = ProxyManager(INSTRUMENT_CONFIG, simulation=True, log_dir=str(tmp_path))
    m.start()
    yield m
    m.stop()


def test_client_query_through_proxy(manager: ProxyManager) -> None:
    """A client query is forwarded to the proxy and returns a response."""
    client = manager.client("DMM_CH1")
    client.connect("MOCK::DMM")
    response = client.query("MEAS:VOLT:DC?")
    # Mock driver returns a float in scientific notation
    assert float(response) > 0


def test_client_write_through_proxy(manager: ProxyManager) -> None:
    """Write commands pass through without error."""
    client = manager.client("DMM_CH1")
    client.connect("MOCK::DMM")
    client.write("CONF:VOLT:DC 10")
    response = client.query("MEAS:VOLT:DC?")
    assert float(response) > 0


def test_error_propagates_across_ipc(manager: ProxyManager) -> None:
    """Driver errors are raised as RuntimeError on the client side."""
    client = manager.client("DMM_CH1")
    # Query without connect → mock driver raises RuntimeError inside the proxy
    with pytest.raises(RuntimeError, match="Not connected"):
        client.query("MEAS:VOLT:DC?")


def test_unknown_instrument_returns_error(manager: ProxyManager) -> None:
    """Requests for an unknown resource fail cleanly."""
    client = manager.client("NO_SUCH_INSTRUMENT")
    with pytest.raises(RuntimeError, match="Unknown instrument"):
        client.query("MEAS:VOLT:DC?")


def test_method_forwarding_via_getattr(manager: ProxyManager) -> None:
    """Generic method forwarding: client.measure_voltage() → proxy method call."""
    client = manager.client("DMM_CH1")
    client.connect("MOCK::DMM")
    value = client.measure_voltage()
    assert isinstance(value, float)


def test_call_method_explicit(manager: ProxyManager) -> None:
    """call_method() forwards by name."""
    client = manager.client("DMM_CH1")
    client.connect("MOCK::DMM")
    value = client.call_method("measure_voltage")
    assert isinstance(value, float)


def test_per_instrument_lock_serializes(manager: ProxyManager) -> None:
    """Concurrent access to the SAME instrument is serialized by the proxy.

    Multiple client threads hammer one instrument; the proxy's per-instrument
    lock ensures responses are correctly matched to requests (no cross-talk).
    """
    client = manager.client("DMM_CH1")
    client.connect("MOCK::DMM")

    results: list[float] = []
    errors: list[Exception] = []

    def _worker() -> None:
        try:
            for _ in range(20):
                value = client.query("MEAS:VOLT:DC?")
                results.append(float(value))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    assert len(results) == 100  # 5 threads × 20 queries


def test_multi_instrument_concurrency(manager: ProxyManager) -> None:
    """Different instruments can be operated concurrently without deadlock."""
    dmm = manager.client("DMM_CH1")
    psu = manager.client("PSU_1")
    dmm.connect("MOCK::DMM")
    psu.connect("MOCK::PSU")

    dmm_results: list[str] = []
    psu_results: list[str] = []
    errors: list[Exception] = []

    def _dmm_worker() -> None:
        try:
            for _ in range(15):
                dmm_results.append(dmm.query("MEAS:VOLT:DC?"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def _psu_worker() -> None:
        try:
            for _ in range(15):
                psu_results.append(psu.query("OUTP?"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_dmm_worker), threading.Thread(target=_psu_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    assert len(dmm_results) == 15
    assert len(psu_results) == 15


def test_recording_writes_jsonl(manager: ProxyManager, tmp_path: Path) -> None:
    """Every proxy call is recorded to a JSONL file."""
    client = manager.client("DMM_CH1")
    client.connect("MOCK::DMM")
    client.query("MEAS:VOLT:DC?")

    log_files = list(Path(tmp_path).glob("recording_*.jsonl"))
    assert log_files, "expected at least one recording JSONL file"
    content = log_files[0].read_text(encoding="utf-8")
    assert "MEAS:VOLT:DC?" in content
    assert "resource_id" in content


def test_client_timeout_after_stop(manager: ProxyManager) -> None:
    """After the proxy stops, calls fail with an error rather than hanging."""
    client = manager.client("DMM_CH1")
    client.connect("MOCK::DMM")
    manager.stop()
    with pytest.raises((RuntimeError, EOFError)):
        client.query("MEAS:VOLT:DC?")
