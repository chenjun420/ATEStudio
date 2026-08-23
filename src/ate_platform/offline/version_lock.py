"""T26 离线版本锁（设计文档 §10.5.4.2 版本一致性）。

语义（doc §10.5.4.2）：端侧执行时锁定序列版本与脚本版本快照，断网期间不隐式升级；
恢复联网后若云端版本已更新，按「新任务用新版本、进行中任务用锁定版本」处理。

规则：
- **联网禁止加锁**：锁只在离线自治窗口内产生（``must not lock when online``）；
- **进行中任务用锁定版本**：持有锁的条目一律按锁定版本读取，更新到达也不切换；
- **新版本只新增行**：离线到达的更新经 :meth:`VersionLockManager.store_update`
  写入缓存新 ``(id, version)`` 行——SQLite 主键 ``(id, version)`` 保证绝不覆写
  已锁定的旧行；对已锁定版本的任何写尝试直接拒绝；
- **未 ACK 版本离线不可用**：加锁前经 :meth:`OfflineCacheStore.get_usable`
  （默认 ``require_acked=True``）校验，:class:`NotAckedError` 等门控异常原样透传；
- **对账释放**：锁由对账流程（T21）显式释放（单条 / 按执行 / 全量），重复释放幂等安全。

线程安全：进程内 ``threading.RLock`` 串行化锁表操作；缓存自身有独立连接锁，
两层锁无交叉获取顺序，无死锁风险。锁表为内存态——进程重启即无运行中执行，
无需持久化。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ate_platform.offline.cache_store import (
    KIND_SEQUENCE,
    KIND_TOPOLOGY,
    OfflineCacheError,
    OfflineCacheStore,
)

__all__ = [
    "AlreadyLockedError",
    "LockedVersionImmutableError",
    "OnlineLockRejectedError",
    "VersionLock",
    "VersionLockError",
    "VersionLockManager",
]


class VersionLockError(OfflineCacheError):
    """版本锁层异常基类（归入离线层异常体系）。"""


class OnlineLockRejectedError(VersionLockError):
    """联网状态下禁止加锁——锁只在离线自治窗口内产生。"""


class AlreadyLockedError(VersionLockError):
    """条目已被其他版本/执行锁定——冲突加锁被拒绝。"""


class LockedVersionImmutableError(VersionLockError):
    """试图改写已锁定的版本——离线期间锁定快照不可变（§10.5.4.2）。"""


@dataclass(frozen=True)
class VersionLock:
    """一条版本锁记录（不可变快照）。"""

    kind: str
    entry_id: str
    version: str
    execution_id: str
    locked_at: float


#: kind → OfflineCacheStore 对应的写入方法名（仅消费其公开 API）
_STORE_METHODS = {
    KIND_SEQUENCE: "store_sequence",
    KIND_TOPOLOGY: "store_topology",
}


class VersionLockManager:
    """离线自治期间的版本锁管理器。

    Args:
        cache: ACK 门控缓存（T18 的 :class:`OfflineCacheStore`）。
        clock: 可注入时钟（测试用）；生产省略即取 ``time.time``。
    """

    def __init__(
        self, cache: OfflineCacheStore, *, clock: Callable[[], float] | None = None
    ) -> None:
        self._cache = cache
        self._clock: Callable[[], float] = time.time if clock is None else clock
        self._online = True  # 安全默认：联网态，禁止加锁
        self._lock = threading.RLock()
        self._locks: dict[tuple[str, str], VersionLock] = {}

    # ------------------------------------------------------------------
    # 在线/离线模式
    # ------------------------------------------------------------------
    @property
    def is_online(self) -> bool:
        """当前是否处于联网模式（联网态禁止加锁）。"""
        return self._online

    def set_online(self, online: bool) -> None:
        """切换联网模式（由断网感知层驱动；已有锁不受影响，仍由对账释放）。"""
        self._online = bool(online)

    # ------------------------------------------------------------------
    # 加锁 / 解析
    # ------------------------------------------------------------------
    def acquire(self, kind: str, entry_id: str, version: str, execution_id: str) -> VersionLock:
        """为运行中执行锁定 ``(kind, entry_id)`` 的指定版本。

        - 联网态拒绝（:class:`OnlineLockRejectedError`）；
        - 已有锁先判重入/冲突（锁定条目不可被重新绑定，与请求版本是否存在无关）；
        - 新锁落定前过缓存 ACK 门控：未 ACK / 缺失 / 损坏一律异常透传且不留锁；
        - 同 ``(execution_id, version)`` 重入幂等返回既有锁；
        - 版本或执行者不同则视为冲突（:class:`AlreadyLockedError`）。
        """
        if self._online:
            raise OnlineLockRejectedError(
                f"{kind}/{entry_id}: refusing to lock while online "
                "(version locks only apply during offline autonomy, doc §10.5.4.2)"
            )
        key = (kind, entry_id)
        with self._lock:
            existing = self._locks.get(key)
            if existing is not None:
                if existing.version == version and existing.execution_id == execution_id:
                    return existing  # 重入：幂等
                raise AlreadyLockedError(
                    f"{kind}/{entry_id}: locked to {existing.version} by "
                    f"{existing.execution_id}; cannot lock {version}/{execution_id}"
                )
            # 无既有锁：加锁前强制过 ACK 门控——未 ACK 的版本离线不可用，
            # 更不可作为执行快照（异常透传且不留半把锁）
            self._cache.get_usable(kind, entry_id, version)
            lock = VersionLock(kind, entry_id, version, execution_id, self._clock())
            self._locks[key] = lock
            return lock

    def resolve_pinned(self, kind: str, entry_id: str, execution_id: str) -> tuple[str, str]:
        """解析该执行的可用载荷，返回 ``(version, payload)``。

        - 已有锁：严格按锁定版本读取（进行中任务不隐式升级）；
        - 无锁 + 离线：取最新「已 ACK」版本读取并落锁——把下载到的版本
          快照钉死给本次执行（新任务从此刻起成为进行中任务）；
        - 无锁 + 联网：只读不加锁（``must not lock when online``）。

        Raises:
            NotAckedError: 最新版本未获云端 ACK（离线白名单之外）。
            CacheMissError: 条目完全未知。
            CorruptionError: 校验和不匹配。
        """
        with self._lock:
            existing = self._locks.get((kind, entry_id))
            if existing is not None:
                payload = self._cache.get_usable(kind, entry_id, existing.version)
                return existing.version, payload
            version = self._latest_acked_version(kind, entry_id)
            payload = (
                self._cache.get_usable(kind, entry_id)
                if version is None
                # 无已 ACK 版本：交还缓存门控抛精确异常（NotAcked/CacheMiss）
                else self._cache.get_usable(kind, entry_id, version)
            )
            if not self._online:
                self._locks[(kind, entry_id)] = VersionLock(
                    kind, entry_id, version or "", execution_id, self._clock()
                )
            return version or "", payload

    def _latest_acked_version(self, kind: str, entry_id: str) -> str | None:
        """经公开视图取该条目最新的已 ACK 版本；没有则 ``None``。

        ``list_cached`` 与 ``get_usable(version=None)`` 同序
        （``created_at DESC, rowid DESC``），故首个命中即最新。
        """
        for status in self._cache.list_cached(kind):
            if status.id == entry_id:
                if status.state == "acked":
                    return status.version
                return None  # 最新版本未 ACK：白名单外，交由门控拒绝
        return None

    # ------------------------------------------------------------------
    # 离线更新落缓存（新任务用新版本）
    # ------------------------------------------------------------------
    def store_update(
        self, kind: str, entry_id: str, version: str, payload: str, checksum: str | None = None
    ) -> None:
        """把云端更新写入缓存新版本行（离线到达的更新照收）。

        对已锁定 ``(entry_id, version)`` 的写尝试一律拒绝——离线期间锁定
        快照不可变；其余情况委托缓存存储：主键 ``(id, version)`` 决定
        新版本只会新增行，已锁定的旧行绝不被覆写。
        """
        method_name = _STORE_METHODS.get(kind)
        if method_name is None:
            raise ValueError(f"unknown cache kind: {kind!r} (expected {sorted(_STORE_METHODS)})")
        with self._lock:
            existing = self._locks.get((kind, entry_id))
            if existing is not None and existing.version == version:
                raise LockedVersionImmutableError(
                    f"{kind}/{entry_id}@{version}: locked snapshot is immutable while "
                    f"{existing.execution_id} is running (doc §10.5.4.2)"
                )
        getattr(self._cache, method_name)(entry_id, version, payload, checksum)

    # ------------------------------------------------------------------
    # 对账释放（T21）
    # ------------------------------------------------------------------
    def release(self, kind: str, entry_id: str) -> bool:
        """释放单条锁（对账完成时调用）。返回是否确有锁被释放（幂等友好）。"""
        with self._lock:
            return self._locks.pop((kind, entry_id), None) is not None

    def release_for_execution(self, execution_id: str) -> int:
        """释放某次执行持有的全部锁（跨 kind），返回释放数量。"""
        with self._lock:
            doomed = [k for k, lk in self._locks.items() if lk.execution_id == execution_id]
            for key in doomed:
                del self._locks[key]
        return len(doomed)

    def release_all(self) -> int:
        """释放全部锁（全量对账），返回释放数量。"""
        with self._lock:
            count = len(self._locks)
            self._locks.clear()
        return count

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------
    def get_lock(self, kind: str, entry_id: str) -> VersionLock | None:
        """查询条目当前锁；无锁返回 ``None``。"""
        with self._lock:
            return self._locks.get((kind, entry_id))

    def list_locks(self) -> list[VersionLock]:
        """列出全部锁（按 kind/id/version 排序，保证确定性）。"""
        with self._lock:
            return sorted(self._locks.values(), key=lambda lk: (lk.kind, lk.entry_id, lk.version))
