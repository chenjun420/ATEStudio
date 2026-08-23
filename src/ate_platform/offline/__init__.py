"""端侧离线自治层（设计文档 §10.5）。

缓存分层：序列/工装拓扑 SQLite WAL 缓存（T18）+ 脚本磁盘 SHA256 缓存（T19）
+ 待上传执行记录队列（T20）+ 容量保护（T22）。对账（T21）按计划另行落位。
心跳断连检测（T23）：既有 worker 心跳通道超时 + 迟滞 → offline/online 翻转。
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
from ate_platform.offline.heartbeat import (
    DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    DEFAULT_REQUIRED_MISSES,
    STATE_OFFLINE,
    STATE_ONLINE,
    HeartbeatError,
    HeartbeatMonitor,
    HeartbeatStatus,
)
from ate_platform.offline.capacity_guard import (
    DEFAULT_SOFT_AGE_SECONDS,
    DEFAULT_SOFT_SIZE_BYTES,
    CapacityAlert,
    CapacityGuard,
    CapacityStatus,
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

__all__ = [
    "DEFAULT_HEARTBEAT_TIMEOUT_SECONDS",
    "DEFAULT_REQUIRED_MISSES",
    "DEFAULT_RETENTION_SECONDS",
    "KIND_SEQUENCE",
    "KIND_TOPOLOGY",
    "STATE_ACKED",
    "STATE_OFFLINE",
    "STATE_ONLINE",
    "STATE_PENDING",
    "STATE_UPLOADED",
    "CacheMissError",
    "CorruptionError",
    "EntryStatus",
    "HeartbeatError",
    "HeartbeatMonitor",
    "HeartbeatStatus",
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
    "VersionMismatchError",
    "sha256_checksum",
    "sha256_file",
    "sha256_text",
]
