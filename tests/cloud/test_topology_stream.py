"""Topology runtime SSE stream tests (设计文档 §8.3.6，任务 #9).

覆盖：
- /topology-stream 端点返回 EventSourceResponse（正确 content-type）
- SSEBridge 独立 "topology" 流队列：事件按类型入队、与主队列隔离
- 事件 payload 完整（id/type/category/run_id/data/timestamp）
- 流队列引用计数清理（断开归零删除）
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from sse_starlette.sse import EventSourceResponse

from ate_cloud.api.v1.executions import stream_topology_state
from ate_cloud.nats.sse_bridge import SSEBridge


class TestTopologyStreamEndpoint:
    @pytest.mark.asyncio
    async def test_endpoint_returns_event_source(self, app) -> None:
        """端点返回 EventSourceResponse（text/event-stream）。"""
        bridge: SSEBridge = app.state.sse_bridge
        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.app = app

        response = await stream_topology_state(
            run_id="run-topo-1", request=mock_request, bridge=bridge,
        )
        assert isinstance(response, EventSourceResponse)
        assert response.media_type == "text/event-stream"

    @pytest.mark.asyncio
    async def test_generator_delivers_stream_events(self, app) -> None:
        """流队列中的事件按类型被 generator 逐条产出。"""
        bridge: SSEBridge = app.state.sse_bridge
        run_id = "run-topo-gen"

        await bridge.publish_stream_event(
            run_id, "topology", "instrument",
            {"instrument_id": "PSU_MAIN", "status": "active"},
        )
        await bridge.publish_stream_event(
            run_id, "topology", "measurement",
            {"dut_id": "DUT1", "testpoint_id": "TP1", "value": 4.98},
        )

        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.app = app
        mock_request.is_disconnected = AsyncMock(return_value=False)

        response = await stream_topology_state(
            run_id=run_id, request=mock_request, bridge=bridge,
        )

        # 直接迭代 EventSourceResponse 的 body iterator（httpx/ASGITransport
        # 无法流式消费无限 SSE 流，见 test_sse_execution.py 同类说明）。
        seen: list[tuple[str, str]] = []
        body = response.body_iterator
        try:
            for _ in range(2):
                chunk = await asyncio.wait_for(body.__anext__(), timeout=2.0)
                # sse_starlette 产出 ServerSentEvent 对象（event/data 字段）
                seen.append((chunk.event, chunk.data))
        except (TimeoutError, StopAsyncIteration):
            pass

        # 验证事件顺序与内容
        assert seen[0][0] == "instrument"
        assert "PSU_MAIN" in str(seen[0][1])
        assert seen[1][0] == "measurement"
        assert "4.98" in str(seen[1][1])


class TestStreamQueue:
    @pytest.mark.asyncio
    async def test_publish_stream_event_enqueues(self) -> None:
        """publish_stream_event 事件进入独立 topology 队列。"""
        bridge = SSEBridge(nc=None)
        run_id = "run-topo-q"
        await bridge.publish_stream_event(
            run_id, "topology", "link", {"link_id": "L1", "active": True},
        )

        q = bridge.get_stream_queue(run_id, "topology")
        assert q.qsize() == 1
        event = await asyncio.wait_for(q.get(), timeout=1.0)
        assert event["type"] == "link"
        assert event["category"] == "link"
        assert event["run_id"] == run_id
        assert event["data"]["link_id"] == "L1"
        assert event["timestamp"] > 0

    @pytest.mark.asyncio
    async def test_stream_queue_isolated_from_main(self) -> None:
        """topology 流队列与 /events 主队列隔离，无竞争。"""
        bridge = SSEBridge(nc=None)
        run_id = "run-topo-iso"

        q_main = bridge.get_or_create_queue(run_id)
        q_stream = bridge.get_stream_queue(run_id, "topology")
        assert q_main is not q_stream

        await bridge.publish_event(run_id, "EXECUTION_STARTED", {"phase": "start"})
        assert q_main.qsize() == 1
        assert q_stream.qsize() == 0

        await bridge.publish_stream_event(run_id, "topology", "link", {"link_id": "L1"})
        assert q_main.qsize() == 1  # 主队列不收到流事件
        assert q_stream.qsize() == 1

    @pytest.mark.asyncio
    async def test_stream_queue_refcount_cleanup(self) -> None:
        """get_stream_queue/remove_stream_queue 引用计数归零后清理。"""
        bridge = SSEBridge(nc=None)
        run_id = "run-topo-ref"

        _q1 = bridge.get_stream_queue(run_id, "topology")
        _q2 = bridge.get_stream_queue(run_id, "topology")
        assert bridge._stream_refcounts[f"{run_id}:topology"] == 2

        bridge.remove_stream_queue(run_id, "topology")
        assert bridge._stream_refcounts[f"{run_id}:topology"] == 1
        assert f"{run_id}:topology" in bridge._stream_queues

        bridge.remove_stream_queue(run_id, "topology")
        assert f"{run_id}:topology" not in bridge._stream_queues
        assert f"{run_id}:topology" not in bridge._stream_refcounts

    @pytest.mark.asyncio
    async def test_queue_full_drops_oldest(self) -> None:
        """流队列满时丢弃最旧事件（不阻塞发布）。"""
        bridge = SSEBridge(nc=None)
        run_id = "run-topo-full"
        key = f"{run_id}:topology"

        # 预置队列并填满（maxsize=1000 太大，改用直接操纵内部队列）
        q = asyncio.Queue(maxsize=2)
        bridge._stream_queues[key] = q
        bridge._stream_refcounts[key] = 1
        await bridge.publish_stream_event(run_id, "topology", "a", {"n": 1})
        await bridge.publish_stream_event(run_id, "topology", "b", {"n": 2})
        await bridge.publish_stream_event(run_id, "topology", "c", {"n": 3})

        assert q.qsize() == 2
        first = await asyncio.wait_for(q.get(), timeout=1.0)
        assert first["data"]["n"] == 2  # 最旧的 n=1 被丢弃
