"""Unit tests for InstrumentServer — gRPC server for remote instrument control.

Tests cover server lifecycle, instrument registration, session management,
and RPC handler dispatch. Uses mock drivers and direct handler calls
(no real network needed for unit tests).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import grpc
import pytest

from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.grpc.instrument_server import (
    InstrumentServer,
    _InstrumentEntry,
    _InstrumentServicer,
    _SessionState,
)
from ate_platform.drivers.grpc.protocol import (
    ConnectRequest,
    DisconnectRequest,
    GetIdentityRequest,
    IsConnectedRequest,
    ListInstrumentsRequest,
    QueryRequest,
    ReadRequest,
    WriteRequest,
)


class MockDriver(BaseDriver):
    """Simple mock driver for testing — bypasses real VISA."""

    def __init__(self) -> None:
        """Initialize mock driver without ResourceManager."""
        self._instrument: object = None
        self._address: str = ""
        self._connected: bool = False
        self._query_response: str = "mock_response"
        self._read_response: str = "mock_read"

    def connect(self, address: str) -> None:
        """Mock connect."""
        self._address = address
        self._connected = True

    def disconnect(self) -> None:
        """Mock disconnect."""
        self._connected = False
        self._address = ""

    @property
    def is_connected(self) -> bool:  # type: ignore[override]
        """Check mock connection state."""
        return self._connected

    @property
    def address(self) -> str:  # type: ignore[override]
        """Get mock address."""
        return self._address

    def query(self, command: str, delay: float | None = None) -> str:  # noqa: PLW0221
        """Mock query."""
        if not self._connected:
            msg = "Not connected"
            raise RuntimeError(msg)
        if command == "*IDN?":
            return "Mock,Driver,SN001,1.0"
        return self._query_response

    def write(self, command: str) -> None:  # noqa: PLW0221
        """Mock write."""
        if not self._connected:
            msg = "Not connected"
            raise RuntimeError(msg)

    def read(self) -> str:  # noqa: PLW0221
        """Mock read."""
        if not self._connected:
            msg = "Not connected"
            raise RuntimeError(msg)
        return self._read_response


class TestInstrumentServerInit:
    """Tests for InstrumentServer initialization."""

    def test_default_init(self) -> None:
        """Test initialization with defaults."""
        server = InstrumentServer()
        assert server.port == 50051
        assert not server.is_running
        assert server.list_instrument_ids() == []

    def test_custom_port(self) -> None:
        """Test initialization with custom port."""
        server = InstrumentServer(port=9090)
        assert server.port == 9090


class TestInstrumentRegistration:
    """Tests for instrument registration."""

    def test_register_instrument(self) -> None:
        """Test registering an instrument."""
        server = InstrumentServer()
        driver = MockDriver()
        server.register_instrument("dmm_1", driver)

        assert "dmm_1" in server.list_instrument_ids()

    def test_register_duplicate_raises(self) -> None:
        """Test registering a duplicate instrument_id raises ValueError."""
        server = InstrumentServer()
        server.register_instrument("dmm_1", MockDriver())
        with pytest.raises(ValueError, match="already registered"):
            server.register_instrument("dmm_1", MockDriver())

    def test_unregister_instrument(self) -> None:
        """Test unregistering an instrument."""
        server = InstrumentServer()
        server.register_instrument("dmm_1", MockDriver())
        server.unregister_instrument("dmm_1")

        assert "dmm_1" not in server.list_instrument_ids()

    def test_unregister_nonexistent_no_error(self) -> None:
        """Test unregistering a non-existent instrument does not raise."""
        server = InstrumentServer()
        server.unregister_instrument("nonexistent")  # Should not raise

    def test_list_instrument_ids(self) -> None:
        """Test listing instrument IDs."""
        server = InstrumentServer()
        server.register_instrument("dmm_1", MockDriver())
        server.register_instrument("psu_1", MockDriver())

        ids = server.list_instrument_ids()
        assert set(ids) == {"dmm_1", "psu_1"}


class TestServerLifecycle:
    """Tests for server start/stop."""

    def test_start_stop(self) -> None:
        """Test starting and stopping the server."""
        server = InstrumentServer(port=0)  # port 0 = ephemeral
        server.start()
        assert server.is_running

        server.stop()
        assert not server.is_running

    def test_double_start_raises(self) -> None:
        """Test starting an already-running server raises RuntimeError."""
        server = InstrumentServer(port=0)
        server.start()
        with pytest.raises(RuntimeError, match="already started"):
            server.start()
        server.stop()

    def test_stop_without_start_no_error(self) -> None:
        """Test stopping a non-started server does not raise."""
        server = InstrumentServer()
        server.stop()  # Should not raise


class TestRPCHandlers:
    """Tests for RPC handler methods."""

    def test_handle_connect_success(self) -> None:
        """Test Connect RPC handler delegates to driver.connect()."""
        server = InstrumentServer()
        driver = MockDriver()
        server.register_instrument("dmm_1", driver)

        request = ConnectRequest(
            session_id="s1",
            instrument_id="dmm_1",
            address="TCPIP0::1.2.3.4::INSTR",
        )
        context = MagicMock(spec=grpc.ServicerContext)
        response = server._handle_connect(request, context)

        assert response.success
        assert response.error == ""
        assert driver.is_connected
        assert driver.address == "TCPIP0::1.2.3.4::INSTR"

    def test_handle_connect_unknown_instrument(self) -> None:
        """Test Connect RPC for non-registered instrument returns error."""
        server = InstrumentServer()

        request = ConnectRequest(
            session_id="s1",
            instrument_id="unknown",
            address="ADDR",
        )
        context = MagicMock(spec=grpc.ServicerContext)
        response = server._handle_connect(request, context)

        assert not response.success
        assert "not registered" in response.error

    def test_handle_connect_driver_failure(self) -> None:
        """Test Connect RPC when driver.connect() raises."""
        server = InstrumentServer()
        driver = MockDriver()
        driver.connect = MagicMock(side_effect=RuntimeError("VISA timeout"))
        server.register_instrument("dmm_1", driver)

        request = ConnectRequest(
            session_id="s1",
            instrument_id="dmm_1",
            address="ADDR",
        )
        context = MagicMock(spec=grpc.ServicerContext)
        response = server._handle_connect(request, context)

        assert not response.success
        assert "VISA timeout" in response.error

    def test_handle_disconnect_success(self) -> None:
        """Test Disconnect RPC handler delegates to driver.disconnect()."""
        server = InstrumentServer()
        driver = MockDriver()
        server.register_instrument("dmm_1", driver)

        # Connect first
        connect_req = ConnectRequest(
            session_id="s1", instrument_id="dmm_1", address="ADDR"
        )
        context = MagicMock(spec=grpc.ServicerContext)
        server._handle_connect(connect_req, context)
        assert driver.is_connected

        # Now disconnect
        disconnect_req = DisconnectRequest(session_id="s1", instrument_id="dmm_1")
        response = server._handle_disconnect(disconnect_req, context)

        assert response.success
        assert not driver.is_connected

    def test_handle_query_success(self) -> None:
        """Test Query RPC handler delegates to driver.query()."""
        server = InstrumentServer()
        driver = MockDriver()
        driver._query_response = "1.234567E+00"
        server.register_instrument("dmm_1", driver)

        # Connect first
        connect_req = ConnectRequest(
            session_id="s1", instrument_id="dmm_1", address="ADDR"
        )
        context = MagicMock(spec=grpc.ServicerContext)
        server._handle_connect(connect_req, context)

        # Query
        query_req = QueryRequest(
            session_id="s1", instrument_id="dmm_1", command="MEAS:VOLT?"
        )
        response = server._handle_query(query_req, context)

        assert response.success
        assert response.response == "1.234567E+00"

    def test_handle_query_with_delay(self) -> None:
        """Test Query RPC handler passes delay parameter."""
        server = InstrumentServer()
        driver = MockDriver()
        server.register_instrument("dmm_1", driver)

        connect_req = ConnectRequest(
            session_id="s1", instrument_id="dmm_1", address="ADDR"
        )
        context = MagicMock(spec=grpc.ServicerContext)
        server._handle_connect(connect_req, context)

        query_req = QueryRequest(
            session_id="s1", instrument_id="dmm_1", command="MEAS:VOLT?"
        )
        query_req.delay = 0.5
        response = server._handle_query(query_req, context)

        assert response.success

    def test_handle_query_not_connected(self) -> None:
        """Test Query RPC when instrument is not connected returns error."""
        server = InstrumentServer()
        driver = MockDriver()
        server.register_instrument("dmm_1", driver)

        query_req = QueryRequest(
            session_id="s1", instrument_id="dmm_1", command="*IDN?"
        )
        context = MagicMock(spec=grpc.ServicerContext)
        response = server._handle_query(query_req, context)

        assert not response.success
        assert "Not connected" in response.error

    def test_handle_write_success(self) -> None:
        """Test Write RPC handler delegates to driver.write()."""
        server = InstrumentServer()
        driver = MockDriver()
        server.register_instrument("dmm_1", driver)

        connect_req = ConnectRequest(
            session_id="s1", instrument_id="dmm_1", address="ADDR"
        )
        context = MagicMock(spec=grpc.ServicerContext)
        server._handle_connect(connect_req, context)

        write_req = WriteRequest(
            session_id="s1", instrument_id="dmm_1", command="*RST"
        )
        response = server._handle_write(write_req, context)

        assert response.success

    def test_handle_read_success(self) -> None:
        """Test Read RPC handler delegates to driver.read()."""
        server = InstrumentServer()
        driver = MockDriver()
        driver._read_response = "+9.999999E+00"
        server.register_instrument("dmm_1", driver)

        connect_req = ConnectRequest(
            session_id="s1", instrument_id="dmm_1", address="ADDR"
        )
        context = MagicMock(spec=grpc.ServicerContext)
        server._handle_connect(connect_req, context)

        read_req = ReadRequest(session_id="s1", instrument_id="dmm_1")
        response = server._handle_read(read_req, context)

        assert response.success
        assert response.response == "+9.999999E+00"

    def test_handle_get_identity(self) -> None:
        """Test GetIdentity RPC handler queries *IDN?."""
        server = InstrumentServer()
        driver = MockDriver()
        server.register_instrument("dmm_1", driver)

        connect_req = ConnectRequest(
            session_id="s1", instrument_id="dmm_1", address="ADDR"
        )
        context = MagicMock(spec=grpc.ServicerContext)
        server._handle_connect(connect_req, context)

        identity_req = GetIdentityRequest(session_id="s1", instrument_id="dmm_1")
        response = server._handle_getidentity(identity_req, context)

        assert response.success
        assert response.identity == "Mock,Driver,SN001,1.0"

    def test_handle_is_connected(self) -> None:
        """Test IsConnected RPC handler returns connection state."""
        server = InstrumentServer()
        driver = MockDriver()
        server.register_instrument("dmm_1", driver)

        context = MagicMock(spec=grpc.ServicerContext)
        is_conn_req = IsConnectedRequest(session_id="s1", instrument_id="dmm_1")

        # Not connected yet
        response = server._handle_isconnected(is_conn_req, context)
        assert not response.connected

        # Connect
        connect_req = ConnectRequest(
            session_id="s1", instrument_id="dmm_1", address="ADDR"
        )
        server._handle_connect(connect_req, context)

        # Now connected
        response = server._handle_isconnected(is_conn_req, context)
        assert response.connected

    def test_handle_list_instruments(self) -> None:
        """Test ListInstruments RPC handler returns all instruments."""
        server = InstrumentServer()
        driver1 = MockDriver()
        driver2 = MockDriver()
        server.register_instrument("dmm_1", driver1)
        server.register_instrument("psu_1", driver2)

        # Connect dmm_1
        connect_req = ConnectRequest(
            session_id="s1", instrument_id="dmm_1", address="ADDR1"
        )
        context = MagicMock(spec=grpc.ServicerContext)
        server._handle_connect(connect_req, context)

        list_req = ListInstrumentsRequest(session_id="s1")
        response = server._handle_listinstruments(list_req, context)

        assert len(response.instruments) == 2
        ids = {inst.instrument_id for inst in response.instruments}
        assert ids == {"dmm_1", "psu_1"}

        # Check dmm_1 is connected, psu_1 is not
        for inst in response.instruments:
            if inst.instrument_id == "dmm_1":
                assert inst.connected
                assert inst.address == "ADDR1"
            elif inst.instrument_id == "psu_1":
                assert not inst.connected


class TestSessionManagement:
    """Tests for multi-client session management."""

    def test_multiple_sessions_same_instrument(self) -> None:
        """Test multiple client sessions can use the same instrument."""
        server = InstrumentServer()
        driver = MockDriver()
        server.register_instrument("dmm_1", driver)

        context = MagicMock(spec=grpc.ServicerContext)

        # Session 1 connects
        req1 = ConnectRequest(session_id="s1", instrument_id="dmm_1", address="ADDR")
        server._handle_connect(req1, context)

        # Session 2 connects (same instrument)
        req2 = ConnectRequest(session_id="s2", instrument_id="dmm_1", address="ADDR")
        server._handle_connect(req2, context)

        active = server.get_active_sessions()
        assert set(active) == {"s1", "s2"}

    def test_disconnect_clears_session(self) -> None:
        """Test disconnect clears the session from active list."""
        server = InstrumentServer()
        driver = MockDriver()
        server.register_instrument("dmm_1", driver)

        context = MagicMock(spec=grpc.ServicerContext)

        # Connect
        connect_req = ConnectRequest(
            session_id="s1", instrument_id="dmm_1", address="ADDR"
        )
        server._handle_connect(connect_req, context)
        assert "s1" in server.get_active_sessions()

        # Disconnect
        disconnect_req = DisconnectRequest(session_id="s1", instrument_id="dmm_1")
        server._handle_disconnect(disconnect_req, context)
        assert "s1" not in server.get_active_sessions()

    def test_unregister_clears_sessions(self) -> None:
        """Test unregistering an instrument clears its sessions."""
        server = InstrumentServer()
        driver = MockDriver()
        server.register_instrument("dmm_1", driver)

        context = MagicMock(spec=grpc.ServicerContext)
        connect_req = ConnectRequest(
            session_id="s1", instrument_id="dmm_1", address="ADDR"
        )
        server._handle_connect(connect_req, context)
        assert "s1" in server.get_active_sessions()

        server.unregister_instrument("dmm_1")
        assert "s1" not in server.get_active_sessions()


class TestServicerDispatch:
    """Tests for the _InstrumentServicer generic RPC handler."""

    def test_service_returns_handler_for_valid_method(self) -> None:
        """Test that servicer returns a handler for valid method names."""
        server = InstrumentServer()
        servicer = _InstrumentServicer(server)

        details = MagicMock(spec=grpc.HandlerCallDetails)
        details.method = "/ate_platform.drivers.grpc.InstrumentService/Query"

        handler = servicer.service(details)
        assert handler is not None
        assert handler.unary_unary is not None

    def test_service_returns_none_for_unknown_service(self) -> None:
        """Test that servicer returns None for unknown service name."""
        server = InstrumentServer()
        servicer = _InstrumentServicer(server)

        details = MagicMock(spec=grpc.HandlerCallDetails)
        details.method = "/unknown.Service/Method"

        handler = servicer.service(details)
        assert handler is None

    def test_service_returns_none_for_unknown_method(self) -> None:
        """Test that servicer returns None for unknown method name."""
        server = InstrumentServer()
        servicer = _InstrumentServicer(server)

        details = MagicMock(spec=grpc.HandlerCallDetails)
        details.method = "/ate_platform.drivers.grpc.InstrumentService/UnknownMethod"

        handler = servicer.service(details)
        assert handler is None

    def test_service_returns_none_for_malformed_path(self) -> None:
        """Test that servicer returns None for malformed method paths."""
        server = InstrumentServer()
        servicer = _InstrumentServicer(server)

        details = MagicMock(spec=grpc.HandlerCallDetails)
        details.method = "malformed"

        handler = servicer.service(details)
        assert handler is None


class TestDataclasses:
    """Tests for internal dataclasses."""

    def test_session_state_defaults(self) -> None:
        """Test _SessionState default values."""
        state = _SessionState()
        assert state.instrument_id == ""
        assert state.address == ""

    def test_instrument_entry_defaults(self) -> None:
        """Test _InstrumentEntry default values."""
        driver = MockDriver()
        entry = _InstrumentEntry(driver=driver)
        assert entry.driver is driver
        assert entry.sessions == set()

    def test_instrument_entry_with_sessions(self) -> None:
        """Test _InstrumentEntry with sessions."""
        driver = MockDriver()
        entry = _InstrumentEntry(driver=driver, sessions={"s1", "s2"})
        assert entry.sessions == {"s1", "s2"}
