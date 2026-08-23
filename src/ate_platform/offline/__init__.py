"""端侧离线自治层（设计文档 §10.5）。

缓存分层：序列/工装拓扑 SQLite WAL 缓存（T18）+ 脚本磁盘 SHA256 缓存（T19）
+ 待上传执行记录队列（T20）+ 容量保护（T22）。重连对账（T21）：
Reconciler 消费上述模块公开 API + 传输缝完成断网恢复后的状态收敛。
心跳断连检测（T23）：既有 worker 心跳通道超时 + 迟滞 → offline/online 翻转。
离线版本锁（T26）：执行期版本快照，进行中任务用锁定版本、新任务用新版本。
"""

from ate_platform.offline.cache_store import (
    KIND_SEQUENCE,
    KIND_TOPOLOGY,
    CacheMissError,
    CorruptionError,
    EntryStatus,
    NotAckedError,
    OfflineCacheError,
    OfflineCacheStore,
    VersionMismatchError,
    sha256_checksum,
)
from ate_platform.offline.capacity_guard import (
    DEFAULT_SOFT_AGE_SECONDS,
    DEFAULT_SOFT_SIZE_BYTES,
    CapacityAlert,
    CapacityGuard,
    CapacityStatus,
)
from ate_platform.offline.heartbeat import (
    DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    DEFAULT_REQUIRED_MISSES,
    STATE_OFFLINE,
    STATE_ONLINE,
    HeartbeatError,
    HeartbeatMonitor,
    HeartbeatStatus,
)
from ate_platform.offline.reconciliation import (
    RESOLVE_CONFIRMED,
    RESOLVE_CONFLICT,
    QuarantinedItem,
    Reconciler,
    ReconcileReport,
    ReconcileUploader,
    VersionResolution,
)
from ate_platform.offline.script_cache import (
    OfflineScriptCache,
    ScriptCacheError,
    ScriptCorruptionError,
    ScriptMissError,
    ScriptStatus,
    ScriptVersionMismatchError,
    sha256_file,
    sha256_text,
)
from ate_platform.offline.upload_queue import (
    DEFAULT_RETENTION_SECONDS,
    STATE_ACKED,
    STATE_PENDING,
    STATE_UPLOADED,
    UploadQueue,
    UploadQueueError,
    UploadRecord,
)
from ate_platform.offline.version_lock import (
    AlreadyLockedError,
    LockedVersionImmutableError,
    OnlineLockRejectedError,
    VersionLock,
    VersionLockError,
    VersionLockManager,
)

__all__ = [
    "DEFAULT_HEARTBEAT_TIMEOUT_SECONDS",
    "DEFAULT_REQUIRED_MISSES",
    "DEFAULT_RETENTION_SECONDS",
    "DEFAULT_SOFT_AGE_SECONDS",
    "DEFAULT_SOFT_SIZE_BYTES",
    "KIND_SEQUENCE",
    "KIND_TOPOLOGY",
    "RESOLVE_CONFLICT",
    "RESOLVE_CONFIRMED",
    "STATE_ACKED",
    "STATE_OFFLINE",
    "STATE_ONLINE",
    "STATE_PENDING",
    "STATE_UPLOADED",
    "AlreadyLockedError",
    "CapacityAlert",
    "CapacityGuard",
    "CapacityStatus",
    "CacheMissError",
    "CorruptionError",
    "EntryStatus",
    "HeartbeatError",
    "HeartbeatMonitor",
    "HeartbeatStatus",
    "LockedVersionImmutableError",
    "NotAckedError",
    "OfflineCacheError",
    "OfflineCacheStore",
    "OfflineScriptCache",
    "OnlineLockRejectedError",
    "QuarantinedItem",
    "ReconcileReport",
    "ReconcileUploader",
    "Reconciler",
    "ScriptCacheError",
    "ScriptCorruptionError",
    "ScriptMissError",
    "ScriptStatus",
    "ScriptVersionMismatchError",
    "UploadQueue",
    "UploadQueueError",
    "UploadRecord",
    "VersionLock",
    "VersionLockError",
    "VersionLockManager",
    "VersionMismatchError",
    "VersionResolution",
    "sha256_checksum",
    "sha256_file",
    "sha256_text",
]
