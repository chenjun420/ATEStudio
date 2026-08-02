"""GrpcDriverStub — remote instrument driver via gRPC.

Extends BaseDriver to delegate SCPI/instrument calls to a remote InstrumentServer
over a gRPC channel. This allows test sequences to control instruments that are
physically connected to a different machine on the network.

Usage:
    stub = GrpcDriverStub(
        server_address="localhost:50051",
        instrument_id="dmm_1",
        session_id="session-abc",
    )
    stub.connect("TCPIP0::192.168.1.1::INSTR")
    response = stub.query("*IDN?")
    stub.disconnect()
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import grpc

from ate_platform.drivers.base_hal import BaseDriver
from ate_platform.drivers.grpc.protocol import (
    SERVICE_METHODS,
    SERVICE_NAME,
    ConnectRequest,
    ConnectResponse,
    DisconnectRequest,
    DisconnectResponse,
    GetIdentityRequest,
    GetIdentityResponse,
    IsConnectedRequest,
    IsConnectedResponse,
    QueryRequest,
    QueryResponse,
    ReadRequest,
    ReadResponse,
    WriteRequest,
    WriteResponse,
)

if TYPE_CHECKING:
    from google.protobuf.message import Message


class GrpcDriverStub(BaseDriver):
    """HAL driver that delegates all instrument calls via gRPC.

    Overrides connect/disconnect/query/write/read to send RPC calls to a remote
    InstrumentServer. The server routes calls to the actual instrument driver.

    Attributes:
        _server_address: gRPC server address (host:port).
        _instrument_id: Registered instrument name on the server.
        _session_id: Unique session ID for this client connection.
        _channel: gRPC channel (created on connect, closed on disconnect).
        _grpc_connected: Whether the gRPC channel is active.
        _remote_connected: Whether the remote instrument is connected.
        _remote_address: VISA address of the remote instrument.
    """

    def __init__(
        self,
        server_address: str = "localhost:50051",
        instrument_id: str = "",
        session_id: str | None = None,
        resource_manager: object | None = None,
    ) -> None:
        """Initialize the gRPC driver stub.

        Args:
            server_address: gRPC server address in host:port format.
            instrument_id: Name of the registered instrument on the server.
            session_id: Unique session ID. Auto-generated if None.
            resource_manager: Ignored — kept for BaseDriver signature compat.
        """
        # Deliberately skip BaseDriver.__init__ to avoid creating a PyVISA
        # ResourceManager — this driver talks gRPC, not VISA.
        self._server_address: str = server_address
        self._instrument_id: str = instrument_id
        self._session_id: str = session_id or str(uuid.uuid4())
        self._channel: grpc.Channel | None = None
        self._grpc_connected: bool = False
        self._remote_connected: bool = False
        self._remote_address: str = ""

    # ------------------------------------------------------------------
    # gRPC channel management
    # ------------------------------------------------------------------

    def _ensure_channel(self) -> grpc.Channel:
        """Create or return the gRPC channel."""
        if self._channel is None:
            self._channel = grpc.insecure_channel(self._server_address)
            self._grpc_connected = True
        return self._channel

    def _close_channel(self) -> None:
        """Close the gRPC channel if open."""
        if self._channel is not None:
            self._channel.close()
            self._channel = None
            self._grpc_connected = False

    def _call_rpc(
        self,
        method: str,
        request: Message,
    ) -> Message:
        """Make a unary-unary RPC call to the InstrumentService.

        Args:
            method: RPC method name (e.g. "Query").
            request: Protobuf request message.

        Returns:
            Protobuf response message.

        Raises:
            RuntimeError: If the RPC fails or the server returns an error.
        """
        channel = self._ensure_channel()
        request_class, response_class = SERVICE_METHODS[method]
        full_method = f"/{SERVICE_NAME}/{method}"
        response = channel.unary_unary(
            full_method,
            request_serializer=request_class.SerializeToString,
            response_deserializer=response_class.FromString,
        )(request)
        return response

    # ------------------------------------------------------------------
    # BaseDriver overrides
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:  # type: ignore[override]
        """Check if the remote instrument is connected."""
        return self._remote_connected

    @property
    def address(self) -> str:  # type: ignore[override]
        """Get the remote instrument address."""
        return self._remote_address

    def connect(self, address: str) -> None:  # noqa: PLW0221
        """Connect to the remote instrument via gRPC.

        Args:
            address: VISA resource address to connect to on the server side.

        Raises:
            RuntimeError: If the server reports a connection failure.
        """
        request = ConnectRequest(
            session_id=self._session_id,
            instrument_id=self._instrument_id,
            address=address,
        )
        response: ConnectResponse = self._call_rpc("Connect", request)  # type: ignore[assignment]
        if not response.success:
            self._close_channel()
            msg = f"Remote connect failed: {response.error}"
            raise RuntimeError(msg)
        self._remote_connected = True
        self._remote_address = address

    def disconnect(self) -> None:  # noqa: PLW0221
        """Disconnect from the remote instrument."""
        if not self._remote_connected:
            self._close_channel()
            return

        request = DisconnectRequest(
            session_id=self._session_id,
            instrument_id=self._instrument_id,
        )
        response: DisconnectResponse = self._call_rpc("Disconnect", request)  # type: ignore[assignment]
        if not response.success:
            # Log but don't raise — we're disconnecting anyway
            pass
        self._remote_connected = False
        self._remote_address = ""
        self._close_channel()

    def write(self, command: str) -> None:  # noqa: PLW0221
        """Send a SCPI write command to the remote instrument.

        Args:
            command: SCPI command string.

        Raises:
            RuntimeError: If not connected or the server reports an error.
        """
        if not self._remote_connected:
            msg = "Not connected to any instrument. Call connect() first."
            raise RuntimeError(msg)

        request = WriteRequest(
            session_id=self._session_id,
            instrument_id=self._instrument_id,
            command=command,
        )
        response: WriteResponse = self._call_rpc("Write", request)  # type: ignore[assignment]
        if not response.success:
            msg = f"Remote write failed: {response.error}"
            raise RuntimeError(msg)

    def query(self, command: str, delay: float | None = None) -> str:  # noqa: PLW0221
        """Send a SCPI query and read the response from the remote instrument.

        Args:
            command: SCPI query command.
            delay: Optional delay between write and read in seconds.

        Returns:
            Response string from the remote instrument.

        Raises:
            RuntimeError: If not connected or the server reports an error.
        """
        if not self._remote_connected:
            msg = "Not connected to any instrument. Call connect() first."
            raise RuntimeError(msg)

        request = QueryRequest(
            session_id=self._session_id,
            instrument_id=self._instrument_id,
            command=command,
        )
        if delay is not None:
            request.delay = delay
        response: QueryResponse = self._call_rpc("Query", request)  # type: ignore[assignment]
        if not response.success:
            msg = f"Remote query failed: {response.error}"
            raise RuntimeError(msg)
        return response.response

    def read(self) -> str:  # noqa: PLW0221
        """Read a response from the remote instrument.

        Returns:
            Response string from the remote instrument.

        Raises:
            RuntimeError: If not connected or the server reports an error.
        """
        if not self._remote_connected:
            msg = "Not connected to any instrument. Call connect() first."
            raise RuntimeError(msg)

        request = ReadRequest(
            session_id=self._session_id,
            instrument_id=self._instrument_id,
        )
        response: ReadResponse = self._call_rpc("Read", request)  # type: ignore[assignment]
        if not response.success:
            msg = f"Remote read failed: {response.error}"
            raise RuntimeError(msg)
        return response.response

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    def get_identity(self) -> str:
        """Get the instrument identity (*IDN?) via gRPC.

        Returns:
            Identity string from the remote instrument.

        Raises:
            RuntimeError: If the server reports an error.
        """
        request = GetIdentityRequest(
            session_id=self._session_id,
            instrument_id=self._instrument_id,
        )
        response: GetIdentityResponse = self._call_rpc("GetIdentity", request)  # type: ignore[assignment]
        if not response.success:
            msg = f"Remote get_identity failed: {response.error}"
            raise RuntimeError(msg)
        return response.identity

    def check_remote_connected(self) -> bool:
        """Check if the remote instrument is connected (server-side state).

        Returns:
            True if the server reports the instrument as connected.
        """
        request = IsConnectedRequest(
            session_id=self._session_id,
            instrument_id=self._instrument_id,
        )
        response: IsConnectedResponse = self._call_rpc("IsConnected", request)  # type: ignore[assignment]
        return response.connected

    def __enter__(self) -> GrpcDriverStub:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Context manager exit — ensures disconnect."""
        self.disconnect()
