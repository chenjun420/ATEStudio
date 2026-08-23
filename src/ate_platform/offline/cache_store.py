"""离线序列/工装拓扑缓存存储（设计文档 §10.5 / §9.4.2）。

端侧缓存分层第一层（§10.5.2）：已下发序列 YAML 与工装拓扑 JSON 落入本地
SQLite（WAL 模式），含版本号与 SHA256 校验和。核心契约：

- **下发即缓存**（§10.5.4.1）：云→端 ``update_plan`` / ``update_topology``
  在端侧先落库并回执 ACK；**未完成 ACK 的版本不作为可离线使用版本**
  （§10.5.4.3 离线白名单），:meth:`OfflineCacheStore.get_usable` 默认强制
  该门控；
- **校验和不匹配拒绝执行**（§10.5.2）：每次读取重算 SHA256 并比对，
  不匹配抛 :class:`CorruptionError`，绝不静默返回损坏载荷；
- **端侧 SQLite 生产配置**（§9.4.2）：``journal_mode=WAL`` /
  ``synchronous=NORMAL`` / ``busy_timeout=30000`` / ``cache_size=-20000``；
- **Schema 版本化**：``PRAGMA user_version`` 记录 schema 版本，为后续
  迁移预留（发现更新版本数据库时拒绝打开，避免旧代码写坏新数据）。

本模块只做纯本地缓存层：上传队列/对账/容量保护/心跳属 T20-T23，不在此实现。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

#: 缓存条目种类（对应 sequences / topologies 两张表）
KIND_SEQUENCE = "sequence"
KIND_TOPOLOGY = "topology"

_STATE_CACHED = "cached"
_STATE_ACKED = "acked"

_SCHEMA_VERSION = 1

# §9.4.2 端侧 SQLite 生产配置（journal_mode=WAL 持久化在库文件中，
# synchronous/busy_timeout/cache_size 为每连接生效，故每次打开都应用）
_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA busy_timeout = 30000",
    "PRAGMA cache_size = -20000",
)

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    id         TEXT NOT NULL,
    version    TEXT NOT NULL,
    payload    TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'cached',
    created_at REAL NOT NULL,
    acked_at   REAL,
    PRIMARY KEY (id, version)
)
"""

_TABLES: dict[str, str] = {KIND_SEQUENCE: "sequences", KIND_TOPOLOGY: "topologies"}


class OfflineCacheError(Exception):
    """离线缓存层异常基类。"""


class CacheMissError(OfflineCacheError):
    """缓存中不存在该条目（id 完全未知）。"""


class VersionMismatchError(OfflineCacheError):
    """条目存在但请求的版本从未缓存过（§10.5.4.2 版本一致性）。"""


class NotAckedError(OfflineCacheError):
    """版本已缓存但云端尚未 ACK —— 离线模式下不可使用（§10.5.4.1/3）。"""


class CorruptionError(OfflineCacheError):
    """载荷/校验和不匹配 —— 拒绝服务，绝不静默返回损坏内容。"""


def sha256_checksum(payload: str) -> str:
    """计算载荷的 SHA256 十六进制摘要（UTF-8 编码）。"""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EntryStatus:
    """缓存条目状态视图（不含载荷，供列表/UI 与 T21 对账使用）。"""

    kind: str
    id: str
    version: str
    state: str  # 'cached' | 'acked'
    checksum: str
    created_at: float
    acked_at: float | None


def _resolve_table(kind: str) -> str:
    try:
        return _TABLES[kind]
    except KeyError:
        raise ValueError(f"unknown cache kind: {kind!r} (expected {sorted(_TABLES)})") from None


class OfflineCacheStore:
    """ACK 门控的离线序列/拓扑 SQLite 缓存。

    线程安全：单连接（``check_same_thread=False``）+ :class:`threading.RLock`
    串行化全部操作——端侧写入频率低（仅下发/ACK 时），锁开销可忽略。
    """

    #: 当前 schema 版本（PRAGMA user_version）
    SCHEMA_VERSION = _SCHEMA_VERSION

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        with self._lock:
            for pragma in _PRAGMAS:
                self._conn.execute(pragma)
            for table in _TABLES.values():
                self._conn.execute(_TABLE_DDL.format(table=table))
            self._init_schema_version()
            self._conn.commit()
        logger.info("offline_cache_opened", path=str(self._path), schema_version=self.SCHEMA_VERSION)

    # ------------------------------------------------------------------
    # Schema 版本管理
    # ------------------------------------------------------------------
    def _init_schema_version(self) -> None:
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if current == 0:
            self._conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        elif current > self.SCHEMA_VERSION:
            # 未来升级路径：旧代码遇到新库必须拒绝打开，防止写坏新 schema
            raise OfflineCacheError(
                f"offline cache db at {self._path} has schema v{current}, "
                f"but this code only understands v{self.SCHEMA_VERSION}"
            )

    # ------------------------------------------------------------------
    # 写入（下发即缓存）
    # ------------------------------------------------------------------
    def store_sequence(
        self, seq_id: str, version: str, payload: str, checksum: str | None = None
    ) -> None:
        """缓存一条序列（YAML 或 JSON 文本）。"""
        self._store(KIND_SEQUENCE, seq_id, version, payload, checksum)

    def store_topology(
        self, topo_id: str, version: str, payload: str, checksum: str | None = None
    ) -> None:
        """缓存一条工装拓扑（JSON 文本）。"""
        self._store(KIND_TOPOLOGY, topo_id, version, payload, checksum)

    def _store(self, kind: str, entry_id: str, version: str, payload: str, checksum: str | None) -> None:
        if not isinstance(payload, str):
            raise TypeError("payload must be a str (YAML/JSON text)")
        computed = sha256_checksum(payload)
        if checksum is not None and checksum != computed:
            raise ValueError(
                f"checksum mismatch for {kind}/{entry_id}@{version}: "
                f"provided {checksum[:12]}… != computed {computed[:12]}…"
            )
        table = _resolve_table(kind)
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                f"SELECT payload, checksum FROM {table} WHERE id = ? AND version = ?",
                (entry_id, version),
            ).fetchone()
            if row is not None and row[0] == payload and row[1] == computed:
                # 幂等重发（同 id+version+内容）：保留现有 ACK 状态，
                # 云端重试不应把已确认可离线的版本打回 cached
                return
            # 新条目或内容变化：一律回到 cached，等待新一轮 ACK
            self._conn.execute(
                f"INSERT OR REPLACE INTO {table} "
                "(id, version, payload, checksum, state, created_at, acked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (entry_id, version, payload, computed, _STATE_CACHED, now),
            )
            self._conn.commit()
        logger.debug("offline_cache_stored", kind=kind, id=entry_id, version=version)

    # ------------------------------------------------------------------
    # ACK 门控
    # ------------------------------------------------------------------
    def mark_acked(self, kind: str, entry_id: str, version: str, acked_at: float | None = None) -> bool:
        """将 ``cached → acked``（云端确认下发成功后调用）。

        Returns:
            True 表示状态翻转成功；False 表示条目不存在（幂等友好，
            供 T21 对账安全重放）。
        """
        table = _resolve_table(kind)
        ts = time.time() if acked_at is None else acked_at
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE {table} SET state = ?, acked_at = ? WHERE id = ? AND version = ?",
                (_STATE_ACKED, ts, entry_id, version),
            )
            self._conn.commit()
        changed = cur.rowcount > 0
        logger.debug("offline_cache_acked", kind=kind, id=entry_id, version=version, changed=changed)
        return changed

    # ------------------------------------------------------------------
    # 读取（完整性 + ACK 双重门控）
    # ------------------------------------------------------------------
    def get_usable(
        self, kind: str, entry_id: str, version: str | None = None, require_acked: bool = True
    ) -> str:
        """取回可用的缓存载荷；不可用一律抛异常，绝不静默降级。

        Args:
            kind: :data:`KIND_SEQUENCE` 或 :data:`KIND_TOPOLOGY`。
            entry_id: 条目 id。
            version: 期望版本；``None`` 取最新缓存版本。
            require_acked: ACK 门控开关。离线执行路径必须保持默认 True；
                仅在线诊断允许显式关闭。

        Raises:
            CacheMissError: id 完全不存在。
            VersionMismatchError: 请求的版本从未缓存过。
            CorruptionError: 校验和不匹配（篡改/磁盘损坏）。
            NotAckedError: 版本未获云端 ACK（require_acked=True 时）。
        """
        table = _resolve_table(kind)
        with self._lock:
            if version is None:
                row = self._conn.execute(
                    f"SELECT payload, checksum, state, version FROM {table} "
                    "WHERE id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (entry_id,),
                ).fetchone()
                if row is None:
                    raise CacheMissError(f"{kind}/{entry_id}: no cached versions")
            else:
                row = self._conn.execute(
                    f"SELECT payload, checksum, state, version FROM {table} "
                    "WHERE id = ? AND version = ?",
                    (entry_id, version),
                ).fetchone()
                if row is None:
                    exists = self._conn.execute(
                        f"SELECT 1 FROM {table} WHERE id = ?", (entry_id,)
                    ).fetchone()
                    if exists:
                        raise VersionMismatchError(
                            f"{kind}/{entry_id}@{version}: version never cached"
                        )
                    raise CacheMissError(f"{kind}/{entry_id}: no cached versions")

            payload, checksum, state, stored_version = row
            # 完整性优先于授权：损坏行连状态信息都不值得信任
            if sha256_checksum(payload) != checksum:
                logger.error(
                    "offline_cache_corruption", kind=kind, id=entry_id, version=stored_version
                )
                raise CorruptionError(
                    f"{kind}/{entry_id}@{stored_version}: checksum mismatch — refusing to serve"
                )
            if require_acked and state != _STATE_ACKED:
                raise NotAckedError(
                    f"{kind}/{entry_id}@{stored_version}: cached but not cloud-ACKed; "
                    "not usable offline (doc §10.5.4.1)"
                )
            return payload

    # ------------------------------------------------------------------
    # 列表 / 删除 / 清理
    # ------------------------------------------------------------------
    def list_cached(self, kind: str | None = None) -> list[EntryStatus]:
        """列出缓存条目状态视图（不含载荷）。"""
        kinds = [kind] if kind is not None else list(_TABLES)
        entries: list[EntryStatus] = []
        with self._lock:
            for k in kinds:
                table = _resolve_table(k)
                rows = self._conn.execute(
                    f"SELECT id, version, state, checksum, created_at, acked_at FROM {table} "
                    "ORDER BY id, created_at DESC, rowid DESC"
                ).fetchall()
                entries.extend(
                    EntryStatus(
                        kind=k,
                        id=r[0],
                        version=r[1],
                        state=r[2],
                        checksum=r[3],
                        created_at=r[4],
                        acked_at=r[5],
                    )
                    for r in rows
                )
        return entries

    def delete(self, kind: str, entry_id: str, version: str | None = None) -> int:
        """删除指定版本（version 给定）或该条目全部版本。返回删除行数。"""
        table = _resolve_table(kind)
        with self._lock:
            if version is None:
                cur = self._conn.execute(f"DELETE FROM {table} WHERE id = ?", (entry_id,))
            else:
                cur = self._conn.execute(
                    f"DELETE FROM {table} WHERE id = ? AND version = ?", (entry_id, version)
                )
            self._conn.commit()
            return cur.rowcount

    def prune(self, kind: str, keep_last_n: int = 2) -> int:
        """按条目保留最近 N 个版本（§10.5.2「保留最近 N 个版本」）。

        同一 ``created_at``（Windows 时钟粒度粗）以插入序 rowid 决胜，
        保证最新写入的版本存活。
        """
        if keep_last_n < 1:
            raise ValueError("keep_last_n must be >= 1")
        table = _resolve_table(kind)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, version FROM {table} ORDER BY id, created_at DESC, rowid DESC"
            ).fetchall()
            per_id: dict[str, list[str]] = {}
            for entry_id, ver in rows:
                per_id.setdefault(entry_id, []).append(ver)
            doomed = [(eid, ver) for eid, vers in per_id.items() for ver in vers[keep_last_n:]]
            if doomed:
                self._conn.executemany(
                    f"DELETE FROM {table} WHERE id = ? AND version = ?", doomed
                )
                self._conn.commit()
            return len(doomed)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭连接（WAL 文件在正常关闭时自动 checkpoint）。"""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> OfflineCacheStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
