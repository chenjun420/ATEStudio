"""Real NATS JetStream integration tests against the debug server.

Verifies the cloud/edge messaging fabric the station-handoff and
edge-worker features depend on, using the REAL nats-py client:

* TCP connect + JetStream enabled.
* The streams created by ``StreamManager`` (ATE_TASKS, ATE_STATUS,
  ATE_EXECUTION_EVENTS, ATE_DEAD_LETTERS) are present or creatable.
* The worker-registry KV bucket ``ate-workers`` (stream_manager) and the
  station-handoff KV bucket ``ate-handoffs`` (station_orchestrator /
  shared.multi_station) are present or creatable.

Skipped by default (gate in conftest); skipped per-service when 4222 is
unreachable. Stream/KV names are imported from the production modules so
the tests can never drift from the names the app actually uses.
"""

from __future__ import annotations

import nats
import pytest
from nats.js.errors import NotFoundError

from ate_cloud.nats.stream_manager import (
    _ATE_DEAD_LETTERS_CONFIG,
    _ATE_EXECUTION_EVENTS_CONFIG,
    _ATE_STATUS_CONFIG,
    _ATE_TASKS_CONFIG,
    _WORKER_KV_BUCKET,
)
from ate_platform.scheduler.station_orchestrator import HANDOFF_KV_BUCKET

pytestmark = pytest.mark.integration

#: Streams the cloud lifespan creates (idempotently) at startup.
_EXPECTED_STREAMS = (
    _ATE_TASKS_CONFIG.name,
    _ATE_STATUS_CONFIG.name,
    _ATE_EXECUTION_EVENTS_CONFIG.name,
    _ATE_DEAD_LETTERS_CONFIG.name,
)
#: JetStream KV buckets the platform uses.
_EXPECTED_KV_BUCKETS = (_WORKER_KV_BUCKET, HANDOFF_KV_BUCKET)


async def _connect(target, credentials):
    user, password = credentials
    kwargs: dict[str, object] = {"connect_timeout": 5}
    if user:
        kwargs["user"] = user
        kwargs["password"] = password
    return await nats.connect(target.url, **kwargs)


async def test_nats_connect_and_jetstream_enabled(require_nats, nats_credentials) -> None:
    """Given the debug NATS server, when we connect, JetStream must answer."""
    nc = await _connect(require_nats, nats_credentials)
    try:
        js = nc.jetstream()
        info = await js.account_info()
        # JetStream enabled ⇒ account_info returns a report (no raise).
        assert info is not None
    finally:
        await nc.close()


async def test_nats_required_streams_present_or_creatable(require_nats, nats_credentials) -> None:
    """Given JetStream, each platform stream exists or can be created idempotently."""
    nc = await _connect(require_nats, nats_credentials)
    try:
        js = nc.jetstream()
        for name in _EXPECTED_STREAMS:
            assert name, "stream config must carry a name"
            try:
                await js.stream_info(name)
            except NotFoundError:
                # Not yet created by a cloud deploy — verify it IS creatable.
                config = next(c for c in (
                    _ATE_TASKS_CONFIG,
                    _ATE_STATUS_CONFIG,
                    _ATE_EXECUTION_EVENTS_CONFIG,
                    _ATE_DEAD_LETTERS_CONFIG,
                ) if c.name == name)
                await js.add_stream(config=config)
                await js.stream_info(name)  # round-trip confirms creation
    finally:
        await nc.close()


async def test_nats_kv_buckets_present_or_creatable(require_nats, nats_credentials) -> None:
    """Given JetStream, the worker + handoff KV buckets exist or are creatable."""
    nc = await _connect(require_nats, nats_credentials)
    try:
        js = nc.jetstream()
        for bucket in _EXPECTED_KV_BUCKETS:
            try:
                await js.key_value(bucket)
            except NotFoundError:
                # Bucket not yet provisioned — verify it can be created.
                await js.create_key_value(bucket=bucket)
                await js.key_value(bucket)
    finally:
        await nc.close()
