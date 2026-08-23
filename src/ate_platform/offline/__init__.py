"""端侧离线自治层（设计文档 §10.5）。

当前仅含缓存分层第一层：序列/工装拓扑 SQLite WAL 缓存（T18）。
脚本磁盘缓存（T19）、上传队列与对账（T20/T21）、容量保护（T22）
按计划另行落位。
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

__all__ = [
    "KIND_SEQUENCE",
    "KIND_TOPOLOGY",
    "CacheMissError",
    "CorruptionError",
    "EntryStatus",
    "NotAckedError",
    "OfflineCacheError",
    "OfflineCacheStore",
    "VersionMismatchError",
    "sha256_checksum",
]
