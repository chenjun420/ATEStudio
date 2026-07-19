"""Unit tests for ResumeManager."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ate_platform.data.resume import PendingMessage, ResumeManager
from ate_platform.types import StepResult, StepStatus


@pytest.fixture
def mock_cache():
    cache = MagicMock()
    cache._lock = asyncio.Lock()
    cache._db = MagicMock()
    cache._db.execute = AsyncMock()
    cache._db.commit = AsyncMock()
    return cache


@pytest.fixture
def mock_publisher():
    publisher = MagicMock()
    publisher.is_connected = True
    publisher.publish = AsyncMock(return_value=True)
    return publisher


@pytest.fixture
def sample_result():
    return StepResult(status=StepStatus.PASSED, outputs={"value": 42}, error=None)


class TestPendingMessage:
    def test_create_pending_message(self, sample_result):
        msg = PendingMessage(step_id="step-001", result=sample_result, sequence_id="seq-001", retry_count=0)
        assert msg.step_id == "step-001"
        assert msg.result == sample_result
        assert msg.sequence_id == "seq-001"
        assert msg.retry_count == 0

    def test_default_retry_count(self, sample_result):
        msg = PendingMessage(step_id="step-001", result=sample_result)
        assert msg.retry_count == 0


class TestResumeManager:
    def test_init_with_defaults(self, mock_cache, mock_publisher):
        manager = ResumeManager(mock_cache, mock_publisher)
        assert manager._cache == mock_cache
        assert manager._publisher == mock_publisher
        assert manager._max_retries == 3
        assert manager._base_backoff == 1.0
        assert manager._batch_interval == 0.1
        assert manager._running is False

    def test_init_with_none_dependencies(self):
        manager = ResumeManager(None, None)
        assert manager._cache is None
        assert manager._publisher is None

    @pytest.mark.asyncio
    async def test_upload_result_queues_message(self, mock_cache, mock_publisher, sample_result):
        manager = ResumeManager(mock_cache, mock_publisher)
        await manager.upload_result(sample_result, "step-001", "seq-001")
        assert manager._pending.qsize() == 1
        msg = await manager._pending.get()
        assert msg.step_id == "step-001"

    @pytest.mark.asyncio
    async def test_upload_result_without_cache(self, mock_publisher, sample_result):
        manager = ResumeManager(None, mock_publisher)
        await manager.upload_result(sample_result, "step-001")
        assert manager._pending.qsize() == 1

    @pytest.mark.asyncio
    async def test_start_and_stop(self, mock_cache, mock_publisher):
        manager = ResumeManager(mock_cache, mock_publisher, batch_interval=0.01)
        with patch.object(manager, "recover", new_callable=AsyncMock):
            await manager.start()
        assert manager._running is True
        await manager.stop()
        assert manager._running is False

    @pytest.mark.asyncio
    async def test_recover_without_cache(self):
        manager = ResumeManager(None, None)
        await manager.recover()

    @pytest.mark.asyncio
    async def test_retry_pending_without_cache(self):
        manager = ResumeManager(None, None)
        await manager.retry_pending()

    def test_build_payload(self, mock_cache, mock_publisher, sample_result):
        manager = ResumeManager(mock_cache, mock_publisher)
        message = PendingMessage(step_id="step-001", result=sample_result, sequence_id="seq-001")
        payload = manager._build_payload(message)
        assert isinstance(payload, bytes)
        data = json.loads(payload)
        assert data["step_id"] == "step-001"
        assert data["status"] == "PASSED"

    @pytest.mark.asyncio
    async def test_upload_loop_processes_messages(self, mock_cache, mock_publisher, sample_result):
        manager = ResumeManager(mock_cache, mock_publisher, batch_interval=0.01, base_backoff=0.01)
        with patch.object(manager, "recover", new_callable=AsyncMock):
            await manager.start()
        await manager.upload_result(sample_result, "step-001")
        await asyncio.sleep(0.2)
        mock_publisher.publish.assert_called()
        await manager.stop()

    @pytest.mark.asyncio
    async def test_upload_loop_handles_disconnected_publisher(self, mock_cache, sample_result):
        mock_publisher = MagicMock()
        mock_publisher.is_connected = False
        manager = ResumeManager(mock_cache, mock_publisher, batch_interval=0.01)
        with patch.object(manager, "recover", new_callable=AsyncMock):
            await manager.start()
        await manager.upload_result(sample_result, "step-001")
        await asyncio.sleep(0.1)
        mock_publisher.publish.assert_not_called()
        await manager.stop()

    @pytest.mark.asyncio
    async def test_full_workflow(self, mock_cache, mock_publisher, sample_result):
        manager = ResumeManager(mock_cache, mock_publisher, batch_interval=0.01, base_backoff=0.01)
        with patch.object(manager, "recover", new_callable=AsyncMock):
            await manager.start()
        await manager.upload_result(sample_result, "step-001")
        await asyncio.sleep(0.2)
        await manager.stop()
        mock_publisher.publish.assert_called()
