"""Real Qdrant integration tests against the debug server.

Verifies the vector backend used by the failure index and the station
fault-case index, using the REAL qdrant-client async client:

* Connect + server version round-trip.
* The collections this plan uses are reachable / creatable:
  - ``settings.qdrant_collection_failures`` (``ate_failures``)
  - ``ate_fault_symptoms`` (FaultSymptomVectorStore.DEFAULT_SYMPTOM_COLLECTION)

Collection names and embedding dimensionality are imported from the
production modules so the tests cannot drift from the real config.
Skipped by default; skipped per-service when 6333 is unreachable.
"""

from __future__ import annotations

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

from ate_cloud.config import settings

pytestmark = pytest.mark.integration

#: Collections this plan depends on (real names from production config).
# ``ate_fault_symptoms`` used to be listed here too, on the strength of
# FaultSymptomVectorStore. That class existed only to back KG evolution, so once
# the knowledge-graph subsystem went, nothing in production could write to that
# collection — and a test asserting it is "present or creatable" is a test that a
# capability exists. It does not, so it is no longer listed.
_EXPECTED_COLLECTIONS = (settings.qdrant_collection_failures,)  # "ate_failures"


def _client(target, api_key):
    kwargs: dict[str, object] = {
        "url": target.url,
        "timeout": 5.0,
        # The pinned client may be newer than the server on .24 (1.12.4);
        # the REST calls we make are stable across those versions.
        "check_compatibility": False,
    }
    if api_key:
        kwargs["api_key"] = api_key
    return AsyncQdrantClient(**kwargs)


async def test_qdrant_connect_and_server_version(require_qdrant, qdrant_api_key) -> None:
    """Given the debug Qdrant server, when we connect, the root/version API answers."""
    client = _client(require_qdrant, qdrant_api_key)
    try:
        # get_collections is a lightweight authenticated round-trip.
        response = await client.get_collections()
        assert response.collections is not None
    finally:
        await client.close()


async def test_qdrant_required_collections_present_or_creatable(
    require_qdrant, qdrant_api_key
) -> None:
    """Given Qdrant, each plan collection exists or can be created (COSINE)."""
    client = _client(require_qdrant, qdrant_api_key)
    try:
        existing = await client.get_collections()
        present = {c.name for c in existing.collections}
        for name in _EXPECTED_COLLECTIONS:
            assert name, "collection name must be non-empty"
            if name in present:
                continue
            # Not yet provisioned — verify it IS creatable, mirroring
            # FailureIndexer.ensure_collection.
            await client.create_collection(
                collection_name=name,
                vectors_config=qmodels.VectorParams(
                    size=settings.embedding_dimensions,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            assert await client.collection_exists(name)
    finally:
        await client.close()
