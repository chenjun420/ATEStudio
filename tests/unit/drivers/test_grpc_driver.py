"""Unit tests for GrpcDriverStub — remote instrument driver via gRPC.

Tests verify that GrpcDriverStub correctly delegates SCPI/instrument calls
via gRPC, handles connection lifecycle, and raises appropriate errors.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.grpc.grpc_driver_stub import GrpcDriverStub
from ate_platform.drivers.grpc.protocol import (
    ConnectResponse,
    DisconnectResponse,
    GetIdentityResponse,
    IsConnectedResponse,
    QueryResponse,
    ReadResponse,
    WriteResponse,
)


class TestGrpcDriverStubInit:
    """Tests for GrpcDriverStub initialization."""

    def test_inherits_base_driver(self) -> None:
        """GrpcDriverStub must be a BaseDriver subclass."""
        assert issubclass(GrpcDriverStub, BaseDriver)

    def test_default_init(self) -> None:
        """Test initialization with defaults."""
        stub = GrpcDriverStub()
        assert stub._server_address == "localhost:50051"
        assert stub._instrument_id == ""
        assert stub._session_id != ""  # auto-generated
        assert stub._channel is None
        assert not stub._grpc_connected
        assert not stub.is_connected
        assert stub.address == ""

    def test_custom_init(self) -> None:
        """Test initialization with custom parameters."""
        stub = GrpcDriverStub(
            server_address="remote.host:9090",
            instrument_id="dmm_1",
            session_id="my-session",
        )
        assert stub._server_address == "remote.host:9090"
        assert stub._instrument_id == "dmm_1"
        assert stub._session_id == "my-session"

    def test_unique_session_ids(self) -> None:
        """Each stub without explicit session_id gets a unique ID."""
        stub1 = GrpcDriverStub()
        stub2 = GrpcDriverStub()
        assert stub1._session_id != stub2._session_id


class TestGrpcDriverStubConnect:
    """Tests for connect/disconnect via gRPC."""

    @patch("ate_platform.drivers.grpc.grpc_driver_stub.grpc")
    def test_connect_success(self, mock_grpc: MagicMock) -> None:
        """Test successful connect delegates to gRPC Connect RPC."""
        mock_channel = MagicMock()
        mock_grpc.insecure_channel.return_value = mock_channel

        # Mock the unary_unary call to return a success ConnectResponse
        mock_call = MagicMock(return_value=ConnectResponse(success=True, error=""))
        mock_channel.unary_unary.return_value = mock_call

        stub = GrpcDriverStub(
            server_address="localhost:50051",
            instrument_id="dmm_1",
            session_id="s1",
        )
        stub.connect("TCPIP0::192.168.1.1::INSTR")

        assert stub.is_connected
        assert stub.address == "TCPIP0::192.168.1.1::INSTR"
        mock_grpc.insecure_channel.assert_called_once_with("localhost:50051")
        mock_call.assert_called_once()

    @patch("ate_platform.drivers.grpc.grpc_driver_stub.grpc")
    def test_connect_failure_raises(self, mock_grpc: MagicMock) -> None:
        """Test connect failure raises RuntimeError."""
        mock_channel = MagicMock()
        mock_grpc.insecure_channel.return_value = mock_channel

        mock_call = MagicMock(
            return_value=ConnectResponse(success=False, error="VISA error: timeout")
        )
        mock_channel.unary_unary.return_value = mock_call

        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        with pytest.raises(RuntimeError, match="Remote connect failed: VISA error"):
            stub.connect("TCPIP0::bad::INSTR")

        assert not stub.is_connected

    @patch("ate_platform.drivers.grpc.grpc_driver_stub.grpc")
    def test_disconnect_success(self, mock_grpc: MagicMock) -> None:
        """Test disconnect delegates to gRPC Disconnect RPC."""
        mock_channel = MagicMock()
        mock_grpc.insecure_channel.return_value = mock_channel

        # Connect first
        mock_channel.unary_unary.return_value = MagicMock(
            return_value=ConnectResponse(success=True, error="")
        )
        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        stub.connect("ADDR")
        assert stub.is_connected

        # Now disconnect — reset mock for the disconnect call
        mock_channel.unary_unary.return_value = MagicMock(
            return_value=DisconnectResponse(success=True, error="")
        )
        stub.disconnect()

        assert not stub.is_connected
        assert stub.address == ""

    @patch("ate_platform.drivers.grpc.grpc_driver_stub.grpc")
    def test_disconnect_not_connected_no_error(self, mock_grpc: MagicMock) -> None:
        """Test disconnect when not connected does not raise."""
        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        # Should not raise
        stub.disconnect()

    @patch("ate_platform.drivers.grpc.grpc_driver_stub.grpc")
    def test_context_manager(self, mock_grpc: MagicMock) -> None:
        """Test using stub as context manager."""
        mock_channel = MagicMock()
        mock_grpc.insecure_channel.return_value = mock_channel
        mock_channel.unary_unary.return_value = MagicMock(
            return_value=ConnectResponse(success=True, error="")
        )

        with GrpcDriverStub(instrument_id="dmm_1", session_id="s1") as stub:
            stub.connect("ADDR")
            assert stub.is_connected
        # After context exit, should be disconnected
        assert not stub.is_connected


class TestGrpcDriverStubQuery:
    """Tests for query via gRPC."""

    @patch("ate_platform.drivers.grpc.grpc_driver_stub.grpc")
    def test_query_success(self, mock_grpc: MagicMock) -> None:
        """Test query delegates to gRPC Query RPC."""
        mock_channel = MagicMock()
        mock_grpc.insecure_channel.return_value = mock_channel

        # First call: Connect success, second: Query response
        responses = [
            ConnectResponse(success=True, error=""),
            QueryResponse(response="Keithley,2450,12345,1.0", success=True, error=""),
        ]
        mock_channel.unary_unary.return_value = MagicMock(side_effect=responses)

        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        stub.connect("ADDR")
        result = stub.query("*IDN?")

        assert result == "Keithley,2450,12345,1.0"

    @patch("ate_platform.drivers.grpc.grpc_driver_stub.grpc")
    def test_query_with_delay(self, mock_grpc: MagicMock) -> None:
        """Test query passes delay parameter."""
        mock_channel = MagicMock()
        mock_grpc.insecure_channel.return_value = mock_channel

        responses = [
            ConnectResponse(success=True, error=""),
            QueryResponse(response="OK", success=True, error=""),
        ]
        mock_call = MagicMock(side_effect=responses)
        mock_channel.unary_unary.return_value = mock_call

        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        stub.connect("ADDR")
        stub.query("*IDN?", delay=0.5)

        # Verify the query request had delay set
        query_request = mock_call.call_args_list[1].args[0]
        assert query_request.HasField("delay")
        assert query_request.delay == 0.5

    @patch("ate_platform.drivers.grpc.grpc_driver_stub.grpc")
    def test_query_failure_raises(self, mock_grpc: MagicMock) -> None:
        """Test query failure raises RuntimeError."""
        mock_channel = MagicMock()
        mock_grpc.insecure_channel.return_value = mock_channel

        responses = [
            ConnectResponse(success=True, error=""),
            QueryResponse(response="", success=False, error="Timeout"),
        ]
        mock_channel.unary_unary.return_value = MagicMock(side_effect=responses)

        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        stub.connect("ADDR")
        with pytest.raises(RuntimeError, match="Remote query failed: Timeout"):
            stub.query("*IDN?")

    def test_query_not_connected_raises(self) -> None:
        """Test query when not connected raises RuntimeError."""
        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        with pytest.raises(RuntimeError, match="Not connected"):
            stub.query("*IDN?")


class TestGrpcDriverStubWrite:
    """Tests for write via gRPC."""

    @patch("ate_platform.drivers.grpc.grpc_driver_stub.grpc")
    def test_write_success(self, mock_grpc: MagicMock) -> None:
        """Test write delegates to gRPC Write RPC."""
        mock_channel = MagicMock()
        mock_grpc.insecure_channel.return_value = mock_channel

        responses = [
            ConnectResponse(success=True, error=""),
            WriteResponse(success=True, error=""),
        ]
        mock_call = MagicMock(side_effect=responses)
        mock_channel.unary_unary.return_value = mock_call

        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        stub.connect("ADDR")
        stub.write("*RST")

        # Verify write request
        write_request = mock_call.call_args_list[1].args[0]
        assert write_request.command == "*RST"

    @patch("ate_platform.drivers.grpc.grpc_driver_stub.grpc")
    def test_write_failure_raises(self, mock_grpc: MagicMock) -> None:
        """Test write failure raises RuntimeError."""
        mock_channel = MagicMock()
        mock_grpc.insecure_channel.return_value = mock_channel

        responses = [
            ConnectResponse(success=True, error=""),
            WriteResponse(success=False, error="Bus error"),
        ]
        mock_channel.unary_unary.return_value = MagicMock(side_effect=responses)

        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        stub.connect("ADDR")
        with pytest.raises(RuntimeError, match="Remote write failed: Bus error"):
            stub.write("*RST")

    def test_write_not_connected_raises(self) -> None:
        """Test write when not connected raises RuntimeError."""
        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        with pytest.raises(RuntimeError, match="Not connected"):
            stub.write("*RST")


class TestGrpcDriverStubRead:
    """Tests for read via gRPC."""

    @patch("ate_platform.drivers.grpc.grpc_driver_stub.grpc")
    def test_read_success(self, mock_grpc: MagicMock) -> None:
        """Test read delegates to gRPC Read RPC."""
        mock_channel = MagicMock()
        mock_grpc.insecure_channel.return_value = mock_channel

        responses = [
            ConnectResponse(success=True, error=""),
            ReadResponse(response="+1.234567E+00", success=True, error=""),
        ]
        mock_channel.unary_unary.return_value = MagicMock(side_effect=responses)

        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        stub.connect("ADDR")
        result = stub.read()

        assert result == "+1.234567E+00"

    def test_read_not_connected_raises(self) -> None:
        """Test read when not connected raises RuntimeError."""
        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        with pytest.raises(RuntimeError, match="Not connected"):
            stub.read()


class TestGrpcDriverStubIdentity:
    """Tests for get_identity and check_remote_connected."""

    @patch("ate_platform.drivers.grpc.grpc_driver_stub.grpc")
    def test_get_identity_success(self, mock_grpc: MagicMock) -> None:
        """Test get_identity delegates to gRPC GetIdentity RPC."""
        mock_channel = MagicMock()
        mock_grpc.insecure_channel.return_value = mock_channel

        responses = [
            ConnectResponse(success=True, error=""),
            GetIdentityResponse(identity="Keithley,2450,SN001,A1.0", success=True, error=""),
        ]
        mock_channel.unary_unary.return_value = MagicMock(side_effect=responses)

        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        stub.connect("ADDR")
        result = stub.get_identity()

        assert result == "Keithley,2450,SN001,A1.0"

    @patch("ate_platform.drivers.grpc.grpc_driver_stub.grpc")
    def test_check_remote_connected(self, mock_grpc: MagicMock) -> None:
        """Test check_remote_connected queries server-side state."""
        mock_channel = MagicMock()
        mock_grpc.insecure_channel.return_value = mock_channel

        responses = [
            ConnectResponse(success=True, error=""),
            IsConnectedResponse(connected=True),
        ]
        mock_channel.unary_unary.return_value = MagicMock(side_effect=responses)

        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        stub.connect("ADDR")
        assert stub.check_remote_connected() is True


class TestGrpcDriverStubRPCDispatch:
    """Tests for RPC method dispatch and serialization."""

    @patch("ate_platform.drivers.grpc.grpc_driver_stub.grpc")
    def test_rpc_uses_correct_service_name(self, mock_grpc: MagicMock) -> None:
        """Test that RPC calls use the correct service name in the method path."""
        mock_channel = MagicMock()
        mock_grpc.insecure_channel.return_value = mock_channel
        mock_channel.unary_unary.return_value = MagicMock(
            return_value=ConnectResponse(success=True, error="")
        )

        stub = GrpcDriverStub(instrument_id="dmm_1", session_id="s1")
        stub.connect("ADDR")

        # Check that unary_unary was called with the correct method path
        call_args = mock_channel.unary_unary.call_args
        method_path = call_args.args[0] if call_args.args else call_args[0]
        assert "InstrumentService" in method_path
        assert "Connect" in method_path
