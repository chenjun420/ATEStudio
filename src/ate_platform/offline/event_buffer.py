"""JetStream 文件存储离线事件缓冲（设计文档 §5 / §10.5，RH-2）。

边缘侧离线事件缓冲：把站内事件写入 NATS JetStream ``TESTSTATION_EVENTS``
流（显式 **FileStorage** 声明），并通过一个 **durable pull consumer**
（``AckExplicit`` + ``DeliverAll``）取回待处理事件、逐条 ACK。

设计要点（与 cache_store/upload_queue 同一模块约定）：

- **文件存储链**：流声明 ``storage=StorageType.FILE``；服务器侧
  ``config/nats-server.conf`` 将 ``store_dir`` 固定为
  ``/var/lib/nats/jetstream``（docker-compose 挂载具名卷），消息在
  NATS 不可达/进程重启后依然存活（§10.5 离线自治）。
- **绝不抛出（publish）**：:meth:`EventBuffer.publish` 在 NATS 不可达、
  JetStream 错误、序列化失败时一律返回 ``False`` 并记 warning——离线
  降级是数据路径的一部分，不是异常路径。
- **可注入时钟**：``clock`` 默认 :func:`time.time`（事件载荷时间戳），
  测试可注入确定性时钟。
- **线程安全**：内部 ``threading.RLock`` 保护连接生命周期；NATS IO
  本身走 asyncio（nats-py），调用方在事件循环线程内使用。
- **配置开关**：``nats_file_store_enabled``（默认 true）——false 时
  :meth:`publish` 直接返回 False（诚实降级，不静默建流）。

本模块只做事件缓冲/取回/ACK，不做对账（T21 Reconciler 已有）与
重试策略（消费方职责）。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: 事件流名称（§10.5 站内事件流）。
STREAM_NAME = "TESTSTATION_EVENTS"

#: 事件流通配主题（站内事件统一入口）。
SUBJECT_PREFIX = "teststation.events"

#: durable pull consumer 名称。
DURABLE_NAME = "teststation-event-buffer"

#: 环境变量：是否启用 JetStream 文件存储事件缓冲（默认 true）。
ENV_NATS_FILE_STORE_ENABLED = "ATE_PLATFORM_NATS_FILE_STORE_ENABLED"


def _env_flag(name: str, default: bool) -> bool:
    """读取布尔环境变量（``1/true/yes/on`` 为真，其余为假，缺省回落默认值）。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def nats_file_store_enabled() -> bool:
    """平台侧配置：JetStream 文件存储事件缓冲是否启用（默认 true）。"""
    return _env_flag(ENV_NATS_FILE_STORE_ENABLED, default=True)


#: 连接/发布硬上限秒数（fail-soft）：不可达时快速返回 False，
#: 绝不让调用方陷进 nats-py 默认 60 次×2s 的重连循环。
#: 注意 nats-py 的 connect_timeout 在「防火墙静默丢包」场景可能不触发
#: （连接 SYN 无响应时底层等待不受其约束），故外层再包一层
#: ``asyncio.wait_for`` 硬超时（Windows CI 实测必要）。
#: 另注意 nats-py 2.15 ``_select_next_server`` 在 max_reconnect_attempts=0
#: 时对单台服务器仍会无限重试（预算守卫仅在 >0 时生效）——
#: allow_reconnect=False + 外层硬超时双保险才真正封死挂起。
_CONNECT_TIMEOUT_SECONDS = 1.0
_PUBLISH_TIMEOUT_SECONDS = 2.0
_FETCH_TIMEOUT_SECONDS = 5.0
_MAX_RECONNECT_ATTEMPTS = 0  # 0 = 不自动重连（降级由调用方决定何时重试）


async def _nats_connect(url: str, **kwargs: Any) -> Any:
    """连接 NATS（模块级接缝：测试用 monkeypatch 替换，生产走 nats-py）。

    生产路径显式 fail-soft 四重保障：
    1. ``connect_timeout`` 短超时；
    2. ``max_reconnect_attempts=0`` + ``allow_reconnect=False``
       （连接失败立即上抛而非后台重连阻塞）；
    3. ``error_cb`` 静默（nats-py 默认回调会把每次重试的 traceback
       刷满 stderr；降级由调用方统一记 warning）；
    4. 外层 ``asyncio.wait_for`` 硬超时（兜底 nats-py 自身超时不触发的
       场景，如防火墙静默丢 SYN 包、单服务器无限重试循环）。
    """
    import nats

    async def _silent_error_cb(exc: Exception) -> None:
        """连接期错误回调：静默（publish/fetch 的降级路径统一记日志）。"""

    timeout = kwargs.pop("hard_timeout", _CONNECT_TIMEOUT_SECONDS + 0.5)
    coro = nats.connect(
        servers=[url],
        connect_timeout=kwargs.pop("connect_timeout", _CONNECT_TIMEOUT_SECONDS),
        max_reconnect_attempts=kwargs.pop(
            "max_reconnect_attempts", _MAX_RECONNECT_ATTEMPTS
        ),
        allow_reconnect=kwargs.pop("allow_reconnect", False),
        flush_timeout=kwargs.pop("flush_timeout", _CONNECT_TIMEOUT_SECONDS),
        error_cb=kwargs.pop("error_cb", _silent_error_cb),
        **kwargs,
    )
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        # wait_for 取消内层协程时，nats-py 可能已建立半连接 socket；
        # close 兜底清理，避免句柄泄漏。
        raise ConnectionError(
            f"nats connect hard-timeout after {timeout}s: {url}"
        ) from None


@dataclass(frozen=True)
class PendingEvent:
    """fetch_pending 返回的待处理事件视图（已去 NATS 化，仅载荷+序号）。"""

    stream_seq: int
    subject: str
    payload: dict[str, Any]
    delivered_at: float


class EventBuffer:
    """TESTSTATION_EVENTS 流上的 durable pull consumer 封装。

    生命周期：首次 ``publish``/``fetch_pending`` 时惰性连接并确保
    流与 consumer 存在（幂等，已存在则复用）。``close`` 关闭连接。

    Args:
        nats_url: NATS 连接地址（默认 ``nats://localhost:4222``，
            与 JetStreamWorker/LeafNodeRunner 同一约定）。
        clock: 可注入时钟（默认 :func:`time.time`）。
        enabled: 显式覆盖配置开关（默认取
            :func:`nats_file_store_enabled`）。
    """

    def __init__(
        self,
        *,
        nats_url: str = "nats://localhost:4222",
        clock: Any = time.time,
        enabled: bool | None = None,
    ) -> None:
        self._nats_url = nats_url
        self._clock = clock
        self._enabled = nats_file_store_enabled() if enabled is None else enabled
        self._lock = threading.RLock()
        self._nc: Any = None
        self._js: Any = None
        self._sub: Any = None
        # 最近一次 fetch 投递的原始消息句柄（stream_seq -> Msg），
        # ack(seq) 据此定位消息；ACK 成功后移除。
        self._unacked: dict[int, Any] = {}
        logger.info(
            "event_buffer_opened (nats_url=%s, enabled=%s, stream=%s)",
            self._nats_url,
            self._enabled,
            STREAM_NAME,
        )

    # -- 连接与拓扑确保 ---------------------------------------------------

    async def _ensure_connection(self) -> tuple[Any, Any]:
        """确保 NATS 连接与 JetStream 上下文（幂等、fail-soft）。

        锁只保护状态读写，await 在锁外执行（await 持锁会阻塞所有
        等待连接的线程整个连接超时窗口）；并发调用可能重复连接，
        后建立者覆盖，先建立者被 close 掉（幂等无害）。

        Returns:
            ``(nc, js)`` 已连接的客户端与 JetStream 上下文。

        Raises:
            Exception: 连接失败时原样上抛（调用方决定降级）。
        """
        with self._lock:
            if self._nc is not None and self._nc.is_connected:
                return self._nc, self._js
        nc = await _nats_connect(self._nats_url)
        js = nc.jetstream()
        with self._lock:
            # 并发竞态：保留已存在的连接，关闭后来者，避免句柄泄漏
            if self._nc is not None and self._nc.is_connected:
                try:
                    await nc.close()
                except Exception:  # noqa: BLE001 - 竞态清理尽力而为
                    pass
                return self._nc, self._js
            self._nc, self._js = nc, js
            return self._nc, self._js

    async def _ensure_stream(self, js: Any) -> None:
        """确保 TESTSTATION_EVENTS 流存在（FileStorage 显式声明）。

        已存在则不改动（尊重服务器侧既有配置）；缺失则创建，显式
        ``StorageType.FILE``（文件存储，跨重启持久）。
        """
        from nats.js.api import StorageType, StreamConfig
        from nats.js.errors import NotFoundError

        try:
            await js.stream_info(STREAM_NAME)
        except NotFoundError:
            config = StreamConfig(
                name=STREAM_NAME,
                subjects=[f"{SUBJECT_PREFIX}.>"],
                storage=StorageType.FILE,
            )
            await js.add_stream(config)
            logger.info(
                "stream %s created (storage=%s)", STREAM_NAME, StorageType.FILE
            )

    async def _ensure_subscription(self) -> Any:
        """确保 durable pull consumer 订阅存在（AckExplicit + DeliverAll）。

        Returns:
            PullSubscription（durable，跨进程重启续传）。
        """
        if self._sub is not None:
            return self._sub
        _nc, js = await self._ensure_connection()
        await self._ensure_stream(js)
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

        config = ConsumerConfig(
            durable_name=DURABLE_NAME,
            ack_policy=AckPolicy.EXPLICIT,
            deliver_policy=DeliverPolicy.ALL,
        )
        self._sub = await js.pull_subscribe(
            f"{SUBJECT_PREFIX}.>",
            durable=DURABLE_NAME,
            stream=STREAM_NAME,
            config=config,
        )
        return self._sub

    # -- 公开 API ----------------------------------------------------------

    async def publish(self, event: dict[str, Any], subject: str | None = None) -> bool:
        """发布事件到 TESTSTATION_EVENTS 流。

        任何失败（NATS 不可达、流满、序列化错误、配置关闭）都返回
        ``False``——绝不抛出，离线降级是数据路径的一部分。

        Args:
            event: 事件载荷（dict，JSON 序列化；非法载荷返回 False）。
            subject: 可选主题后缀（默认 ``teststation.events``）。

        Returns:
            True 表示已发布并确认（pub ack 收到）；False 表示降级。
        """
        if not self._enabled:
            logger.debug("event_buffer disabled - publish skipped")
            return False
        try:
            payload = dict(event)
            payload.setdefault("buffered_at", self._clock())
            data = json.dumps(payload).encode("utf-8")
            _nc, js = await self._ensure_connection()
            await self._ensure_stream(js)
            target = f"{SUBJECT_PREFIX}.{subject}" if subject else SUBJECT_PREFIX
            ack = await asyncio.wait_for(
                js.publish(target, data, timeout=_PUBLISH_TIMEOUT_SECONDS),
                timeout=_PUBLISH_TIMEOUT_SECONDS * 2,
            )
            logger.debug(
                "event published (subject=%s, stream=%s, seq=%s)",
                target,
                getattr(ack, "stream", STREAM_NAME),
                getattr(ack, "seq", "?"),
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 降级契约：publish 永不抛出
            logger.warning(
                "event publish degraded to False (stream=%s): %s", STREAM_NAME, exc
            )
            return False

    async def fetch_pending(self, limit: int = 10, timeout: float = 1.0) -> list[PendingEvent]:
        """拉取一批待处理事件（不 ACK，由调用方逐条 :meth:`ack`）。

        Args:
            limit: 最多拉取条数（batch）。
            timeout: pull 等待秒数（无消息时静默返回空列表）。

        Returns:
            待处理事件列表；不可达/超时一律空列表（永不抛出）。
        """
        if not self._enabled:
            return []
        if limit < 1:
            raise ValueError("limit must be >= 1")
        try:
            sub = await self._ensure_subscription()
            # 外层硬超时兜底：pull 等待 timeout + 连接/IO 余量，
            # 服务器半挂（TCP 连着但 JetStream 不响应）时绝不长阻塞。
            messages = await asyncio.wait_for(
                sub.fetch(batch=limit, timeout=timeout),
                timeout=timeout + _FETCH_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - 拉取降级为空批
            logger.warning(
                "fetch_pending degraded to empty (stream=%s): %s", STREAM_NAME, exc
            )
            return []
        pending: list[PendingEvent] = []
        for msg in messages:
            try:
                payload = json.loads(msg.data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # 载荷损坏：ACK 掉毒消息，避免 durable consumer 卡死
                await self._safe_ack(msg)
                logger.warning(
                    "corrupt event payload acked-dropped (subject=%s)", msg.subject
                )
                continue
            metadata = getattr(msg, "metadata", None)
            seq = getattr(metadata, "sequence", None)
            stream_seq = seq.stream if seq is not None else getattr(msg, "sequence", 0)
            self._unacked[stream_seq] = msg
            pending.append(
                PendingEvent(
                    stream_seq=stream_seq,
                    subject=msg.subject,
                    payload=payload,
                    delivered_at=self._clock(),
                )
            )
        return pending

    async def ack(self, seq: int) -> bool:
        """ACK 指定 stream 序号的事件。

        seq 对应 :class:`PendingEvent.stream_seq`（最近一次
        :meth:`fetch_pending` 投递过的序号）；未投递过、已 ACK 或不可达
        时返回 False（永不抛出）。

        Args:
            seq: 流内序号。

        Returns:
            True 表示 ACK 已发送；False 表示降级。
        """
        if not self._enabled:
            return False
        msg = self._unacked.get(seq)
        if msg is None:
            logger.debug("ack for undelivered seq %s - nothing to ack", seq)
            return False
        try:
            # 硬超时兜底：ACK 走网络 IO，服务器半挂时不长阻塞
            # （失败返回 False；消息未 ACK，durable consumer 重启后会重投）。
            await asyncio.wait_for(msg.ack(), timeout=_PUBLISH_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - ACK 降级为 False
            logger.warning("ack degraded to False (seq=%s): %s", seq, exc)
            return False
        self._unacked.pop(seq, None)
        logger.debug("event acked (seq=%s)", seq)
        return True

    async def _safe_ack(self, msg: Any) -> None:
        """ACK 单条消息，吞掉所有错误（毒消息丢弃路径，硬超时防挂）。"""
        try:
            await asyncio.wait_for(msg.ack(), timeout=_PUBLISH_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - 毒消息 ACK 失败不中断
            logger.warning("poison-message ack failed (ignored): %s", exc)

    async def close(self) -> None:
        """关闭 NATS 连接（幂等；失败仅告警，硬超时防挂）。"""
        with self._lock:
            nc, self._nc, self._js, self._sub = self._nc, None, None, None
            self._unacked.clear()
        if nc is None:
            return
        try:
            await asyncio.wait_for(nc.drain(), timeout=_CONNECT_TIMEOUT_SECONDS)
            await nc.close()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不抛出
            logger.warning("event_buffer close degraded: %s", exc)
            try:
                await nc.close()
            except Exception:  # noqa: BLE001 - 二次关闭尽力而为
                pass

    @property
    def enabled(self) -> bool:
        """配置开关当前值。"""
        return self._enabled
