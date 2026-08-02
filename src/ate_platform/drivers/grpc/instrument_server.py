"""InstrumentServer — gRPC server for remote instrument control.

Exposes registered BaseDriver instances as RPC calls. Multiple clients can
connect simultaneously, each with its own session. The server routes RPC
calls to the correct instrument by instrument_id.

Usage:
    server = InstrumentServer(port=50051)
    server.register_instrument("dmm_1", my_dmm_driver)
    server.start()
    # ... clients connect and issue commands ...
    server.stop()
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

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
    InstrumentInfo,
    IsConnectedRequest,
    IsConnectedResponse,
    ListInstrumentsRequest,
    ListInstrumentsResponse,
    QueryRequest,
    QueryResponse,
    ReadRequest,
    ReadResponse,
    WriteRequest,
    WriteResponse,
)


@dataclass
class _SessionState:
    """Per-session connection state for an instrument.

    Tracks which instrument a session has connected to, so disconnect
    can clean up properly.

    Attributes:
        instrument_id: The instrument this session is connected to (empty = none).
        address: VISA address the session connected to.
    """

    instrument_id: str = ""
    address: str = ""


@dataclass
class _InstrumentEntry:
    """Registered instrument entry.

    Attributes:
        driver: The BaseDriver instance backing this instrument.
        sessions: Set of session IDs currently connected to this instrument.
    """

    driver: BaseDriver
    sessions: set[str] = field(default_factory=set)


class _InstrumentServicer(grpc.GenericRpcHandler):
    """gRPC generic RPC handler for InstrumentService.

    Dispatches RPC calls to the InstrumentServer's handler methods.
    Uses GenericRpcHandler to avoid needing generated _pb2_grpc.py stubs.
    """

    def __init__(self, server: InstrumentServer) -> None:
        """Initialize the servicer with a reference to the InstrumentServer.

        Args:
            server: The InstrumentServer that owns this servicer.
        """
        self._server = server

    def service(
        self,
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
        """Return the RPC method handler for the requested method.

        Args:
            handler_call_details: gRPC call details containing method name.

        Returns:
            RpcMethodHandler for the requested method, or None if not found.
        """
        method_path = handler_call_details.method
        # method_path format: "/package.ServiceName/MethodName"
        parts = method_path.strip("/").split("/")
        if len(parts) != 2:
            return None

        service_name, method_name = parts
        if service_name != SERVICE_NAME:
            return None

        if method_name not in SERVICE_METHODS:
            return None

        request_class, response_class = SERVICE_METHODS[method_name]
        handler = getattr(self._server, f"_handle_{method_name.lower()}")

        return grpc.unary_unary_rpc_method_handler(
            handler,
            request_deserializer=request_class.FromString,
            response_serializer=response_class.SerializeToString,
        )


class InstrumentServer:
    """gRPC server that exposes instrument methods as RPC calls.

    Manages instrument registration, multi-client sessions, and RPC routing.
    Thread-safe — multiple clients can issue commands concurrently.

    Attributes:
        _port: Port to listen on.
        _instruments: Registered instrument entries (instrument_id → entry).
        _sessions: Session states (session_id → state).
        _lock: Thread lock for instrument/session registry access.
        _server: Underlying gRPC server (None when stopped).
        _started: Whether the server is currently running.
    """

    def __init__(self, port: int = 50051, host: str = "[::]") -> None:
        """Initialize the instrument server.

        Args:
            port: TCP port to listen on (default 50051).
            host: Bind address (default [::] = all interfaces).
        """
        self._port: int = port
        self._host: str = host
        self._instruments: dict[str, _InstrumentEntry] = {}
        self._sessions: dict[str, _SessionState] = {}
        self._lock: threading.Lock = threading.Lock()
        self._server: grpc.Server | None = None
        self._started: bool = False

    # ------------------------------------------------------------------
    # Instrument registration
    # ------------------------------------------------------------------

    def register_instrument(
        self,
        instrument_id: str,
        driver: BaseDriver,
    ) -> None:
        """Register an instrument driver with the server.

        Args:
            instrument_id: Unique name for this instrument.
            driver: BaseDriver instance to expose via RPC.

        Raises:
            ValueError: If instrument_id is already registered.
        """
        with self._lock:
            if instrument_id in self._instruments:
                msg = f"Instrument '{instrument_id}' already registered"
                raise ValueError(msg)
            self._instruments[instrument_id] = _InstrumentEntry(driver=driver)

    def unregister_instrument(self, instrument_id: str) -> None:
        """Unregister an instrument from the server.

        Disconnects all sessions using this instrument before removal.

        Args:
            instrument_id: Name of the instrument to remove.
        """
        with self._lock:
            entry = self._instruments.pop(instrument_id, None)
            if entry is None:
                return
            # Disconnect all sessions using this instrument
            for session_id in entry.sessions:
                state = self._sessions.get(session_id)
                if state and state.instrument_id == instrument_id:
                    state.instrument_id = ""
                    state.address = ""
            entry.sessions.clear()

    def list_instrument_ids(self) -> list[str]:
        """List all registered instrument IDs.

        Returns:
            List of registered instrument IDs.
        """
        with self._lock:
            return list(self._instruments.keys())

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the gRPC server.

        Raises:
            RuntimeError: If the server is already started.
        """
        if self._started:
            msg = "Server is already started"
            raise RuntimeError(msg)

        self._server = grpc.server(
            ThreadPoolExecutor(max_workers=10),
        )
        self._server.add_generic_rpc_handlers((_InstrumentServicer(self),))
        bind_addr = f"{self._host}:{self._port}"
        self._server.add_insecure_port(bind_addr)
        self._server.start()
        self._started = True

    def stop(self, grace: float | None = 1.0) -> None:
        """Stop the gRPC server.

        Args:
            grace: Grace period in seconds for ongoing RPCs to complete.
        """
        if self._server is not None:
            self._server.stop(grace=grace)
            self._server = None
        self._started = False

    @property
    def is_running(self) -> bool:
        """Check if the server is currently running."""
        return self._started

    @property
    def port(self) -> int:
        """Get the server port."""
        return self._port

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _get_or_create_session(self, session_id: str) -> _SessionState:
        """Get or create session state.

        Args:
            session_id: Session identifier.

        Returns:
            SessionState for the given session.
        """
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = _SessionState()
            return self._sessions[session_id]

    def _get_instrument(self, instrument_id: str) -> _InstrumentEntry:
        """Get a registered instrument entry.

        Args:
            instrument_id: Instrument name.

        Returns:
            The instrument entry.

        Raises:
            ValueError: If the instrument is not registered.
        """
        with self._lock:
            entry = self._instruments.get(instrument_id)
            if entry is None:
                msg = f"Instrument '{instrument_id}' not registered"
                raise ValueError(msg)
            return entry

    def get_active_sessions(self) -> list[str]:
        """List all active session IDs.

        Returns:
            List of session IDs that have a connected instrument.
        """
        with self._lock:
            return [
                sid
                for sid, state in self._sessions.items()
                if state.instrument_id
            ]

    # ------------------------------------------------------------------
    # RPC handlers (called by _InstrumentServicer)
    # ------------------------------------------------------------------

    def _handle_connect(
        self,
        request: ConnectRequest,
        context: grpc.ServicerContext,
    ) -> ConnectResponse:
        """Handle Connect RPC — connect the instrument to a VISA address."""
        try:
            entry = self._get_instrument(request.instrument_id)
            entry.driver.connect(request.address)

            state = self._get_or_create_session(request.session_id)
            state.instrument_id = request.instrument_id
            state.address = request.address

            with self._lock:
                entry.sessions.add(request.session_id)

            return ConnectResponse(success=True, error="")
        except Exception as e:
            return ConnectResponse(success=False, error=str(e))

    def _handle_disconnect(
        self,
        request: DisconnectRequest,
        context: grpc.ServicerContext,
    ) -> DisconnectResponse:
        """Handle Disconnect RPC — disconnect the instrument."""
        try:
            entry = self._get_instrument(request.instrument_id)
            entry.driver.disconnect()

            with self._lock:
                entry.sessions.discard(request.session_id)
                state = self._sessions.get(request.session_id)
                if state is not None:
                    state.instrument_id = ""
                    state.address = ""

            return DisconnectResponse(success=True, error="")
        except Exception as e:
            return DisconnectResponse(success=False, error=str(e))

    def _handle_query(
        self,
        request: QueryRequest,
        context: grpc.ServicerContext,
    ) -> QueryResponse:
        """Handle Query RPC — send a SCPI query and read response."""
        try:
            entry = self._get_instrument(request.instrument_id)
            delay: float | None = None
            if request.HasField("delay"):
                delay = request.delay
            response: str = entry.driver.query(request.command, delay=delay)
            return QueryResponse(response=response, success=True, error="")
        except Exception as e:
            return QueryResponse(response="", success=False, error=str(e))

    def _handle_write(
        self,
        request: WriteRequest,
        context: grpc.ServicerContext,
    ) -> WriteResponse:
        """Handle Write RPC — send a SCPI write command."""
        try:
            entry = self._get_instrument(request.instrument_id)
            entry.driver.write(request.command)
            return WriteResponse(success=True, error="")
        except Exception as e:
            return WriteResponse(success=False, error=str(e))

    def _handle_read(
        self,
        request: ReadRequest,
        context: grpc.ServicerContext,
    ) -> ReadResponse:
        """Handle Read RPC — read a response from the instrument."""
        try:
            entry = self._get_instrument(request.instrument_id)
            response: str = entry.driver.read()
            return ReadResponse(response=response, success=True, error="")
        except Exception as e:
            return ReadResponse(response="", success=False, error=str(e))

    def _handle_getidentity(
        self,
        request: GetIdentityRequest,
        context: grpc.ServicerContext,
    ) -> GetIdentityResponse:
        """Handle GetIdentity RPC — get instrument identity (*IDN?)."""
        try:
            entry = self._get_instrument(request.instrument_id)
            identity: str = entry.driver.query("*IDN?")
            return GetIdentityResponse(identity=identity, success=True, error="")
        except Exception as e:
            return GetIdentityResponse(identity="", success=False, error=str(e))

    def _handle_isconnected(
        self,
        request: IsConnectedRequest,
        context: grpc.ServicerContext,
    ) -> IsConnectedResponse:
        """Handle IsConnected RPC — check if instrument is connected."""
        try:
            entry = self._get_instrument(request.instrument_id)
            connected: bool = entry.driver.is_connected
            return IsConnectedResponse(connected=connected)
        except Exception:
            return IsConnectedResponse(connected=False)

    def _handle_listinstruments(
        self,
        request: ListInstrumentsRequest,
        context: grpc.ServicerContext,
    ) -> ListInstrumentsResponse:
        """Handle ListInstruments RPC — list all registered instruments."""
        response = ListInstrumentsResponse()
        with self._lock:
            for instrument_id, entry in self._instruments.items():
                info: InstrumentInfo = response.instruments.add()
                info.instrument_id = instrument_id
                info.address = entry.driver.address
                info.connected = entry.driver.is_connected
        return response


# Suppress unused import — Any is used in type annotations for RPC handlers
_ = Any
