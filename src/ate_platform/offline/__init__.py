"""端侧离线自治层（设计文档 §10.5）。

缓存分层：序列/工装拓扑 SQLite WAL 缓存（T18）+ 脚本磁盘 SHA256 缓存（T19）。
上传队列与对账（T20/T21）、容量保护（T22）按计划另行落位。
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

__all__ = [
    "KIND_SEQUENCE",
    "KIND_TOPOLOGY",
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
    "VersionMismatchError",
    "sha256_checksum",
    "sha256_file",
    "sha256_text",
]
