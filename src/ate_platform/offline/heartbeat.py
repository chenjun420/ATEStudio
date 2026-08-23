"""端侧心跳断连检测（设计文档 §10.5 心跳超时 10s）。

离线自治分层第四块（§10.5）：站点订阅**既有 worker 心跳通道**（云端
dashboard 在线/离线判定的同一信源），心跳不可见超过 10s 即判定断连，
翻转 offline_mode——冻结版本锁、读取切换到本地缓存、执行记录进入待上传
队列（T24 消费本模块的状态翻转事件落地这三件事）。核心契约：

- **不发明新心跳机制**：本模块是纯本地状态机，只消费调用方从既有通道
  收到的心跳（:meth:`HeartbeatMonitor.record_beat`），不含任何 NATS/
  HTTP 传输与后台线程；
- **迟滞防抖**：单次超时 miss 不翻转，需 **连续 2 次** miss（默认，
  构造器可配）才进入 offline；任一心跳到达立即恢复 online 并清零计数
  ——抖动链路（漏一拍就来拍）永远到不了连续 miss；
- **进程本地暂停不进入离线**（§10.5 约束）：:meth:`pause_local` 挂起
  判定（本地暂停不是断连证据），:meth:`resume_local` 以恢复时刻为新
  基线重新计时；
- **可注入时钟**：构造器注入 ``clock`` 可调用对象（默认
  :func:`time.monotonic`），判定全程确定性、可测试；
- **状态翻转通知**：:meth:`add_listener` 注册回调 ``(old_state,
  new_state)``，T24 据此驱动 offline_mode 三件事。

超时语义：``now - 基线 > timeout`` 记一次 miss（严格大于，恰好整点
不算）；基线 = 最近一次心跳时刻（从未收到过心跳则以构造/恢复时刻为
基线——启动即失联同样可检出）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)

#: 站点连接状态（§10.5 心跳在线/离线）
STATE_ONLINE = "online"
STATE_OFFLINE = "offline"

#: 心跳超时默认值：10s（§10.5「心跳 10s 不可见即离线」）
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 10.0

#: 迟滞默认值：连续 2 次 miss 才翻转（§10.5「require 2 consecutive misses」）
DEFAULT_REQUIRED_MISSES = 2


class HeartbeatError(Exception):
    """心跳断连检测层异常基类。"""


@dataclass(frozen=True)
class HeartbeatStatus:
    """心跳监视器状态视图（供 UI 与 T24 offline_mode 消费）。"""

    state: str  # 'online' | 'offline'
    paused: bool  # 进程本地暂停中（判定挂起）
    consecutive_misses: int
    last_beat_at: float | None
    seconds_since_last_beat: float | None
    entered_offline_at: float | None


class HeartbeatMonitor:
    """既有 worker 心跳通道的断连检测状态机（超时 + 迟滞）。

    线程安全：内部 :class:`threading.RLock` 串行化全部操作——端侧心跳
    频率低（秒级一拍），锁开销可忽略。
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        required_misses: int = DEFAULT_REQUIRED_MISSES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if required_misses < 1:
            raise ValueError("required_misses must be >= 1")
        self._timeout = float(timeout_seconds)
        self._required_misses = int(required_misses)
        self._clock = clock
        self._lock = threading.RLock()
        self._state = STATE_ONLINE
        self._paused = False
        self._misses = 0
        self._last_beat_at: float | None = None
        self._entered_offline_at: float | None = None
        # 超时基线：最近一次心跳；从未收到过心跳时以构造时刻为基线
        self._baseline = self._clock()
        self._listeners: list[Callable[[str, str], None]] = []
        logger.info(
            "heartbeat_monitor_opened",
            timeout_seconds=self._timeout,
            required_misses=self._required_misses,
        )

    # ------------------------------------------------------------------
    # 心跳摄入（既有 worker 心跳通道的唯一入口）
    # ------------------------------------------------------------------
    def record_beat(self) -> HeartbeatStatus:
        """登记一拍来自既有通道的心跳；offline 时立即恢复 online。"""
        now = self._clock()
        with self._lock:
            self._last_beat_at = now
            self._baseline = now
            self._misses = 0
            if self._state == STATE_OFFLINE:
                self._transition_locked(STATE_OFFLINE, STATE_ONLINE, now)
            else:
                logger.debug("heartbeat_beat_recorded", at=now)
            return self._status_locked(now)

    # ------------------------------------------------------------------
    # 超时评估（调用方轮询驱动；纯状态机，无内置线程）
    # ------------------------------------------------------------------
    def check(self) -> HeartbeatStatus:
        """评估超时 + 迟滞；按需翻转 online/offline。

        - 本地暂停中：不计 miss、不翻转（§10.5：本地暂停 ≠ 断连）；
        - ``now - baseline > timeout``：miss +1（严格大于）；否则清零；
        - 连续 miss 达到 ``required_misses`` 且当前 online → 翻转 offline。
        """
        now = self._clock()
        with self._lock:
            if not self._paused:
                gap = now - self._baseline
                if gap > self._timeout:
                    self._misses += 1
                    logger.debug(
                        "heartbeat_miss_counted",
                        consecutive_misses=self._misses,
                        seconds_since_last_beat=gap,
                    )
                else:
                    self._misses = 0
                if (
                    self._state == STATE_ONLINE
                    and self._misses >= self._required_misses
                ):
                    self._transition_locked(STATE_ONLINE, STATE_OFFLINE, now)
            return self._status_locked(now)

    # ------------------------------------------------------------------
    # 进程本地暂停（§10.5：不得因本地暂停进入离线）
    # ------------------------------------------------------------------
    def pause_local(self) -> HeartbeatStatus:
        """挂起断连判定（本地暂停期间 check 不计 miss）。"""
        with self._lock:
            self._paused = True
            logger.info("heartbeat_paused")
            return self._status_locked(self._clock())

    def resume_local(self) -> HeartbeatStatus:
        """恢复判定，并以恢复时刻为新基线重新计时（暂停期不作断连证据）。"""
        now = self._clock()
        with self._lock:
            self._paused = False
            self._baseline = now
            self._misses = 0
            logger.info("heartbeat_resumed", at=now)
            return self._status_locked(now)

    # ------------------------------------------------------------------
    # 状态翻转通知（T24 offline_mode 消费入口）
    # ------------------------------------------------------------------
    def add_listener(self, callback: Callable[[str, str], None]) -> None:
        """注册 ``(old_state, new_state)`` 回调，仅在状态翻转时调用。"""
        with self._lock:
            self._listeners.append(callback)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _transition_locked(self, old: str, new: str, at: float) -> None:
        self._state = new
        if new == STATE_OFFLINE:
            self._entered_offline_at = at
            logger.info(
                "heartbeat_offline_entered",
                seconds_since_last_beat=at - (self._last_beat_at or self._baseline),
                consecutive_misses=self._misses,
                timeout_seconds=self._timeout,
            )
        else:
            self._entered_offline_at = None
            logger.info("heartbeat_online_recovered", at=at)
        listeners = list(self._listeners)
        for cb in listeners:
            cb(old, new)

    def _status_locked(self, now: float) -> HeartbeatStatus:
        gap = None if self._last_beat_at is None else now - self._last_beat_at
        return HeartbeatStatus(
            state=self._state,
            paused=self._paused,
            consecutive_misses=self._misses,
            last_beat_at=self._last_beat_at,
            seconds_since_last_beat=gap,
            entered_offline_at=self._entered_offline_at,
        )

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        """当前连接状态（'online' | 'offline'）。"""
        with self._lock:
            return self._state

    @property
    def status(self) -> HeartbeatStatus:
        """完整状态快照（冻结 dataclass）。"""
        with self._lock:
            return self._status_locked(self._clock())
