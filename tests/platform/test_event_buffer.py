"""RH-2 JetStream 文件存储离线事件缓冲测试（设计文档 §5/§10.5）。

覆盖契约：
- **consumer 声明参数**：stream TESTSTATION_EVENTS、durable pull consumer
  AckExplicit + DeliverAll、流显式 FileStorage；
- **publish/fetch/ack 往返**：内存 JS mock（monkeypatch _nats_connect），
  事件发布 -> 拉取 -> 载荷完整 -> ACK 移出待处理；
- **不可达降级**：连接抛错时 publish 返回 False 绝不抛出、
  fetch_pending 返回空列表；
- **配置开关**：nats_file_store_enabled 默认 true；false 时 publish 直接
  False 且不建连接；
- **毒消息**：损坏载荷被 ACK 丢弃，不阻塞后续事件。

真实 nats-server 不可用时全部走内存 mock；若 CI 未来提供 nats-server，
见文末 integration marker 的端到端用例（无 ATE_TEST_NATS_URL 自动跳过）。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

from ate_platform.offline.event_buffer import (
    DURABLE_NAME,
    ENV_NATS_FILE_STORE_ENABLED,
    STREAM_NAME,
    SUBJECT_PREFIX,
    EventBuffer,
    PendingEvent,
    nats_file_store_enabled,
)

# ---------------------------------------------------------------------------
# 内存 JetStream mock（_nats_connect 替身）
# ---------------------------------------------------------------------------


class _Seq:
    def __init__(self, stream: int) -> None:
        self.stream = stream


class _Metadata:
    def __init__(self, stream_seq: int) -> None:
        self.sequence = _Seq(stream_seq)


class FakeMsg:
    def __init__(self, subject: str, data: bytes, stream_seq: int) -> None:
        self.subject = subject
        self.data = data
        self.metadata = _Metadata(stream_seq)
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


class FakeSubscription:
    """pull consumer 替身：从 pending 取一批（不 ACK，交给调用方）。"""

    def __init__(self, store: InMemoryJetStream) -> None:
        self._store = store

    async def fetch(
        self, batch: int = 1, timeout: float | None = 5
    ) -> list[FakeMsg]:
        msgs: list[FakeMsg] = []
        while len(msgs) < batch and self._store.pending:
            seq = next(iter(self._store.pending))
            subject, data = self._store.pending.pop(seq)
            msg = FakeMsg(subject, data, seq)
            self._store.delivered[seq] = msg
            msgs.append(msg)
        return msgs


class InMemoryJetStream:
    """内存 JetStream：记录流/消费者声明 + 消息存储 + pull 语义。"""

    def __init__(self) -> None:
        self.streams: dict[str, dict[str, Any]] = {}
        self.consumers: dict[str, dict[str, Any]] = {}
        self.pending: dict[int, tuple[str, bytes]] = {}
        self.delivered: dict[int, FakeMsg] = {}
        self.published: list[tuple[str, bytes]] = []
        self._seq = 0

    async def stream_info(self, name: str) -> dict[str, Any]:
        if name not in self.streams:
            from nats.js.errors import NotFoundError

            raise NotFoundError(f"stream {name} not found")
        return self.streams[name]

    async def add_stream(self, config: Any) -> dict[str, Any]:
        self.streams[config.name] = {
            "config": config,
            "subjects": list(config.subjects or []),
            "storage": config.storage,
        }
        return self.streams[config.name]

    async def pull_subscribe(
        self,
        subject: str,
        durable: str | None = None,
        stream: str | None = None,
        config: Any = None,
    ) -> FakeSubscription:
        key = f"{stream}:{durable}"
        if key not in self.consumers and config is not None:
            self.consumers[key] = {
                "durable": durable,
                "stream": stream,
                "filter_subject": subject,
                "ack_policy": config.ack_policy,
                "deliver_policy": config.deliver_policy,
            }
        return FakeSubscription(self)

    async def publish(
        self, subject: str, data: bytes, timeout: float | None = None
    ) -> Any:
        self._seq += 1
        self.pending[self._seq] = (subject, data)
        self.published.append((subject, data))
        return type("PubAck", (), {"stream": STREAM_NAME, "seq": self._seq})()


class FakeNatsClient:
    def __init__(self, js: InMemoryJetStream) -> None:
        self._js = js
        self.is_connected = True
        self.drained = False
        self.closed = False

    def jetstream(self) -> InMemoryJetStream:
        return self._js

    async def drain(self) -> None:
        self.drained = True

    async def close(self) -> None:
        self.closed = True


class FakeClock:
    """确定性时钟（离线模块既有约定：显式 advance，无真实等待）。"""

    def __init__(self, t0: float = 1_000.0) -> None:
        self._t = t0

    def advance(self, dt: float) -> None:
        self._t += dt

    def __call__(self) -> float:
        return self._t


@pytest.fixture()
def js() -> InMemoryJetStream:
    return InMemoryJetStream()


@pytest.fixture()
def fake_connect(js, monkeypatch):
    """monkeypatch 模块级连接接缝 -> 内存客户端（记录连接 URL）。"""
    import ate_platform.offline.event_buffer as eb

    calls: list[str] = []

    async def _connect(url: str, **kwargs: Any) -> FakeNatsClient:
        calls.append(url)
        return FakeNatsClient(js)

    monkeypatch.setattr(eb, "_nats_connect", _connect)
    return calls


@pytest.fixture()
def unreachable_connect(monkeypatch):
    """monkeypatch 连接接缝 -> 永远连不上（模拟 NATS 不可达）。"""
    import ate_platform.offline.event_buffer as eb

    async def _connect(url: str, **kwargs: Any) -> FakeNatsClient:
        raise ConnectionError(f"nats unreachable: {url}")

    monkeypatch.setattr(eb, "_nats_connect", _connect)


# ---------------------------------------------------------------------------
# 消费者/流声明参数
# ---------------------------------------------------------------------------


class TestDeclarations:
    async def test_stream_declared_with_file_storage(self, js, fake_connect):
        buf = EventBuffer(nats_url="nats://mock:4222")
        await buf.publish({"kind": "run.started"})

        assert STREAM_NAME in js.streams
        storage = js.streams[STREAM_NAME]["storage"]
        assert storage.value == "file"
        assert js.streams[STREAM_NAME]["subjects"] == [f"{SUBJECT_PREFIX}.>"]

    async def test_stream_created_only_once(self, js, fake_connect):
        buf = EventBuffer()
        await buf.publish({"n": 1})
        await buf.publish({"n": 2})
        # stream_info 第二次命中已存在分支 -> add_stream 只调用一次
        assert len(js.streams) == 1

    async def test_consumer_declared_ack_explicit_deliver_all(self, js, fake_connect):
        buf = EventBuffer()
        await buf.publish({"kind": "x"})
        await buf.fetch_pending(limit=5)

        key = f"{STREAM_NAME}:{DURABLE_NAME}"
        assert key in js.consumers
        consumer = js.consumers[key]
        assert consumer["durable"] == DURABLE_NAME
        assert consumer["ack_policy"].value == "explicit"
        assert consumer["deliver_policy"].value == "all"
        assert consumer["filter_subject"] == f"{SUBJECT_PREFIX}.>"

    async def test_consumer_durable_name_stable(self, js, fake_connect):
        assert DURABLE_NAME == "teststation-event-buffer"


# ---------------------------------------------------------------------------
# publish / fetch / ack 往返（内存 JS mock）
# ---------------------------------------------------------------------------


class TestRoundtrip:
    async def test_publish_returns_true_and_stores_event(self, js, fake_connect):
        buf = EventBuffer(clock=FakeClock())
        ok = await buf.publish({"kind": "step.passed", "step": "s1"})
        assert ok is True
        assert len(js.pending) == 1

    async def test_publish_injects_buffered_at_from_clock(self, js, fake_connect):
        clock = FakeClock(t0=42.0)
        buf = EventBuffer(clock=clock)
        await buf.publish({"kind": "x"})
        _subject, data = js.pending[1]
        assert json.loads(data.decode())["buffered_at"] == 42.0

    async def test_publish_with_subject_suffix(self, js, fake_connect):
        buf = EventBuffer()
        await buf.publish({"kind": "x"}, subject="station-01")
        _subject, data = js.pending[1]
        assert _subject == f"{SUBJECT_PREFIX}.station-01"
        assert json.loads(data.decode())["kind"] == "x"

    async def test_fetch_pending_roundtrip_payload(self, js, fake_connect):
        buf = EventBuffer(clock=FakeClock())
        await buf.publish({"kind": "step.failed", "step": "s2"})
        pending = await buf.fetch_pending(limit=10)
        assert len(pending) == 1
        event = pending[0]
        assert isinstance(event, PendingEvent)
        assert event.payload["kind"] == "step.failed"
        assert event.payload["step"] == "s2"
        assert event.stream_seq == 1

    async def test_fetch_respects_limit(self, js, fake_connect):
        buf = EventBuffer()
        for i in range(3):
            await buf.publish({"n": i})
        pending = await buf.fetch_pending(limit=2)
        assert len(pending) == 2
        assert [p.payload["n"] for p in pending] == [0, 1]

    async def test_ack_removes_event_from_pending(self, js, fake_connect):
        buf = EventBuffer()
        await buf.publish({"kind": "done"})
        pending = await buf.fetch_pending(limit=1)
        seq = pending[0].stream_seq
        ok = await buf.ack(seq)
        assert ok is True
        # durable consumer 语义：ACK 后消息不再投递（mock 侧即 delivered 标记）
        assert js.delivered[seq].acked is True

    async def test_fetch_after_ack_returns_nothing(self, js, fake_connect):
        buf = EventBuffer()
        await buf.publish({"kind": "done"})
        pending = await buf.fetch_pending(limit=1)
        await buf.ack(pending[0].stream_seq)
        # mock 的 pending 已被 fetch 消费；再次 fetch 为空批
        assert await buf.fetch_pending(limit=1) == []


# ---------------------------------------------------------------------------
# 不可达降级（publish 永不抛出）
# ---------------------------------------------------------------------------


class TestUnreachableDegrade:
    async def test_publish_returns_false_when_unreachable(self, unreachable_connect):
        buf = EventBuffer(nats_url="nats://dead:4222")
        assert await buf.publish({"kind": "x"}) is False

    async def test_publish_never_raises_even_on_garbage_event(
        self, js, fake_connect
    ):
        """非 JSON 可序列化载荷（集合）-> 序列化失败仍返回 False 不抛出。"""
        buf = EventBuffer()
        assert await buf.publish({"bad": {1, 2}}) is False

    async def test_fetch_pending_returns_empty_when_unreachable(
        self, unreachable_connect
    ):
        buf = EventBuffer()
        assert await buf.fetch_pending(limit=5) == []

    async def test_ack_returns_false_before_any_fetch(self, js, fake_connect):
        buf = EventBuffer()
        assert await buf.ack(1) is False

    async def test_close_is_idempotent_and_safe(self, js, fake_connect):
        buf = EventBuffer()
        await buf.publish({"kind": "x"})
        await buf.close()
        await buf.close()  # 二次关闭不抛


# ---------------------------------------------------------------------------
# 毒消息（损坏载荷 ACK 丢弃）
# ---------------------------------------------------------------------------


class TestPoisonMessages:
    async def test_corrupt_payload_acked_and_dropped(self, js, fake_connect):
        js.pending[1] = (f"{SUBJECT_PREFIX}.x", b"\xff\xfe not json")
        js._seq = 1
        buf = EventBuffer()
        pending = await buf.fetch_pending(limit=1)
        assert pending == []
        assert js.delivered[1].acked is True

    async def test_corrupt_payload_does_not_block_next_event(
        self, js, fake_connect
    ):
        js.pending[1] = (f"{SUBJECT_PREFIX}.x", b"\xff\xfe not json")
        buf = EventBuffer()
        await buf.publish({"kind": "good"})
        pending = await buf.fetch_pending(limit=10)
        assert len(pending) == 1
        # publish 注入 buffered_at 时间戳 - 只断言业务字段
        assert pending[0].payload["kind"] == "good"


# ---------------------------------------------------------------------------
# 配置开关（两侧）
# ---------------------------------------------------------------------------


class TestConfigToggle:
    def test_platform_flag_defaults_true(self, monkeypatch):
        monkeypatch.delenv(ENV_NATS_FILE_STORE_ENABLED, raising=False)
        assert nats_file_store_enabled() is True

    def test_platform_flag_false_via_env(self, monkeypatch):
        monkeypatch.setenv(ENV_NATS_FILE_STORE_ENABLED, "false")
        assert nats_file_store_enabled() is False

    def test_platform_flag_truthy_values(self, monkeypatch):
        for raw in ("1", "true", "YES", "On"):
            monkeypatch.setenv(ENV_NATS_FILE_STORE_ENABLED, raw)
            assert nats_file_store_enabled() is True, raw
        monkeypatch.setenv(ENV_NATS_FILE_STORE_ENABLED, "0")
        assert nats_file_store_enabled() is False

    async def test_disabled_buffer_publishes_nothing(self, js, fake_connect):
        buf = EventBuffer(enabled=False)
        assert await buf.publish({"kind": "x"}) is False
        assert js.published == []
        assert fake_connect == []  # 禁用时绝不建连接

    async def test_disabled_buffer_fetches_nothing(self, js, fake_connect):
        buf = EventBuffer(enabled=False)
        assert await buf.fetch_pending(limit=5) == []

    def test_cloud_settings_flag_defaults_true(self):
        from ate_cloud.config import Settings

        assert Settings().nats_file_store_enabled is True

    def test_cloud_settings_flag_env_override(self, monkeypatch):
        from ate_cloud.config import Settings

        monkeypatch.setenv("ATE_CLOUD_NATS_FILE_STORE_ENABLED", "false")
        assert Settings().nats_file_store_enabled is False


# ---------------------------------------------------------------------------
# 真实 nats-server 端到端（integration marker）
#
# 无服务器时【快速跳过、绝不挂起】：显式 1s 连接超时 + 0 次重连 +
# try/except（连接失败即 pytest.skip）。绝不依赖 nats-py 默认的
# 60 次×2s 后台重连 -- 那会在无服务器的 CI 里挂死整个测试进程。
# ---------------------------------------------------------------------------

_NATS_URL = os.environ.get("ATE_TEST_NATS_URL", "nats://localhost:4222")


async def _probe_server(url: str) -> Any:
    """快速探测 NATS：成功返回已连接客户端；失败/超时抛异常。

    显式 fail-soft 多重保障：connect_timeout=1s、flush_timeout=1s、
    max_reconnect_attempts=0 + allow_reconnect=False（nats-py 默认
    allow_reconnect=True + 60 次重连是挂死根源；注意 nats-py 2.15 的
    ``_select_next_server`` 在 max_reconnect_attempts=0 时对单台服务器
    仍会无限重试，故外层再包 ``asyncio.wait_for`` 硬超时兜底 --
    防火墙静默丢包时 nats-py 自身超时可能不触发，Windows CI 实测）、
    error_cb 静默（默认回调会把每次重试的 traceback 刷满 stderr）。
    任何失败上抛 -> 用例侧 pytest.skip。
    """
    import nats

    async def _silent_error_cb(exc: Exception) -> None:
        """吞掉 nats-py 连接期重试回调噪音（失败由外层 skip 统一处理）。"""

    coro = nats.connect(
        servers=[url],
        connect_timeout=1,
        max_reconnect_attempts=0,
        flush_timeout=1,
        allow_reconnect=False,
        error_cb=_silent_error_cb,
    )
    return await asyncio.wait_for(coro, timeout=3.0)


@pytest.mark.integration
class TestAgainstRealServer:
    async def test_publish_roundtrip_against_real_server(self):
        """需要真实 nats-server（默认 nats://localhost:4222，无则秒级跳过）。"""
        import nats

        try:
            nc = await _probe_server(_NATS_URL)
        except (TimeoutError, OSError, nats.errors.Error) as exc:
            # 无服务器/端口拒绝/静默丢包 -> 快速跳过，绝不挂起
            pytest.skip(f"nats-server not available at {_NATS_URL}: {exc}")

        js_ctx = nc.jetstream()
        from nats.js.api import StorageType, StreamConfig

        try:
            await js_ctx.stream_info(STREAM_NAME)
        except Exception:
            await js_ctx.add_stream(
                StreamConfig(
                    name=STREAM_NAME,
                    subjects=[f"{SUBJECT_PREFIX}.>"],
                    storage=StorageType.FILE,
                )
            )
        await nc.drain()
        await nc.close()

        # 端到端走生产 API（惰性连接会重连一次；服务器已确认存活）
        buf = EventBuffer(nats_url=_NATS_URL)
        try:
            ok = await buf.publish({"kind": "rh2.e2e", "probe": True})
            assert ok is True
            pending = await buf.fetch_pending(limit=10, timeout=2.0)
            match = [p for p in pending if p.payload.get("kind") == "rh2.e2e"]
            assert match, "published event not fetched back"
            assert await buf.ack(match[0].stream_seq) is True
        finally:
            await buf.close()
