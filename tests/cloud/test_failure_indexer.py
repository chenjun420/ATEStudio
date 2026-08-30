"""Test FailureIndexer — RAG failure diagnosis via Qdrant.

All tests use mocked Qdrant client and embedding model to avoid
requiring a running Qdrant instance.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from qdrant_client.http import models as qmodels

from ate_cloud.services.failure_indexer import FailureIndexer
from shared.events import Event, EventType

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_qdrant() -> Generator[MagicMock, None, None]:
    """Mock Qdrant client with tracking for collections and points."""
    client = MagicMock()
    # Simulate get_collections returning empty (collection doesn't exist yet)
    collections = MagicMock()
    collections.collections = []
    client.get_collections.return_value = collections
    # Track upserted points for assertions
    client._upserted_points: list[dict] = []
    client.upsert.side_effect = lambda **kwargs: client._upserted_points.extend(
        kwargs.get("points", [])
    )
    # Track created collections
    client._created_collections: list[str] = []
    client.create_collection.side_effect = lambda **kwargs: client._created_collections.append(
        kwargs.get("collection_name", "unknown")
    )
    yield client


@pytest.fixture
def mock_embedding() -> MagicMock:
    """Mock embedding model that returns configurable vectors."""
    embed = MagicMock()
    embed.return_value = [0.1, 0.2, 0.3]  # default 3-dim for test
    return embed


@pytest.fixture
def indexer(mock_qdrant: MagicMock, mock_embedding: MagicMock) -> FailureIndexer:
    """Create a FailureIndexer with mocked dependencies."""
    return FailureIndexer(
        qdrant_client=mock_qdrant,
        embedding_model=mock_embedding,
        collection_name="ate_failures",
        embedding_dim=3,  # small for tests
    )


@pytest.fixture
def step_failed_event() -> Event:
    """Create a STEP_FAILED event with realistic metadata."""
    return Event(
        type=EventType.STEP_FAILED,
        data={
            "step_id": "rf_cal",
            "failed_step_id": "rf_cal",
            "failed_step_name": "RF Calibration",
            "error_message": "VISA timeout on instrument DMM_CH1",
            "variable_snapshot": {"voltage": 3.28, "temperature": 42.5},
            "step_history": ["power_on:passed", "voltage_check:passed"],
            "sequence_yaml": "name: full_test\nsteps:\n  - id: rf_cal",
            "run_id": "run-001",
        },
    )


@pytest.fixture
def execution_failed_event() -> Event:
    """Create an EXECUTION_COMPLETED event with FAILED result."""
    return Event(
        type=EventType.EXECUTION_COMPLETED,
        data={
            "run_id": "run-001",
            "plan_name": "full_test",
            "status": "FAILED",
            "sequence_yaml": "name: full_test\nsteps: []",
            "error_message": "3 of 5 steps failed",
            "step_history": ["power_on:passed", "rf_cal:failed", "rf_power:skipped"],
        },
    )


@pytest.fixture
def execution_completed_event() -> Event:
    """Create an EXECUTION_COMPLETED event with COMPLETED result (should NOT be indexed)."""
    return Event(
        type=EventType.EXECUTION_COMPLETED,
        data={
            "run_id": "run-002",
            "plan_name": "full_test",
            "status": "COMPLETED",
            "sequence_yaml": "name: full_test\nsteps: []",
        },
    )


# ---------------------------------------------------------------------------
# Tests: ensure_collection
# ---------------------------------------------------------------------------


class TestEnsureCollection:
    """Tests for collection creation on startup."""

    @pytest.mark.asyncio
    async def test_creates_collection_when_missing(self, indexer: FailureIndexer, mock_qdrant: MagicMock) -> None:
        """ensure_collection creates collection when it doesn't exist."""
        await indexer.ensure_collection()
        assert mock_qdrant.create_collection.called
        call_kwargs = mock_qdrant.create_collection.call_args[1]
        assert call_kwargs["collection_name"] == "ate_failures"
        assert call_kwargs["vectors_config"].size == 3
        assert call_kwargs["vectors_config"].distance == qmodels.Distance.COSINE

    @pytest.mark.asyncio
    async def test_skips_when_collection_exists(self, indexer: FailureIndexer, mock_qdrant: MagicMock) -> None:
        """ensure_collection skips when collection already exists."""
        from qdrant_client.http.models import CollectionDescription

        mock_qdrant.get_collections.return_value.collections = [
            CollectionDescription(name="ate_failures")
        ]
        mock_qdrant.create_collection.reset_mock()

        await indexer.ensure_collection()
        assert not mock_qdrant.create_collection.called

    @pytest.mark.asyncio
    async def test_survives_get_collections_failure(
        self, indexer: FailureIndexer, mock_qdrant: MagicMock
    ) -> None:
        """ensure_collection handles Qdrant errors gracefully."""
        mock_qdrant.get_collections.side_effect = ConnectionError("no qdrant")
        # Should not raise
        await indexer.ensure_collection()


# ---------------------------------------------------------------------------
# Tests: _should_index
# ---------------------------------------------------------------------------


class TestShouldIndex:
    """Tests for event filtering logic."""

    def test_indexes_step_failed(self, indexer: FailureIndexer, step_failed_event: Event) -> None:
        """STEP_FAILED events should be indexed."""
        assert indexer._should_index(step_failed_event) is True

    def test_indexes_execution_completed_failed(
        self, indexer: FailureIndexer, execution_failed_event: Event
    ) -> None:
        """EXECUTION_COMPLETED with FAILED status should be indexed."""
        assert indexer._should_index(execution_failed_event) is True

    def test_skips_execution_completed_passed(
        self, indexer: FailureIndexer, execution_completed_event: Event
    ) -> None:
        """EXECUTION_COMPLETED with COMPLETED status should NOT be indexed."""
        assert indexer._should_index(execution_completed_event) is False

    def test_skips_step_completed(self, indexer: FailureIndexer) -> None:
        """STEP_COMPLETED events should NOT be indexed."""
        event = Event(
            type=EventType.STEP_COMPLETED,
            data={"step_id": "step1", "status": "PASSED"},
        )
        assert indexer._should_index(event) is False

    def test_skips_step_started(self, indexer: FailureIndexer) -> None:
        """STEP_STARTED events should NOT be indexed."""
        event = Event(
            type=EventType.STEP_STARTED,
            data={"step_id": "step1"},
        )
        assert indexer._should_index(event) is False


# ---------------------------------------------------------------------------
# Tests: index_failure (metadata extraction)
# ---------------------------------------------------------------------------


class TestIndexFailureMetadata:
    """Tests for metadata extraction from failure events."""

    def test_extracts_metadata_from_step_failed(
        self, indexer: FailureIndexer, step_failed_event: Event, mock_qdrant: MagicMock
    ) -> None:
        """STEP_FAILED metadata: failed_step_id, failed_step_name, error_message, variable_snapshot."""
        metadata = indexer._extract_metadata(step_failed_event)
        assert metadata["failed_step_id"] == "rf_cal"
        assert metadata["failed_step_name"] == "RF Calibration"
        assert metadata["error_message"] == "VISA timeout on instrument DMM_CH1"
        assert metadata["variable_snapshot"] == {"voltage": 3.28, "temperature": 42.5}
        assert metadata["sequence_yaml"].startswith("name: full_test")
        assert metadata["run_id"] == "run-001"
        assert metadata["event_type"] == "STEP_FAILED"

    def test_extracts_missing_step_id_from_data(
        self, indexer: FailureIndexer, mock_qdrant: MagicMock
    ) -> None:
        """When failed_step_id is missing, falls back to step_id."""
        event = Event(
            type=EventType.STEP_FAILED,
            data={"step_id": "step_x", "error_message": "oops"},
        )
        metadata = indexer._extract_metadata(event)
        assert metadata["failed_step_id"] == "step_x"

    def test_extracts_metadata_from_execution_failed(
        self, indexer: FailureIndexer, execution_failed_event: Event, mock_qdrant: MagicMock
    ) -> None:
        """EXECUTION_COMPLETED(FAILED) metadata includes plan_name, status=FailED."""
        metadata = indexer._extract_metadata(execution_failed_event)
        assert metadata["run_id"] == "run-001"
        assert metadata["plan_name"] == "full_test"
        assert metadata["status"] == "FAILED"
        assert metadata["event_type"] == "EXECUTION_COMPLETED"

    def test_build_embed_text_concatenates_fields(self, indexer: FailureIndexer) -> None:
        """Embed text concatenates failed_step_name + error_message + variable_snapshot."""
        metadata = {
            "failed_step_name": "RF Calibration",
            "error_message": "VISA timeout",
            "variable_snapshot": {"voltage": 3.3},
        }
        text = indexer._build_embed_text(metadata)
        assert "RF Calibration" in text
        assert "VISA timeout" in text
        assert "voltage" in text

    def test_build_embed_text_falls_back_to_step_id(self, indexer: FailureIndexer) -> None:
        """When failed_step_name is missing, uses failed_step_id."""
        metadata = {
            "failed_step_id": "step_x",
            "error_message": "oops",
        }
        text = indexer._build_embed_text(metadata)
        assert "step_x" in text
        assert "oops" in text

    def test_build_embed_text_empty_metadata(self, indexer: FailureIndexer) -> None:
        """Empty metadata produces empty embed string."""
        text = indexer._build_embed_text({})
        assert text == ""

    def test_extract_step_history(self, indexer: FailureIndexer, step_failed_event: Event) -> None:
        """Step history is included in metadata."""
        metadata = indexer._extract_metadata(step_failed_event)
        assert metadata["step_history"] == ["power_on:passed", "voltage_check:passed"]


# ---------------------------------------------------------------------------
# Tests: search_similar_failures
# ---------------------------------------------------------------------------


class TestSearchSimilarFailures:
    """Tests for similarity search."""

    @pytest.mark.asyncio
    async def test_returns_empty_on_qdrant_error(
        self, indexer: FailureIndexer, mock_qdrant: MagicMock
    ) -> None:
        """search_similar_failures returns empty list on Qdrant error."""
        mock_qdrant.search.side_effect = ConnectionError("no qdrant")
        results = await indexer.search_similar_failures("VISA timeout")
        assert results == []

    @pytest.mark.asyncio
    async def test_returns_ranked_results(
        self, indexer: FailureIndexer, mock_qdrant: MagicMock, mock_embedding: MagicMock
    ) -> None:
        """search_similar_failures returns scored results with payload."""
        mock_embedding.return_value = [0.1, 0.2, 0.3]
        from qdrant_client.http.models import ScoredPoint

        mock_qdrant.search.return_value = [
            ScoredPoint(
                id="pt-1",
                version=1,
                score=0.95,
                vector=None,
                payload={
                    "failed_step_name": "RF Calibration",
                    "error_message": "VISA timeout on DMM_CH1",
                    "run_id": "run-001",
                },
            ),
            ScoredPoint(
                id="pt-2",
                version=1,
                score=0.78,
                vector=None,
                payload={
                    "failed_step_name": "Power On Test",
                    "error_message": "timeout on instrument",
                    "run_id": "run-005",
                },
            ),
        ]

        results = await indexer.search_similar_failures("VISA timeout", top_k=2)

        assert len(results) == 2
        assert results[0]["id"] == "pt-1"
        assert results[0]["score"] == 0.95
        assert results[0]["failed_step_name"] == "RF Calibration"
        assert results[1]["id"] == "pt-2"
        assert results[1]["score"] == 0.78

    @pytest.mark.asyncio
    async def test_embeds_query_before_search(
        self, indexer: FailureIndexer, mock_embedding: MagicMock, mock_qdrant: MagicMock
    ) -> None:
        """Query text is embedded before calling Qdrant search."""
        mock_embedding.return_value = [0.5, 0.6, 0.7]
        mock_qdrant.search.return_value = []

        await indexer.search_similar_failures("VISA timeout on DMM_CH1")

        mock_embedding.assert_called_once_with("VISA timeout on DMM_CH1")
        mock_qdrant.search.assert_called_once()
        search_kwargs = mock_qdrant.search.call_args[1]
        assert search_kwargs["query_vector"] == [0.5, 0.6, 0.7]
        assert search_kwargs["limit"] == 5  # default top_k


# ---------------------------------------------------------------------------
# Tests: non-blocking indexing
# ---------------------------------------------------------------------------


class TestNonBlockingIndexing:
    """Tests that indexing does not block execution flow."""

    def test_index_failure_does_not_block(self, indexer: FailureIndexer, step_failed_event: Event) -> None:
        """index_failure() returns immediately (is non-blocking)."""
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Must start the loop so create_task has a running loop
            async def _run() -> None:
                return_value = indexer.index_failure(step_failed_event)
                assert return_value is None
                # Let the background task complete
                await asyncio.sleep(0.1)

            loop.run_until_complete(_run())
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    def test_index_failure_creates_task(
        self, indexer: FailureIndexer, step_failed_event: Event, mock_qdrant: MagicMock
    ) -> None:
        """index_failure schedules async task."""
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            async def _run() -> None:
                indexer.index_failure(step_failed_event)
                await asyncio.sleep(0.1)

            loop.run_until_complete(_run())
            # The upsert should have been called
            assert mock_qdrant.upsert.called
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    def test_qdrant_error_does_not_raise_to_caller(
        self, indexer: FailureIndexer, step_failed_event: Event, mock_qdrant: MagicMock
    ) -> None:
        """Qdrant failure does not propagate to index_failure caller."""
        mock_qdrant.upsert.side_effect = ConnectionError("qdrant down")
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            async def _run() -> None:
                # This should not raise
                indexer.index_failure(step_failed_event)
                await asyncio.sleep(0.1)

            loop.run_until_complete(_run())
        finally:
            loop.close()
            asyncio.set_event_loop(None)


# ---------------------------------------------------------------------------
# Tests: subscribe_to_events
# ---------------------------------------------------------------------------


class TestSubscribeToEvents:
    """Tests for event subscription via SSEBridge."""

    @pytest.mark.asyncio
    async def test_subscribe_with_none_bridge(self, indexer: FailureIndexer) -> None:
        """subscribe_to_events with None bridge is a no-op."""
        indexer.subscribe_to_events(None)
        # Should not raise and should log warning

    @pytest.mark.asyncio
    async def test_subscribe_patches_publish_event(
        self, indexer: FailureIndexer, mock_qdrant: MagicMock
    ) -> None:
        """subscribe_to_events wraps bridge.publish_event to intercept failures."""
        from ate_cloud.nats.sse_bridge import SSEBridge

        bridge = SSEBridge(nc=None)

        # We need to track that the indexer's _should_index was checked
        indexer.subscribe_to_events(bridge)

        # bridge.publish_event should now be patched
        assert bridge.publish_event is not SSEBridge.publish_event

        # Publish a STEP_FAILED event through the bridge
        await bridge.publish_event(
            run_id="run-test",
            event_type="STEP_FAILED",
            data={
                "step_id": "step_abc",
                "failed_step_name": "Test Step",
                "error_message": "test error",
            },
        )

        # Allow async indexing task to complete
        await asyncio.sleep(0.1)

        # Verify upsert was called
        assert mock_qdrant.upsert.called

    @pytest.mark.asyncio
    async def test_subscribe_ignores_non_failure_events(
        self, indexer: FailureIndexer, mock_qdrant: MagicMock
    ) -> None:
        """Bridge patch does NOT index non-failure events."""
        from ate_cloud.nats.sse_bridge import SSEBridge

        bridge = SSEBridge(nc=None)
        indexer.subscribe_to_events(bridge)

        # Publish a STEP_STARTED event (not a failure)
        await bridge.publish_event(
            run_id="run-test",
            event_type="STEP_STARTED",
            data={"step_id": "step_abc"},
        )

        await asyncio.sleep(0.1)

        # Upsert should NOT have been called
        assert not mock_qdrant.upsert.called


# ---------------------------------------------------------------------------
# Tests: embedding failure handling
# ---------------------------------------------------------------------------


class TestEmbeddingFailureHandling:
    """Tests for graceful degradation on embedding failures."""

    @pytest.mark.asyncio
    async def test_embed_returns_zero_vector_on_failure(self, indexer: FailureIndexer) -> None:
        """Embedding failure returns zero vector instead of raising."""
        indexer._embedding_model.side_effect = RuntimeError("model not loaded")
        result = await indexer._embed("some text")
        assert result == [0.0, 0.0, 0.0]

    @pytest.mark.asyncio
    async def test_embed_empty_text_returns_zeros(self, indexer: FailureIndexer, mock_embedding: MagicMock) -> None:
        """Empty text skips embedding model entirely."""
        result = await indexer._embed("")
        assert result == [0.0, 0.0, 0.0]
        mock_embedding.assert_not_called()
