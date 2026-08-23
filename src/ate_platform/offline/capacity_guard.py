"""离线缓存容量保护（设计文档 §10.5.4.5「缓存容量保护」）。

端侧离线自治层的容量守卫（T22）：跟踪缓存目录体积与最旧记录年龄，
越过阈值时**告警并暂停接收新序列下发**，防止磁盘写满；测量数据不丢弃。

核心契约（§10.5.4.5 + 计划 #22）：

- **纯咨询式（advisory）**：守卫只观察、只报告——:meth:`CapacityGuard.check`
  返回 :class:`CapacityStatus`，绝不删除用户数据、绝不抛容量异常、绝不拦截
  已缓存内容的读取/执行路径（暂停的只是*新*下载，见
  :attr:`CapacityStatus.downloads_paused` / :meth:`CapacityGuard.can_download`，
  由 T24 下发路径消费）；
- **软/硬双阈值**：软阈值（默认 §10.5.4.5 的 500MB / 72h）越线 → 告警事件；
  硬阈值越线 → 告警 + 暂停新下载。硬阈值缺省等于软阈值（文档单组数字语义：
  超过阈值即"告警并暂停"），可独立调高实现"先预警后暂停"梯度；
- **边沿触发**：每条 (kind, level) 越线只发一次事件，回落到阈值之下重新武装，
  避免持续超限时事件风暴；
- **瞬态临时文件不计入**：``*.tmp`` / ``*.temp`` / ``*.part``（可配置）是
  T19 原子写入的在途文件，不代表持久占用；SQLite ``-wal``/``-shm`` 是真实
  磁盘占用，计入体积；
- **purge 后自动恢复**：暂停状态是当前体积的纯函数，清理使体积回到硬阈值
  之下即自动恢复接收（恢复转换记日志/事件），无需人工复位。

年龄信号默认取目录内最旧非临时文件的 mtime；可通过 ``age_source`` 注入
（例如接 :class:`~ate_platform.offline.cache_store.OfflineCacheStore` 的
``min(created_at)``）。时钟可注入以便确定性测试。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

#: §10.5.4.5 默认软阈值：500MB（体积）
DEFAULT_SOFT_SIZE_BYTES = 500 * 1024 * 1024
#: §10.5.4.5 默认软阈值：72 小时（最旧记录年龄）
DEFAULT_SOFT_AGE_SECONDS = 72 * 3600

#: 瞬态临时文件后缀（T19 原子写入在途文件；不计入体积/年龄统计）
DEFAULT_TEMP_SUFFIXES = frozenset({".tmp", ".temp", ".part"})


@dataclass(frozen=True)
class CapacityAlert:
    """一次越线告警事件（边沿触发，供监听器/T24 事件总线消费）。"""

    kind: str  # 'size' | 'age'
    level: str  # 'soft' | 'hard'
    value: float  # 当前观测值（字节或秒）
    threshold: float  # 对应配置阈值
    message: str


@dataclass(frozen=True)
class CapacityStatus:
    """单次检查的容量快照。

    ``alerts`` 只含**本次检查新触发**的事件（边沿触发语义）；
    持续越限时不重复出现。
    """

    size_bytes: int
    file_count: int
    oldest_age_seconds: float | None  # 目录无有效记录时为 None
    soft_exceeded: bool  # 体积或年龄越过软阈值
    hard_exceeded: bool  # 体积或年龄越过硬阈值
    downloads_paused: bool  # == hard_exceeded；新下载是否应被暂停
    alerts: tuple[CapacityAlert, ...]

    @property
    def can_download(self) -> bool:
        """下载路径消费点：False 表示应拒绝新的序列/脚本下发。"""
        return not self.downloads_paused


def _default_age_source(cache_dir: Path, temp_suffixes: frozenset[str]) -> Callable[[], float | None]:
    """默认年龄源：目录内最旧非临时文件的 mtime（无有效文件 → None）。"""

    def _oldest_mtime() -> float | None:
        if not cache_dir.is_dir():
            return None
        oldest: float | None = None
        for path in cache_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() in temp_suffixes:
                continue
            mtime = path.stat().st_mtime
            if oldest is None or mtime < oldest:
                oldest = mtime
        return oldest

    return _oldest_mtime


class CapacityGuard:
    """离线缓存目录的容量守卫（观察者 + 阈值评估器，线程安全）。

    Args:
        cache_dir: 被守护的缓存目录（不必已存在；缺失视为空且不创建）。
        soft_size_bytes: 体积软阈值（告警），默认 §10.5.4.5 的 500MB。
        soft_age_seconds: 年龄软阈值（告警），默认 §10.5.4.5 的 72h。
        hard_size_bytes: 体积硬阈值（暂停）；``None`` = 与软阈值相同。
        hard_age_seconds: 年龄硬阈值（暂停）；``None`` = 与软阈值相同。
        clock: 可注入时钟（返回 unix 秒），测试确定性用。
        temp_suffixes: 视为瞬态临时文件的扩展名集合。
        age_source: 自定义最旧记录时间戳提供者（unix 秒或 None）；
            缺省用目录内最旧非临时文件 mtime。

    Raises:
        ValueError: 阈值为负，或硬阈值小于软阈值（配置矛盾）。
    """

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        soft_size_bytes: int = DEFAULT_SOFT_SIZE_BYTES,
        soft_age_seconds: float = DEFAULT_SOFT_AGE_SECONDS,
        hard_size_bytes: int | None = None,
        hard_age_seconds: float | None = None,
        clock: Callable[[], float] = time.time,
        temp_suffixes: Iterable[str] = DEFAULT_TEMP_SUFFIXES,
        age_source: Callable[[], float | None] | None = None,
    ) -> None:
        if soft_size_bytes < 0:
            raise ValueError("soft_size_bytes must be >= 0")
        if soft_age_seconds < 0:
            raise ValueError("soft_age_seconds must be >= 0")
        resolved_hard_size = soft_size_bytes if hard_size_bytes is None else hard_size_bytes
        resolved_hard_age = soft_age_seconds if hard_age_seconds is None else hard_age_seconds
        if resolved_hard_size < soft_size_bytes:
            raise ValueError(
                f"hard_size_bytes ({resolved_hard_size}) must be >= soft_size_bytes "
                f"({soft_size_bytes}): pause must never precede its own alert"
            )
        if resolved_hard_age < soft_age_seconds:
            raise ValueError(
                f"hard_age_seconds ({resolved_hard_age}) must be >= soft_age_seconds "
                f"({soft_age_seconds}): pause must never precede its own alert"
            )

        self._cache_dir = Path(cache_dir)
        self._soft_size = soft_size_bytes
        self._soft_age = soft_age_seconds
        self._hard_size = resolved_hard_size
        self._hard_age = resolved_hard_age
        self._clock = clock
        self._temp_suffixes = frozenset(s.lower() for s in temp_suffixes)
        self._age_source = age_source or _default_age_source(self._cache_dir, self._temp_suffixes)
        self._lock = threading.RLock()
        #: 当前处于越线状态的 (kind, level) 集合 —— 边沿触发/再武装记账
        self._latched: set[tuple[str, str]] = set()
        self._listeners: list[Callable[[CapacityAlert], None]] = []
        self._prev_paused = False  # 上次 check 的暂停状态（用于恢复转换事件）

    # ------------------------------------------------------------------
    # 配置视图
    # ------------------------------------------------------------------
    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    @property
    def paused(self) -> bool:
        """最近一次 :meth:`check` 得出的暂停状态（首次检查前为 False）。"""
        with self._lock:
            return self._prev_paused

    # ------------------------------------------------------------------
    # 测量（纯观察，无副作用）
    # ------------------------------------------------------------------
    def measure(self) -> CapacityStatus:
        """扫描目录并评估阈值；不发事件、不改边沿状态。"""
        size = 0
        count = 0
        if self._cache_dir.is_dir():
            for path in self._cache_dir.rglob("*"):
                if not path.is_file() or path.suffix.lower() in self._temp_suffixes:
                    continue
                size += path.stat().st_size
                count += 1
        now = self._clock()
        oldest_ts = self._age_source()
        oldest_age = None if oldest_ts is None else max(0.0, now - oldest_ts)

        soft_exceeded = size > self._soft_size or (oldest_age is not None and oldest_age > self._soft_age)
        hard_exceeded = size > self._hard_size or (oldest_age is not None and oldest_age > self._hard_age)
        return CapacityStatus(
            size_bytes=size,
            file_count=count,
            oldest_age_seconds=oldest_age,
            soft_exceeded=soft_exceeded,
            hard_exceeded=hard_exceeded,
            downloads_paused=hard_exceeded,
            alerts=(),
        )

    # ------------------------------------------------------------------
    # 检查（测量 + 边沿触发事件 + 监听器通知）
    # ------------------------------------------------------------------
    def check(self) -> CapacityStatus:
        """完整检查：测量、触发新越线事件、通知监听器、记录暂停状态转换。"""
        status = self.measure()
        fired: list[CapacityAlert] = []
        with self._lock:
            checks: tuple[tuple[str, str, float | None, float], ...] = (
                ("size", "soft", float(status.size_bytes), float(self._soft_size)),
                ("size", "hard", float(status.size_bytes), float(self._hard_size)),
                ("age", "soft", status.oldest_age_seconds, float(self._soft_age)),
                ("age", "hard", status.oldest_age_seconds, float(self._hard_age)),
            )
            for kind, level, value, threshold in checks:
                key = (kind, level)
                breached = value is not None and value > threshold
                if breached and key not in self._latched:
                    self._latched.add(key)
                    unit = "bytes" if kind == "size" else "seconds"
                    fired.append(
                        CapacityAlert(
                            kind=kind,
                            level=level,
                            value=value,
                            threshold=threshold,
                            message=(
                                f"offline cache {kind} {value:.0f} {unit} exceeds "
                                f"{level} threshold {threshold:.0f} (doc §10.5.4.5)"
                            ),
                        )
                    )
                elif not breached and key in self._latched:
                    self._latched.discard(key)  # 回落 → 重新武装，下次越线再报

            for alert in fired:
                log = logger.warning if alert.level == "hard" else logger.info
                log(
                    "offline_cache_capacity_alert",
                    kind=alert.kind,
                    level=alert.level,
                    value=alert.value,
                    threshold=alert.threshold,
                    dir=str(self._cache_dir),
                )
                self._notify(alert)

            # 暂停/恢复状态转换事件（恢复即 §10.5 QA 场景「purge 后下载恢复」）
            if status.downloads_paused and not self._prev_paused:
                logger.warning(
                    "offline_cache_downloads_paused",
                    size_bytes=status.size_bytes,
                    oldest_age_seconds=status.oldest_age_seconds,
                    dir=str(self._cache_dir),
                )
            elif not status.downloads_paused and self._prev_paused:
                logger.info(
                    "offline_cache_downloads_resumed",
                    size_bytes=status.size_bytes,
                    dir=str(self._cache_dir),
                )
            self._prev_paused = status.downloads_paused

        return CapacityStatus(
            size_bytes=status.size_bytes,
            file_count=status.file_count,
            oldest_age_seconds=status.oldest_age_seconds,
            soft_exceeded=status.soft_exceeded,
            hard_exceeded=status.hard_exceeded,
            downloads_paused=status.downloads_paused,
            alerts=tuple(fired),
        )

    def _notify(self, alert: CapacityAlert) -> None:
        for listener in self._listeners:
            try:
                listener(alert)
            except Exception:  # 守卫绝不能被消费者拖垮（advisory 契约）
                logger.exception("offline_cache_capacity_listener_error")

    # ------------------------------------------------------------------
    # 事件订阅
    # ------------------------------------------------------------------
    def add_listener(self, listener: Callable[[CapacityAlert], None]) -> None:
        """注册告警监听器（同步回调；回调异常被吞掉并记日志）。"""
        with self._lock:
            self._listeners.append(listener)

    def can_download(self) -> bool:
        """下载路径消费点：执行一次完整检查并回答是否允许新下发。"""
        return self.check().can_download
