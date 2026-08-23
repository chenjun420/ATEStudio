"""端侧待上传执行记录队列（设计文档 §10.5 执行记录上传队列）。

离线自治分层第三块（§10.5）：端侧执行产生的记录（JSONL 归档）先落本地
SQLite（WAL 模式）队列，重连后由上传器批量送云端，服务器 ACK 后进入
保留期（默认 7 天）再清理。核心契约：

- **状态机**：``pending → uploaded → acked``，每次翻转即时 ``commit``
  （写前日志语义）——进程崩溃/断电后重开连接，已推进的状态绝不回退；
- **幂等键** ``(station_id, execution_id, seq_no)``：重复入队返回既有行，
  且不把 uploaded/acked 打回 pending（云端重试不得导致重复投递或状态丢失）；
- **ACK 保留期**：``mark_acked`` 写入 ``retained_until = now + retention``
  （默认 7 天，构造器可配）；:meth:`UploadQueue.purge_expired` 只删除
  「已过期且已 acked」的行——pending/uploaded 行不受保留期约束；
- **端侧 SQLite 生产配置**（§9.4.2，与 :mod:`ate_platform.offline.cache_store`
  完全一致）：``journal_mode=WAL`` / ``synchronous=NORMAL`` /
  ``busy_timeout=30000`` / ``cache_size=-20000``；
- **Schema 版本化**：``PRAGMA user_version`` 门控，发现更新版本数据库时
  拒绝打开。

本模块是纯本地状态机：不含任何 HTTP/NATS 传输与重连对账流程（T21 的
reconciliation 将消费 :meth:`list_pending` / :meth:`stats` 并驱动翻转）。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

#: 记录状态（§10.5 执行记录队列状态机）
STATE_PENDING = "pending"
STATE_UPLOADED = "uploaded"
STATE_ACKED = "acked"

#: ACK 后保留期默认值：7 天（§10.5「ACK 后保留 7 天再清理」）
DEFAULT_RETENTION_SECONDS = 7 * 24 * 3600.0

_SCHEMA_VERSION = 1

# §9.4.2 端侧 SQLite 生产配置（与 cache_store 一致；WAL 持久化在库文件中，
# synchronous/busy_timeout/cache_size 为每连接生效，故每次打开都应用）
_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA busy_timeout = 30000",
    "PRAGMA cache_size = -20000",
)

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS upload_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id     TEXT NOT NULL,
    execution_id   TEXT NOT NULL,
    seq_no         INTEGER NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending',
    payload_path   TEXT NOT NULL,
    retries        INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    uploaded_at    REAL,
    acked_at       REAL,
    retained_until REAL,
    UNIQUE (station_id, execution_id, seq_no)
)
"""

_RECORD_COLUMNS = (
    "id, station_id, execution_id, seq_no, state, payload_path, retries, "
    "created_at, uploaded_at, acked_at, retained_until"
)


class UploadQueueError(Exception):
    """上传队列层异常基类。"""


@dataclass(frozen=True)
class UploadRecord:
    """待上传执行记录视图（供列表/UI 与 T21 对账使用）。"""

    id: int
    station_id: str
    execution_id: str
    seq_no: int
    state: str  # 'pending' | 'uploaded' | 'acked'
    payload_path: str
    retries: int
    created_at: float
    uploaded_at: float | None
    acked_at: float | None
    retained_until: float | None


def _row_to_record(row: tuple) -> UploadRecord:
    return UploadRecord(
        id=row[0],
        station_id=row[1],
        execution_id=row[2],
        seq_no=row[3],
        state=row[4],
        payload_path=row[5],
        retries=row[6],
        created_at=row[7],
        uploaded_at=row[8],
        acked_at=row[9],
        retained_until=row[10],
    )


class UploadQueue:
    """pending→uploaded→acked 的端侧执行记录 SQLite 队列。

    线程安全：单连接（``check_same_thread=False``）+ :class:`threading.RLock`
    串行化全部操作——与 :class:`~ate_platform.offline.cache_store.OfflineCacheStore`
    同一模型；端侧入队频率为每次执行一条，锁开销可忽略。
    """

    #: 当前 schema 版本（PRAGMA user_version）
    SCHEMA_VERSION = _SCHEMA_VERSION

    def __init__(self, db_path: str | Path, retention_seconds: float = DEFAULT_RETENTION_SECONDS) -> None:
        if retention_seconds < 0:
            raise ValueError("retention_seconds must be >= 0")
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._retention = float(retention_seconds)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        with self._lock:
            for pragma in _PRAGMAS:
                self._conn.execute(pragma)
            self._conn.execute(_TABLE_DDL)
            self._init_schema_version()
            self._conn.commit()
        logger.info(
            "upload_queue_opened",
            path=str(self._path),
            schema_version=self.SCHEMA_VERSION,
            retention_seconds=self._retention,
        )

    # ------------------------------------------------------------------
    # Schema 版本管理
    # ------------------------------------------------------------------
    def _init_schema_version(self) -> None:
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if current == 0:
            self._conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        elif current > self.SCHEMA_VERSION:
            # 未来升级路径：旧代码遇到新库必须拒绝打开，防止写坏新 schema
            raise UploadQueueError(
                f"upload queue db at {self._path} has schema v{current}, "
                f"but this code only understands v{self.SCHEMA_VERSION}"
            )

    # ------------------------------------------------------------------
    # 入队（幂等键去重）
    # ------------------------------------------------------------------
    def enqueue(
        self, station_id: str, execution_id: str, seq_no: int, payload_path: str
    ) -> UploadRecord:
        """登记一条待上传记录；幂等键命中时原样返回既有行。

        幂等语义：同 ``(station_id, execution_id, seq_no)`` 重发（云端/本地
        重试）不新建行、不重置状态——uploaded/acked 进度绝不因重复入队丢失。
        """
        if not isinstance(payload_path, str) or not payload_path:
            raise ValueError("payload_path must be a non-empty str")
        with self._lock:
            existing = self._conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM upload_records "
                "WHERE station_id = ? AND execution_id = ? AND seq_no = ?",
                (station_id, execution_id, seq_no),
            ).fetchone()
            if existing is not None:
                logger.debug(
                    "upload_queue_enqueue_dedupe",
                    station_id=station_id,
                    execution_id=execution_id,
                    seq_no=seq_no,
                    state=existing[4],
                )
                return _row_to_record(existing)
            cur = self._conn.execute(
                "INSERT INTO upload_records "
                "(station_id, execution_id, seq_no, state, payload_path, retries, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (station_id, execution_id, seq_no, STATE_PENDING, payload_path, time.time()),
            )
            self._conn.commit()
            row = self._conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM upload_records WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        logger.debug(
            "upload_queue_enqueued",
            id=cur.lastrowid,
            station_id=station_id,
            execution_id=execution_id,
            seq_no=seq_no,
        )
        return _row_to_record(row)

    # ------------------------------------------------------------------
    # 状态翻转（每次翻转即时 commit —— 崩溃安全）
    # ------------------------------------------------------------------
    def mark_uploaded(self, record_id: int) -> bool:
        """``pending → uploaded``（上传器批量发出后调用）。

        Returns:
            True 表示翻转成功；False 表示行不存在或已越过该状态
            （幂等友好，供 T21 对账安全重放）。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE upload_records SET state = ?, uploaded_at = ? "
                "WHERE id = ? AND state = ?",
                (STATE_UPLOADED, time.time(), record_id, STATE_PENDING),
            )
            self._conn.commit()
        changed = cur.rowcount > 0
        logger.debug("upload_queue_uploaded", id=record_id, changed=changed)
        return changed

    def mark_acked(self, record_id: int) -> bool:
        """``→ acked``（服务器确认后调用），并写入 ACK 保留期截止时间。

        ACK 是权威终态：允许从 pending 或 uploaded 直接到达（本地 uploaded
        写入即使在上传途中崩溃丢失，服务器的 ACK 仍应被如实落库）。
        ``retained_until = now + retention``（构造器可配，默认 7 天）。

        Returns:
            True 表示翻转成功；False 表示行不存在或已是 acked。
        """
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE upload_records SET state = ?, acked_at = ?, retained_until = ? "
                "WHERE id = ? AND state != ?",
                (STATE_ACKED, now, now + self._retention, record_id, STATE_ACKED),
            )
            self._conn.commit()
        changed = cur.rowcount > 0
        logger.debug("upload_queue_acked", id=record_id, changed=changed)
        return changed

    def record_retry(self, record_id: int) -> bool:
        """上传失败时累加重试计数（状态保持不变，供 T21 上传器退避参考）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE upload_records SET retries = retries + 1 WHERE id = ?",
                (record_id,),
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 过期清理（只删「已过期且已 acked」）
    # ------------------------------------------------------------------
    def purge_expired(self, now: float | None = None) -> int:
        """删除保留期已过的 acked 行，返回删除行数。

        仅 ``state='acked' AND retained_until <= now`` 的行会被删除；
        pending/uploaded 行不受保留期约束（尚未完成投递确认，删了就丢数据）。
        ``now`` 参数仅供测试注入时钟；生产路径省略即取当前时间。
        """
        ts = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM upload_records WHERE state = ? AND retained_until <= ?",
                (STATE_ACKED, ts),
            )
            self._conn.commit()
        if cur.rowcount:
            logger.info("upload_queue_purged", removed=cur.rowcount)
        return cur.rowcount

    # ------------------------------------------------------------------
    # 视图（供上传器批量取件 / UI / T21 对账）
    # ------------------------------------------------------------------
    def get(self, record_id: int) -> UploadRecord | None:
        """按行 id 取单条记录；不存在返回 None。"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM upload_records WHERE id = ?",
                (record_id,),
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_pending(self) -> list[UploadRecord]:
        """列出全部 pending 记录（插入序 = 上传批次序）。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM upload_records WHERE state = ? ORDER BY id",
                (STATE_PENDING,),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def list_all(self) -> list[UploadRecord]:
        """列出全部记录（插入序），供诊断/测试视图。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM upload_records ORDER BY id"
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def stats(self) -> dict[str, int]:
        """各状态计数视图：``{"pending": n, "uploaded": n, "acked": n, "total": n}``。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) FROM upload_records GROUP BY state"
            ).fetchall()
        counts = dict(rows)
        total = sum(counts.values())
        return {
            STATE_PENDING: counts.get(STATE_PENDING, 0),
            STATE_UPLOADED: counts.get(STATE_UPLOADED, 0),
            STATE_ACKED: counts.get(STATE_ACKED, 0),
            "total": total,
        }

    # ------------------------------------------------------------------
    # 生命周期 / 诊断
    # ------------------------------------------------------------------
    def journal_mode(self) -> str:
        """返回当前 journal_mode（测试/诊断用，生产恒为 wal）。"""
        with self._lock:
            return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])

    @property
    def path(self) -> Path:
        """数据库文件路径。"""
        return self._path

    def close(self) -> None:
        """关闭连接（WAL 文件在正常关闭时自动 checkpoint）。"""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> UploadQueue:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
