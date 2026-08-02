"""Dynamic protobuf message definitions for gRPC instrument RPC.

Since grpcio-tools is not installed (and we avoid code-generation at build time),
we construct the protobuf message descriptors dynamically using the protobuf
descriptor API. This module provides factory functions that create message
classes matching the .proto schema in protobuf/instrument.proto.

The gRPC service itself uses grpc.GenericRpcHandler for method dispatch, so no
generated _pb2_grpc.py stubs are needed either.
"""

from __future__ import annotations

from typing import Any

from google.protobuf import descriptor_pb2 as _descriptor_pb2
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message_factory as _message_factory

# ---------------------------------------------------------------------------
# File descriptor for instrument.proto
# ---------------------------------------------------------------------------

_PACKAGE = "ate_platform.drivers.grpc"
_FILE_NAME = "ate_platform/drivers/grpc/protobuf/instrument.proto"

# Field type constants from descriptor_pb2
_TYPE_DOUBLE = _descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE
_TYPE_STRING = _descriptor_pb2.FieldDescriptorProto.TYPE_STRING
_TYPE_BOOL = _descriptor_pb2.FieldDescriptorProto.TYPE_BOOL
_TYPE_MESSAGE = _descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE

_LABEL_OPTIONAL = _descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
_LABEL_REPEATED = _descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED


def _make_field(
    name: str,
    number: int,
    field_type: int,
    label: int = _LABEL_OPTIONAL,
    type_name: str | None = None,
) -> _descriptor_pb2.FieldDescriptorProto:
    """Create a FieldDescriptorProto."""
    field = _descriptor_pb2.FieldDescriptorProto()
    field.name = name
    field.number = number
    field.type = field_type
    field.label = label
    if type_name is not None:
        field.type_name = type_name
    return field


def _build_file_descriptor() -> _descriptor_pb2.FileDescriptorProto:
    """Build the complete FileDescriptorProto for instrument.proto."""
    fd = _descriptor_pb2.FileDescriptorProto()
    fd.name = _FILE_NAME
    fd.package = _PACKAGE
    fd.syntax = "proto3"

    # --- Message types ---
    # We use fully-qualified type names within the same file.
    prefix = f".{_PACKAGE}."

    # ConnectRequest
    m = fd.message_type.add()
    m.name = "ConnectRequest"
    m.field.add().CopyFrom(_make_field("session_id", 1, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("instrument_id", 2, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("address", 3, _TYPE_STRING))

    # ConnectResponse
    m = fd.message_type.add()
    m.name = "ConnectResponse"
    m.field.add().CopyFrom(_make_field("success", 1, _TYPE_BOOL))
    m.field.add().CopyFrom(_make_field("error", 2, _TYPE_STRING))

    # DisconnectRequest
    m = fd.message_type.add()
    m.name = "DisconnectRequest"
    m.field.add().CopyFrom(_make_field("session_id", 1, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("instrument_id", 2, _TYPE_STRING))

    # DisconnectResponse
    m = fd.message_type.add()
    m.name = "DisconnectResponse"
    m.field.add().CopyFrom(_make_field("success", 1, _TYPE_BOOL))
    m.field.add().CopyFrom(_make_field("error", 2, _TYPE_STRING))

    # QueryRequest (delay uses proto3 optional — needs a synthetic oneof)
    m = fd.message_type.add()
    m.name = "QueryRequest"
    m.field.add().CopyFrom(_make_field("session_id", 1, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("instrument_id", 2, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("command", 3, _TYPE_STRING))
    # proto3 optional: create a synthetic oneof and place delay inside it
    oneof = m.oneof_decl.add()
    oneof.name = "_delay"
    delay_field = _make_field("delay", 4, _TYPE_DOUBLE)
    delay_field.proto3_optional = True
    delay_field.oneof_index = 0  # index into oneof_decl
    m.field.add().CopyFrom(delay_field)

    # QueryResponse
    m = fd.message_type.add()
    m.name = "QueryResponse"
    m.field.add().CopyFrom(_make_field("response", 1, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("success", 2, _TYPE_BOOL))
    m.field.add().CopyFrom(_make_field("error", 3, _TYPE_STRING))

    # WriteRequest
    m = fd.message_type.add()
    m.name = "WriteRequest"
    m.field.add().CopyFrom(_make_field("session_id", 1, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("instrument_id", 2, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("command", 3, _TYPE_STRING))

    # WriteResponse
    m = fd.message_type.add()
    m.name = "WriteResponse"
    m.field.add().CopyFrom(_make_field("success", 1, _TYPE_BOOL))
    m.field.add().CopyFrom(_make_field("error", 2, _TYPE_STRING))

    # ReadRequest
    m = fd.message_type.add()
    m.name = "ReadRequest"
    m.field.add().CopyFrom(_make_field("session_id", 1, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("instrument_id", 2, _TYPE_STRING))

    # ReadResponse
    m = fd.message_type.add()
    m.name = "ReadResponse"
    m.field.add().CopyFrom(_make_field("response", 1, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("success", 2, _TYPE_BOOL))
    m.field.add().CopyFrom(_make_field("error", 3, _TYPE_STRING))

    # GetIdentityRequest
    m = fd.message_type.add()
    m.name = "GetIdentityRequest"
    m.field.add().CopyFrom(_make_field("session_id", 1, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("instrument_id", 2, _TYPE_STRING))

    # GetIdentityResponse
    m = fd.message_type.add()
    m.name = "GetIdentityResponse"
    m.field.add().CopyFrom(_make_field("identity", 1, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("success", 2, _TYPE_BOOL))
    m.field.add().CopyFrom(_make_field("error", 3, _TYPE_STRING))

    # IsConnectedRequest
    m = fd.message_type.add()
    m.name = "IsConnectedRequest"
    m.field.add().CopyFrom(_make_field("session_id", 1, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("instrument_id", 2, _TYPE_STRING))

    # IsConnectedResponse
    m = fd.message_type.add()
    m.name = "IsConnectedResponse"
    m.field.add().CopyFrom(_make_field("connected", 1, _TYPE_BOOL))

    # ListInstrumentsRequest
    m = fd.message_type.add()
    m.name = "ListInstrumentsRequest"
    m.field.add().CopyFrom(_make_field("session_id", 1, _TYPE_STRING))

    # InstrumentInfo
    m = fd.message_type.add()
    m.name = "InstrumentInfo"
    m.field.add().CopyFrom(_make_field("instrument_id", 1, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("address", 2, _TYPE_STRING))
    m.field.add().CopyFrom(_make_field("connected", 3, _TYPE_BOOL))

    # ListInstrumentsResponse
    m = fd.message_type.add()
    m.name = "ListInstrumentsResponse"
    m.field.add().CopyFrom(
        _make_field(
            "instruments", 1, _TYPE_MESSAGE, _LABEL_REPEATED, prefix + "InstrumentInfo"
        )
    )

    return fd


# ---------------------------------------------------------------------------
# Build the pool and message classes (module-level singleton)
# ---------------------------------------------------------------------------

_pool = _descriptor_pool.DescriptorPool()
_fd_proto = _build_file_descriptor()
_fd = _pool.Add(_fd_proto)


def _get_message_class(name: str) -> type[Any]:
    """Get a dynamically-generated protobuf message class by name."""
    full_name = f"{_PACKAGE}.{name}"
    descriptor = _pool.FindMessageTypeByName(full_name)
    return _message_factory.GetMessageClass(descriptor)  # type: ignore[no-any-return]


# Exported message classes — these behave like generated _pb2 classes
ConnectRequest = _get_message_class("ConnectRequest")
ConnectResponse = _get_message_class("ConnectResponse")
DisconnectRequest = _get_message_class("DisconnectRequest")
DisconnectResponse = _get_message_class("DisconnectResponse")
QueryRequest = _get_message_class("QueryRequest")
QueryResponse = _get_message_class("QueryResponse")
WriteRequest = _get_message_class("WriteRequest")
WriteResponse = _get_message_class("WriteResponse")
ReadRequest = _get_message_class("ReadRequest")
ReadResponse = _get_message_class("ReadResponse")
GetIdentityRequest = _get_message_class("GetIdentityRequest")
GetIdentityResponse = _get_message_class("GetIdentityResponse")
IsConnectedRequest = _get_message_class("IsConnectedRequest")
IsConnectedResponse = _get_message_class("IsConnectedResponse")
ListInstrumentsRequest = _get_message_class("ListInstrumentsRequest")
InstrumentInfo = _get_message_class("InstrumentInfo")
ListInstrumentsResponse = _get_message_class("ListInstrumentsResponse")


# ---------------------------------------------------------------------------
# Service definition — method name → (request_class, response_class)
# ---------------------------------------------------------------------------

SERVICE_NAME = f"{_PACKAGE}.InstrumentService"

SERVICE_METHODS: dict[str, tuple[type[Any], type[Any]]] = {
    "Connect": (ConnectRequest, ConnectResponse),
    "Disconnect": (DisconnectRequest, DisconnectResponse),
    "Query": (QueryRequest, QueryResponse),
    "Write": (WriteRequest, WriteResponse),
    "Read": (ReadRequest, ReadResponse),
    "GetIdentity": (GetIdentityRequest, GetIdentityResponse),
    "IsConnected": (IsConnectedRequest, IsConnectedResponse),
    "ListInstruments": (ListInstrumentsRequest, ListInstrumentsResponse),
}


# Suppress unused import warnings — _fd is retained for potential introspection
_ = _fd
