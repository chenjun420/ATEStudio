"""gRPC instrument driver package — remote instrument control over gRPC.

Provides:
- GrpcDriverStub: HAL driver that delegates calls to a remote InstrumentServer.
- InstrumentServer: gRPC server exposing registered instruments as RPC calls.
- Protocol message classes for the InstrumentService gRPC service.
"""

from __future__ import annotations

from ate_platform.drivers.grpc.grpc_driver_stub import GrpcDriverStub
from ate_platform.drivers.grpc.instrument_server import InstrumentServer

__all__ = [
    "GrpcDriverStub",
    "InstrumentServer",
]
