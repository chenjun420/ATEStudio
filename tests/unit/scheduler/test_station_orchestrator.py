"""Tests for StationOrchestrator (T30 multi-station orchestration).

Verifies:
1. connect() - connects to NATS and acquires the KV bucket.
2. connect() - creates the KV bucket if it doesn't exist.
3. register_workflow() - writes the workflow definition to the correct KV key.
4. get_workflow() - reads back a registered workflow (round-trip).
5. get_workflow() - returns None for a missing workflow.
6. notify_done() - writes the handoff to session.{sid}.station.{stid}.done.
7. notify_done() - raises ValueError on session/station mismatch.
8. get_handoff() - reads back a handoff record (round-trip).
9. get_handoff() - returns None for a missing handoff.
10. wait_for_upstream() - returns DONE immediately if key already exists (pass).
11. wait_for_upstream() - returns FAILED if key exists with pass_fail=False.
12. wait_for_upstream() - returns TIMEOUT when no key appears in time.
13. wait_for_upstream() - returns DONE when the watcher yields a matching PUT.
14. wait_for_upstream() - skips None markers and DEL operations.
15. KV bucket name follows convention (ate-handoffs).
16. KV key pattern follows convention (session.{sid}.station.{stid}.done).
17. Errors raise RuntimeError (no silent degradation).
18. connect() raises if NATS connection fails.
19. Not-connected methods raise RuntimeError.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.errors import KeyNotFoundError, NotFoundError

from ate_platform.scheduler.station_orchestrator import (
    HANDOFF_KV_BUCKET,
    StationOrchestrator,
    _handoff_key,
    _workflow_key,
)
from shared.multi_station import (
    HandoffStatus,
    StationHandoff,
    StationWorkflow,
    StationWorkflowConfig,
    handoff_to_dict,
)

# ---------------------------------------------------------------------------
# Helpers (mirrors test_config_distribution.py patterns)
# ---------------------------------------------------------------------------


class FakeKVEntry:
    """Mimics nats-py KeyValue.Entry for testing."""

    def __init__(
        self,
        key: str,
        value: bytes | None,
        revision: int = 1,
        operation: str | None = None,
    ) -> None:
        self.bucket = HANDOFF_KV_BUCKET
        self.key = key
        self.value = value
        self.revision = revision
        self.delta = 0
        self.created = None
        self.operation = operation


class FakeKeyWatcher:
    """Mimics nats-py KeyWatcher async iterator for testing.

    Yields the provided entries in order. A ``None`` entry represents the
    end-of-initial-values marker. After all entries are consumed, the
    iterator blocks until ``stop()`` is called (matching nats-py behavior),
    unless ``block_on_empty=False`` in which case it ends immediately.
    """

    def __init__(
        self,
        entries: list[Any],
        block_on_empty: bool = True,
    ) -> None:
        self._entries = list(entries)
        self._block_on_empty = block_on_empty
        self._stop_called = False

    def __aiter__(self) -> FakeKeyWatcher:
        return self

    async def __anext__(self) -> Any:
        if self._entries:
            return self._entries.pop(0)
        if self._block_on_empty:
            # Block until stopped - simulates nats-py's queue-based behavior.
            while not self._stop_called:
                await asyncio.sleep(0.01)
            raise StopAsyncIteration
        # Non-blocking: iterator ends after entries are consumed.
        raise StopAsyncIteration

    async def stop(self) -> None:
        self._stop_called = True


def _make_mock_kv(
    put_return: int = 1,
    entries: dict[str, bytes] | None = None,
) -> MagicMock:
    """Build a mock KV store matching nats-py KeyValue API."""
    kv = MagicMock()
    kv.put = AsyncMock(return_value=put_return)
    kv.watch = AsyncMock(return_value=FakeKeyWatcher(entries=[], block_on_empty=True))

    _entries = entries or {}

    async def _get(key: str) -> Any:
        if key in _entries:
            return FakeKVEntry(key=key, value=_entries[key])
        raise KeyNotFoundError(f"key '{key}' not found")

    kv.get = AsyncMock(side_effect=_get)
    return kv


def _make_mock_nc_existing(kv: MagicMock) -> MagicMock:
    """Build a mock NATS client where the KV bucket already exists."""
    mock_js = MagicMock()
    mock_js.key_value = AsyncMock(return_value=kv)
    mock_js.create_key_value = AsyncMock(return_value=kv)
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc


def _make_mock_nc_missing_bucket(create_kv: MagicMock | None = None) -> MagicMock:
    """Build a mock NATS client where the KV bucket does not exist."""
    mock_js = MagicMock()
    mock_js.key_value = AsyncMock(side_effect=NotFoundError("no bucket"))
    if create_kv is not None:
        mock_js.create_key_value = create_kv
    else:
        mock_js.create_key_value = AsyncMock(return_value=_make_mock_kv())
    mock_nc = MagicMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    return mock_nc


def _make_workflow(
    workflow_id: str = "wf-1",
    station_ids: list[str] | None = None,
) -> StationWorkflow:
    """Build a simple 3-station linear workflow for tests."""
    ids = station_ids or ["station-1", "station-2", "station-3"]
    stations = [
        StationWorkflowConfig(
            station_id=ids[0],
            name="First station",
            sequence_ref="seq-1",
            upstream_stations=[],
        ),
        StationWorkflowConfig(
            station_id=ids[1],
            name="Second station",
            sequence_ref="seq-2",
            upstream_stations=[ids[0]],
        ),
        StationWorkflowConfig(
            station_id=ids[2],
            name="Third station",
            sequence_ref="seq-3",
            upstream_stations=[ids[1]],
        ),
    ]
    return StationWorkflow(
        workflow_id=workflow_id,
        name="test workflow",
        stations=stations,
    )


def _make_handoff(
    session_id: str = "sess-1",
    station_id: str = "station-1",
    pass_fail: bool = True,
) -> StationHandoff:
    return StationHandoff(
        session_id=session_id,
        station_id=station_id,
        serial_number="SN-001",
        pass_fail=pass_fail,
        measurement_summary={"voltage": 5.0, "current": 0.1},
    )


# ---------------------------------------------------------------------------
# connect / disconnect tests
# ---------------------------------------------------------------------------


class TestStationOrchestratorConnect:
    """Tests for connect() and disconnect()."""

    @pytest.mark.asyncio
    async def test_connect_acquires_existing_bucket(self) -> None:
        """connect() acquires the KV bucket if it already exists."""
        kv = _make_mock_kv()
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="s1", nats_url="")
        await orch.connect(nc=mock_nc)

        assert orch._kv is kv
        # Does not own the connection - disconnect should not close it.
        await orch.disconnect()
        mock_nc.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_connect_creates_bucket_if_missing(self) -> None:
        """connect() creates the KV bucket if it doesn't exist."""
        new_kv = _make_mock_kv()
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(side_effect=NotFoundError("no bucket"))
        mock_js.create_key_value = AsyncMock(return_value=new_kv)
        mock_nc = MagicMock()
        mock_nc.jetstream = MagicMock(return_value=mock_js)

        orch = StationOrchestrator(station_id="s1")
        await orch.connect(nc=mock_nc)

        mock_js.key_value.assert_awaited_once_with(HANDOFF_KV_BUCKET)
        mock_js.create_key_value.assert_awaited_once_with(bucket=HANDOFF_KV_BUCKET)
        assert orch._kv is new_kv
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_connect_raises_runtime_error_on_create_failure(self) -> None:
        """connect() raises RuntimeError if bucket creation fails."""
        mock_nc = _make_mock_nc_missing_bucket(
            create_kv=AsyncMock(side_effect=Exception("NATS error")),
        )
        orch = StationOrchestrator(station_id="s1")
        with pytest.raises(RuntimeError, match="Failed to create KV bucket"):
            await orch.connect(nc=mock_nc)

    @pytest.mark.asyncio
    async def test_connect_raises_runtime_error_on_access_failure(self) -> None:
        """connect() raises RuntimeError if bucket access fails (non-NotFoundError)."""
        mock_js = MagicMock()
        mock_js.key_value = AsyncMock(side_effect=Exception("connection lost"))
        mock_nc = MagicMock()
        mock_nc.jetstream = MagicMock(return_value=mock_js)
        orch = StationOrchestrator(station_id="s1")
        with pytest.raises(RuntimeError, match="Failed to access KV bucket"):
            await orch.connect(nc=mock_nc)

    @pytest.mark.asyncio
    async def test_connect_idempotent(self) -> None:
        """connect() is a no-op if already connected."""
        kv = _make_mock_kv()
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="s1")
        await orch.connect(nc=mock_nc)
        # Second connect should not re-acquire.
        mock_js = mock_nc.jetstream.return_value
        mock_js.key_value.reset_mock()
        await orch.connect(nc=mock_nc)
        mock_js.key_value.assert_not_called()
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_is_safe_to_call_multiple_times(self) -> None:
        """disconnect() can be called multiple times without error."""
        kv = _make_mock_kv()
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="s1")
        await orch.connect(nc=mock_nc)
        await orch.disconnect()
        await orch.disconnect()  # Should not raise
        assert orch._nc is None
        assert orch._kv is None

    @pytest.mark.asyncio
    async def test_not_connected_methods_raise(self) -> None:
        """Methods raise RuntimeError if connect() was not called."""
        orch = StationOrchestrator(station_id="s1")
        wf = _make_workflow()
        with pytest.raises(RuntimeError, match="not connected"):
            await orch.register_workflow(wf)
        with pytest.raises(RuntimeError, match="not connected"):
            await orch.get_workflow("wf-1")
        # notify_done validates session/station match first, so use matching
        # values to reach the connection check.
        matching_handoff = StationHandoff(session_id="s", station_id="st")
        with pytest.raises(RuntimeError, match="not connected"):
            await orch.notify_done("s", "st", matching_handoff)
        with pytest.raises(RuntimeError, match="not connected"):
            await orch.wait_for_upstream("s", "st")
        with pytest.raises(RuntimeError, match="not connected"):
            await orch.get_handoff("s", "st")


# ---------------------------------------------------------------------------
# register_workflow / get_workflow tests
# ---------------------------------------------------------------------------


class TestRegisterAndGetWorkflow:
    """Tests for register_workflow() and get_workflow()."""

    @pytest.mark.asyncio
    async def test_register_workflow_writes_correct_key(self) -> None:
        """register_workflow writes to workflow.{workflow_id} in ate-handoffs."""
        kv = _make_mock_kv(put_return=42)
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="s1")
        await orch.connect(nc=mock_nc)

        wf = _make_workflow("wf-abc")
        revision = await orch.register_workflow(wf)

        assert revision == 42
        kv.put.assert_awaited_once_with(
            _workflow_key("wf-abc"),
            # Payload is JSON-encoded workflow dict; check round-trip below.
            kv.put.call_args.args[1],
        )
        assert kv.put.call_args.args[0] == "workflow.wf-abc"
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_register_workflow_serializes_correctly(self) -> None:
        """register_workflow payload round-trips through workflow_from_dict."""
        kv = _make_mock_kv()
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="s1")
        await orch.connect(nc=mock_nc)

        wf = _make_workflow("wf-rt")
        await orch.register_workflow(wf)

        payload = kv.put.call_args.args[1]
        data = json.loads(payload.decode("utf-8"))
        assert data["workflow_id"] == "wf-rt"
        assert len(data["stations"]) == 3
        assert data["stations"][1]["upstream_stations"] == ["station-1"]
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_register_workflow_raises_runtime_error_on_failure(self) -> None:
        """register_workflow raises RuntimeError if the KV put fails."""
        kv = MagicMock()
        kv.put = AsyncMock(side_effect=Exception("NATS connection lost"))
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="s1")
        await orch.connect(nc=mock_nc)

        with pytest.raises(RuntimeError, match="Failed to register workflow"):
            await orch.register_workflow(_make_workflow())
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_get_workflow_returns_workflow(self) -> None:
        """get_workflow reads back a registered workflow (round-trip)."""
        wf = _make_workflow("wf-get")
        kv = _make_mock_kv(
            entries={
                _workflow_key("wf-get"): json.dumps(
                    {
                        "workflow_id": wf.workflow_id,
                        "name": wf.name,
                        "stations": [
                            {
                                "station_id": s.station_id,
                                "name": s.name,
                                "sequence_ref": s.sequence_ref,
                                "upstream_stations": list(s.upstream_stations),
                                "timeout": s.timeout,
                            }
                            for s in wf.stations
                        ],
                        "handoff_rules": dict(wf.handoff_rules),
                        "created_at": wf.created_at.isoformat(),
                    }
                ).encode("utf-8"),
            },
        )
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="s1")
        await orch.connect(nc=mock_nc)

        result = await orch.get_workflow("wf-get")
        assert result is not None
        assert result.workflow_id == "wf-get"
        assert len(result.stations) == 3
        assert result.stations[1].upstream_stations == ["station-1"]
        kv.get.assert_awaited_once_with("workflow.wf-get")
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_get_workflow_returns_none_for_missing(self) -> None:
        """get_workflow returns None when the workflow key doesn't exist."""
        kv = _make_mock_kv(entries={})
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="s1")
        await orch.connect(nc=mock_nc)

        result = await orch.get_workflow("nonexistent")
        assert result is None
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_get_workflow_raises_runtime_error_on_failure(self) -> None:
        """get_workflow raises RuntimeError for non-KeyNotFoundError exceptions."""
        kv = MagicMock()
        kv.get = AsyncMock(side_effect=Exception("KV unavailable"))
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="s1")
        await orch.connect(nc=mock_nc)

        with pytest.raises(RuntimeError, match="Failed to get workflow"):
            await orch.get_workflow("wf-1")
        await orch.disconnect()


# ---------------------------------------------------------------------------
# notify_done / get_handoff tests
# ---------------------------------------------------------------------------


class TestNotifyDoneAndGetHandoff:
    """Tests for notify_done() and get_handoff()."""

    @pytest.mark.asyncio
    async def test_notify_done_writes_correct_key(self) -> None:
        """notify_done writes to session.{sid}.station.{stid}.done."""
        kv = _make_mock_kv(put_return=7)
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-1")
        await orch.connect(nc=mock_nc)

        handoff = _make_handoff(session_id="sess-1", station_id="station-1")
        revision = await orch.notify_done("sess-1", "station-1", handoff)

        assert revision == 7
        expected_key = _handoff_key("sess-1", "station-1")
        assert expected_key == "session.sess-1.station.station-1.done"
        kv.put.assert_awaited_once()
        assert kv.put.call_args.args[0] == expected_key
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_notify_done_serializes_handoff(self) -> None:
        """notify_done payload contains the handoff fields as JSON."""
        kv = _make_mock_kv()
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-1")
        await orch.connect(nc=mock_nc)

        handoff = _make_handoff(pass_fail=False)
        await orch.notify_done("sess-1", "station-1", handoff)

        payload = kv.put.call_args.args[1]
        data = json.loads(payload.decode("utf-8"))
        assert data["session_id"] == "sess-1"
        assert data["station_id"] == "station-1"
        assert data["pass_fail"] is False
        assert data["serial_number"] == "SN-001"
        assert data["measurement_summary"]["voltage"] == 5.0
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_notify_done_raises_on_station_id_mismatch(self) -> None:
        """notify_done raises ValueError if station_id != handoff.station_id."""
        kv = _make_mock_kv()
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-1")
        await orch.connect(nc=mock_nc)

        handoff = _make_handoff(station_id="station-2")
        with pytest.raises(ValueError, match="station_id"):
            await orch.notify_done("sess-1", "station-1", handoff)
        kv.put.assert_not_called()
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_notify_done_raises_on_session_id_mismatch(self) -> None:
        """notify_done raises ValueError if session_id != handoff.session_id."""
        kv = _make_mock_kv()
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-1")
        await orch.connect(nc=mock_nc)

        handoff = _make_handoff(session_id="other-session")
        with pytest.raises(ValueError, match="session_id"):
            await orch.notify_done("sess-1", "station-1", handoff)
        kv.put.assert_not_called()
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_notify_done_raises_runtime_error_on_failure(self) -> None:
        """notify_done raises RuntimeError if the KV put fails."""
        kv = MagicMock()
        kv.put = AsyncMock(side_effect=Exception("KV write failed"))
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-1")
        await orch.connect(nc=mock_nc)

        with pytest.raises(RuntimeError, match="Failed to write handoff"):
            await orch.notify_done("sess-1", "station-1", _make_handoff())
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_get_handoff_returns_handoff(self) -> None:
        """get_handoff reads back a handoff record (round-trip)."""
        handoff = _make_handoff(pass_fail=True)
        kv = _make_mock_kv(
            entries={
                _handoff_key("sess-1", "station-1"): json.dumps(
                    handoff_to_dict(handoff)
                ).encode("utf-8"),
            },
        )
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-2")
        await orch.connect(nc=mock_nc)

        result = await orch.get_handoff("sess-1", "station-1")
        assert result is not None
        assert result.session_id == "sess-1"
        assert result.station_id == "station-1"
        assert result.pass_fail is True
        assert result.serial_number == "SN-001"
        kv.get.assert_awaited_once_with(_handoff_key("sess-1", "station-1"))
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_get_handoff_returns_none_for_missing(self) -> None:
        """get_handoff returns None when no handoff record exists."""
        kv = _make_mock_kv(entries={})
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="s1")
        await orch.connect(nc=mock_nc)

        result = await orch.get_handoff("sess-1", "station-1")
        assert result is None
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_get_handoff_raises_runtime_error_on_failure(self) -> None:
        """get_handoff raises RuntimeError for non-KeyNotFoundError exceptions."""
        kv = MagicMock()
        kv.get = AsyncMock(side_effect=Exception("KV error"))
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="s1")
        await orch.connect(nc=mock_nc)

        with pytest.raises(RuntimeError, match="Failed to get handoff"):
            await orch.get_handoff("sess-1", "station-1")
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_get_handoff_handles_corrupt_payload(self) -> None:
        """get_handoff returns a bare handoff if the payload is corrupt."""
        kv = _make_mock_kv(
            entries={
                _handoff_key("sess-1", "station-1"): b"not-json",
            },
        )
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="s1")
        await orch.connect(nc=mock_nc)

        result = await orch.get_handoff("sess-1", "station-1")
        assert result is not None
        assert result.pass_fail is True  # Safe default
        await orch.disconnect()


# ---------------------------------------------------------------------------
# wait_for_upstream tests
# ---------------------------------------------------------------------------


class TestWaitForUpstream:
    """Tests for wait_for_upstream()."""

    @pytest.mark.asyncio
    async def test_returns_done_if_key_already_exists_pass(self) -> None:
        """wait_for_upstream returns DONE immediately if the key exists (pass)."""
        handoff = _make_handoff(pass_fail=True)
        kv = _make_mock_kv(
            entries={
                _handoff_key("sess-1", "station-1"): json.dumps(
                    handoff_to_dict(handoff)
                ).encode("utf-8"),
            },
        )
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-2")
        await orch.connect(nc=mock_nc)

        status = await orch.wait_for_upstream("sess-1", "station-1", timeout=1.0)
        assert status == HandoffStatus.DONE
        # Should not have started a watch.
        kv.watch.assert_not_called()
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_returns_failed_if_key_exists_with_failure(self) -> None:
        """wait_for_upstream returns FAILED if the key exists with pass_fail=False."""
        handoff = _make_handoff(pass_fail=False)
        kv = _make_mock_kv(
            entries={
                _handoff_key("sess-1", "station-1"): json.dumps(
                    handoff_to_dict(handoff)
                ).encode("utf-8"),
            },
        )
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-2")
        await orch.connect(nc=mock_nc)

        status = await orch.wait_for_upstream("sess-1", "station-1", timeout=1.0)
        assert status == HandoffStatus.FAILED
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_returns_timeout_when_no_key_and_watcher_empty(self) -> None:
        """wait_for_upstream returns TIMEOUT when no key appears in time.

        Uses a watcher that yields nothing then blocks (simulating a real
        nats-py watcher that never receives the key). The short timeout
        ensures the test completes quickly.
        """
        # Watcher blocks forever (until stop), simulating no key arrival.
        blocking_watcher = FakeKeyWatcher(entries=[], block_on_empty=True)
        kv = _make_mock_kv(entries={})
        kv.watch = AsyncMock(return_value=blocking_watcher)
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-2")
        await orch.connect(nc=mock_nc)

        status = await orch.wait_for_upstream(
            "sess-1", "station-1", timeout=0.2,
        )
        assert status == HandoffStatus.TIMEOUT
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_returns_done_when_watcher_yields_matching_put(self) -> None:
        """wait_for_upstream returns DONE when the watcher yields a matching PUT."""
        handoff = _make_handoff(pass_fail=True)
        entry = FakeKVEntry(
            key=_handoff_key("sess-1", "station-1"),
            value=json.dumps(handoff_to_dict(handoff)).encode("utf-8"),
            revision=3,
        )
        # Watcher yields the entry then ends (non-blocking).
        watcher = FakeKeyWatcher(entries=[entry], block_on_empty=False)
        kv = _make_mock_kv(entries={})
        kv.watch = AsyncMock(return_value=watcher)
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-2")
        await orch.connect(nc=mock_nc)

        status = await orch.wait_for_upstream("sess-1", "station-1", timeout=2.0)
        assert status == HandoffStatus.DONE
        kv.watch.assert_awaited_once_with(
            keys=_handoff_key("sess-1", "station-1"),
        )
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_returns_failed_when_watcher_yields_failure_handoff(self) -> None:
        """wait_for_upstream returns FAILED when the handoff has pass_fail=False."""
        handoff = _make_handoff(pass_fail=False)
        entry = FakeKVEntry(
            key=_handoff_key("sess-1", "station-1"),
            value=json.dumps(handoff_to_dict(handoff)).encode("utf-8"),
        )
        watcher = FakeKeyWatcher(entries=[entry], block_on_empty=False)
        kv = _make_mock_kv(entries={})
        kv.watch = AsyncMock(return_value=watcher)
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-2")
        await orch.connect(nc=mock_nc)

        status = await orch.wait_for_upstream("sess-1", "station-1", timeout=2.0)
        assert status == HandoffStatus.FAILED
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_skips_none_marker_before_matching_put(self) -> None:
        """wait_for_upstream skips the None end-of-initial marker."""
        handoff = _make_handoff(pass_fail=True)
        none_marker: Any = None
        entry = FakeKVEntry(
            key=_handoff_key("sess-1", "station-1"),
            value=json.dumps(handoff_to_dict(handoff)).encode("utf-8"),
        )
        watcher = FakeKeyWatcher(
            entries=[none_marker, entry], block_on_empty=False,
        )
        kv = _make_mock_kv(entries={})
        kv.watch = AsyncMock(return_value=watcher)
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-2")
        await orch.connect(nc=mock_nc)

        status = await orch.wait_for_upstream("sess-1", "station-1", timeout=2.0)
        assert status == HandoffStatus.DONE
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_skips_delete_operations(self) -> None:
        """wait_for_upstream skips DEL operations, continues to PUT."""
        del_entry = FakeKVEntry(
            key=_handoff_key("sess-1", "station-1"),
            value=b"",
            operation="DEL",
        )
        handoff = _make_handoff(pass_fail=True)
        put_entry = FakeKVEntry(
            key=_handoff_key("sess-1", "station-1"),
            value=json.dumps(handoff_to_dict(handoff)).encode("utf-8"),
        )
        watcher = FakeKeyWatcher(
            entries=[del_entry, put_entry], block_on_empty=False,
        )
        kv = _make_mock_kv(entries={})
        kv.watch = AsyncMock(return_value=watcher)
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-2")
        await orch.connect(nc=mock_nc)

        status = await orch.wait_for_upstream("sess-1", "station-1", timeout=2.0)
        assert status == HandoffStatus.DONE
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_skips_non_matching_keys(self) -> None:
        """wait_for_upstream ignores entries for other keys."""
        other_entry = FakeKVEntry(
            key=_handoff_key("sess-1", "station-99"),
            value=json.dumps(handoff_to_dict(_make_handoff())).encode("utf-8"),
        )
        handoff = _make_handoff(pass_fail=True)
        match_entry = FakeKVEntry(
            key=_handoff_key("sess-1", "station-1"),
            value=json.dumps(handoff_to_dict(handoff)).encode("utf-8"),
        )
        watcher = FakeKeyWatcher(
            entries=[other_entry, match_entry], block_on_empty=False,
        )
        kv = _make_mock_kv(entries={})
        kv.watch = AsyncMock(return_value=watcher)
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-2")
        await orch.connect(nc=mock_nc)

        status = await orch.wait_for_upstream("sess-1", "station-1", timeout=2.0)
        assert status == HandoffStatus.DONE
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_watch_raises_runtime_error_on_failure(self) -> None:
        """wait_for_upstream raises RuntimeError if the watch cannot be established."""
        kv = _make_mock_kv(entries={})
        kv.watch = AsyncMock(side_effect=Exception("watch failed"))
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-2")
        await orch.connect(nc=mock_nc)

        with pytest.raises(RuntimeError, match="Failed to watch handoff key"):
            await orch.wait_for_upstream("sess-1", "station-1", timeout=1.0)
        await orch.disconnect()

    @pytest.mark.asyncio
    async def test_stops_watcher_after_completion(self) -> None:
        """wait_for_upstream stops the watcher after observing the key."""
        handoff = _make_handoff(pass_fail=True)
        entry = FakeKVEntry(
            key=_handoff_key("sess-1", "station-1"),
            value=json.dumps(handoff_to_dict(handoff)).encode("utf-8"),
        )
        watcher = FakeKeyWatcher(entries=[entry], block_on_empty=True)
        kv = _make_mock_kv(entries={})
        kv.watch = AsyncMock(return_value=watcher)
        mock_nc = _make_mock_nc_existing(kv)
        orch = StationOrchestrator(station_id="station-2")
        await orch.connect(nc=mock_nc)

        await orch.wait_for_upstream("sess-1", "station-1", timeout=2.0)
        assert watcher._stop_called
        await orch.disconnect()


# ---------------------------------------------------------------------------
# Naming convention tests
# ---------------------------------------------------------------------------


class TestNamingConventions:
    """Tests for KV bucket name and key pattern conventions."""

    def test_kv_bucket_name_follows_convention(self) -> None:
        """KV bucket name is 'ate-handoffs' (lower-kebab, per NATS naming)."""
        assert HANDOFF_KV_BUCKET == "ate-handoffs"

    def test_handoff_key_pattern_follows_convention(self) -> None:
        """Handoff key pattern is session.{sid}.station.{stid}.done (lower.dot)."""
        key = _handoff_key("sess-123", "station-1")
        assert key == "session.sess-123.station.station-1.done"

    def test_workflow_key_pattern_follows_convention(self) -> None:
        """Workflow key pattern is workflow.{workflow_id} (lower.dot)."""
        key = _workflow_key("wf-abc")
        assert key == "workflow.wf-abc"

    def test_station_id_property(self) -> None:
        """station_id property returns the configured station_id."""
        orch = StationOrchestrator(station_id="my-station")
        assert orch.station_id == "my-station"

    def test_handoff_status_enum_values(self) -> None:
        """HandoffStatus has the required enum values."""
        assert HandoffStatus.PENDING.value == "pending"
        assert HandoffStatus.DONE.value == "done"
        assert HandoffStatus.TIMEOUT.value == "timeout"
        assert HandoffStatus.FAILED.value == "failed"


# ---------------------------------------------------------------------------
# shared.multi_station serialization tests
# ---------------------------------------------------------------------------


class TestMultiStationSerialization:
    """Tests for the shared.multi_station serialization helpers."""

    def test_handoff_round_trip(self) -> None:
        """handoff_to_dict / handoff_from_dict round-trip."""
        from shared.multi_station import handoff_from_dict, handoff_to_dict

        original = StationHandoff(
            session_id="s1",
            station_id="st1",
            serial_number="SN-42",
            pass_fail=False,
            measurement_summary={"a": 1, "b": [1, 2]},
        )
        data = handoff_to_dict(original)
        restored = handoff_from_dict(data)
        assert restored.session_id == "s1"
        assert restored.station_id == "st1"
        assert restored.serial_number == "SN-42"
        assert restored.pass_fail is False
        assert restored.measurement_summary == {"a": 1, "b": [1, 2]}

    def test_workflow_round_trip(self) -> None:
        """workflow_to_dict / workflow_from_dict round-trip."""
        from shared.multi_station import workflow_from_dict, workflow_to_dict

        original = _make_workflow("wf-rt")
        data = workflow_to_dict(original)
        restored = workflow_from_dict(data)
        assert restored.workflow_id == "wf-rt"
        assert len(restored.stations) == 3
        assert restored.stations[1].upstream_stations == ["station-1"]
        assert restored.stations[1].timeout == 300.0

    def test_handoff_from_dict_tolerates_missing_fields(self) -> None:
        """handoff_from_dict tolerates missing fields with safe defaults."""
        from shared.multi_station import handoff_from_dict

        restored = handoff_from_dict({})
        assert restored.session_id == ""
        assert restored.pass_fail is True
        assert restored.measurement_summary == {}

    def test_handoff_from_dict_tolerates_bad_timestamp(self) -> None:
        """handoff_from_dict falls back to now() on a bad timestamp."""
        from shared.multi_station import handoff_from_dict

        restored = handoff_from_dict({"timestamp": "not-a-date"})
        # Should not raise; timestamp is a datetime.
        assert hasattr(restored.timestamp, "year")
