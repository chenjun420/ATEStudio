"""Real multiprocess path for the InstrumentProxy (V3.2 架构核心).

Runs the proxy as a genuine ``multiprocessing.Process`` (Windows spawn).
Validates that the ``serve`` module-level entry point, the HAL/MAL driver
pairing inside the child process, and the IPC protocol all work across the
process boundary.

These tests are slower than the inline-mode suite (process spawn per test)
and may be flaky on heavily loaded CI runners, so they are a small focused
subset of the inline coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ate_platform.proxy import ProxyManager

INSTRUMENT_CONFIG = {
    "instruments": {
        "DMM_CH1": {"type": "DMM"},
        "PSU_1": {"type": "PSU"},
    }
}


@pytest.fixture
def manager(tmp_path: Path) -> ProxyManager:
    """Start a real proxy Process in simulation mode for each test."""
    m = ProxyManager(INSTRUMENT_CONFIG, simulation=True, log_dir=str(tmp_path))
    m.start()
    yield m
    m.stop()


def test_query_across_process_boundary(manager: ProxyManager) -> None:
    """A query travels through the real process boundary and returns data."""
    client = manager.client("DMM_CH1")
    client.connect("MOCK::DMM")
    response = client.query("MEAS:VOLT:DC?")
    assert float(response) > 0


def test_method_forwarding_across_process(manager: ProxyManager) -> None:
    """Semantic methods (MAL layer) work across the process boundary."""
    client = manager.client("DMM_CH1")
    client.connect("MOCK::DMM")
    value = client.measure_voltage()
    assert isinstance(value, float)


def test_error_propagates_across_process(manager: ProxyManager) -> None:
    """Driver errors raised in the child process surface as RuntimeError."""
    client = manager.client("PSU_1")
    with pytest.raises(RuntimeError, match="Not connected"):
        client.query("OUTP?")


def test_chroma_eload_mock_across_process() -> None:
    """自研 Chroma 电子负载 Mock 经真实代理进程端到端调用。"""
    config = {"instruments": {"ELOAD_1": {"type": "ELOAD"}}}
    m = ProxyManager(config, simulation=True, log_dir="data/recordings")
    m.start()
    try:
        client = m.client("ELOAD_1")
        client.connect("MOCK::ELOAD")
        client.set_load_current(3.0)
        client.enable_load(True)
        assert client.measure_current() == 3.0  # mock 跟踪负载状态
        assert isinstance(client.measure_voltage(), float)
    finally:
        m.stop()
