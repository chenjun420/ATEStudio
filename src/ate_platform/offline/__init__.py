"""端侧离线自治层（设计文档 §10.5）。

缓存分层：序列/工装拓扑 SQLite WAL 缓存（T18）+ 脚本磁盘 SHA256 缓存（T19）
+ 待上传执行记录队列（T20）。对账（T21）、容量保护（T22）按计划另行落位。
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
    "DEFAULT_RETENTION_SECONDS",
    "KIND_SEQUENCE",
    "KIND_TOPOLOGY",
    "STATE_ACKED",
    "STATE_PENDING",
    "STATE_UPLOADED",
    "CacheMissError",
    "CorruptionError",
    "EntryStatus",
    "NotAckedError",
    "OfflineCacheError",
    "OfflineCacheStore",
    "OfflineScriptCache",
    "ScriptCacheError",
    "ScriptCorruptionError",
    "ScriptMissError",
    "ScriptStatus",
    "ScriptVersionMismatchError",
    "UploadQueue",
    "UploadQueueError",
    "UploadRecord",
    "AlreadyLockedError",
    "LockedVersionImmutableError",
    "OnlineLockRejectedError",
    "VersionLock",
    "VersionLockError",
    "VersionLockManager",
    "VersionMismatchError",
    "sha256_checksum",
    "sha256_file",
    "sha256_text",
]
