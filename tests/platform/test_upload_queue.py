"""T20 端侧待上传执行记录队列测试（设计文档 §10.5 执行记录上传队列）。

覆盖契约：
- 状态机 pending → uploaded → acked：每次翻转即时 commit（WAL 写前日志），
  崩溃后重开连接状态不丢；
- 幂等键 (station_id, execution_id, seq_no)：重复入队返回既有行且不回退已推进
  的状态（云端重试不得把 uploaded/acked 打回 pending，也不得重发 acked 项）；
- ACK 后保留期（默认 7 天，构造器可配）：retained_until = acked_at + retention，
  purge_expired 只删除「已过期且已 acked」的行——pending/uploaded/未到期 acked
  一律存活；
- list_pending / stats 视图与 schema 版本门控（PRAGMA user_version）。
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from ate_platform.offline.upload_queue import (
    STATE_ACKED,
    STATE_PENDING,
    STATE_UPLOADED,
    UploadQueue,
    UploadQueueError,
)

DEFAULT_RETENTION = 7 * 24 * 3600.0


@pytest.fixture()
def queue(tmp_path):
    return UploadQueue(tmp_path / "upload_queue.db")


def _enqueue_three(q: UploadQueue) -> list[int]:
    ids = []
    for seq in range(3):
        rec = q.enqueue("st-01", "exec-A", seq, f"records/exec-A/{seq}.jsonl")
        ids.append(rec.id)
    return ids


# ----------------------------------------------------------------------
# 入队 + 幂等键
# ----------------------------------------------------------------------
class TestEnqueue:
    def test_enqueue_persists_pending(self, queue):
        rec = queue.enqueue("st-01", "exec-A", 0, "records/a/0.jsonl")
        assert rec.state == STATE_PENDING
        assert rec.station_id == "st-01"
        assert rec.execution_id == "exec-A"
        assert rec.seq_no == 0
        assert rec.payload_path == "records/a/0.jsonl"
        assert rec.retries == 0
        assert rec.uploaded_at is None and rec.acked_at is None
        # 确实落库（新连接可读）
        with sqlite3.connect(str(queue.path)) as conn:
            row = conn.execute(
                "SELECT state FROM upload_records WHERE id = ?", (rec.id,)
            ).fetchone()
        assert row == (STATE_PENDING,)

    def test_dedupe_same_key_returns_existing_row(self, queue):
        first = queue.enqueue("st-01", "exec-A", 7, "records/a/7.jsonl")
        again = queue.enqueue("st-01", "exec-A", 7, "records/a/7.jsonl")
        assert again.id == first.id
        assert queue.stats()["total"] == 1

    def test_dedupe_preserves_advanced_state(self, queue):
        first = queue.enqueue("st-01", "exec-A", 7, "records/a/7.jsonl")
        assert queue.mark_uploaded(first.id)
        again = queue.enqueue("st-01", "exec-A", 7, "records/a/7.jsonl")
        assert again.id == first.id
        assert again.state == STATE_UPLOADED  # 不打回 pending

    def test_same_seq_different_execution_is_distinct(self, queue):
        a = queue.enqueue("st-01", "exec-A", 0, "a.jsonl")
        b = queue.enqueue("st-01", "exec-B", 0, "b.jsonl")
        c = queue.enqueue("st-02", "exec-A", 0, "c.jsonl")
        assert len({a.id, b.id, c.id}) == 3


# ----------------------------------------------------------------------
# 状态机翻转
# ----------------------------------------------------------------------
class TestTransitions:
    def test_mark_uploaded_transition(self, queue):
        rec = queue.enqueue("st-01", "exec-A", 0, "a.jsonl")
        assert queue.mark_uploaded(rec.id) is True
        after = queue.get(rec.id)
        assert after.state == STATE_UPLOADED
        assert after.uploaded_at is not None

    def test_mark_uploaded_unknown_id_returns_false(self, queue):
        assert queue.mark_uploaded(424242) is False

    def test_mark_acked_sets_retention_default_7d(self, queue):
        rec = queue.enqueue("st-01", "exec-A", 0, "a.jsonl")
        queue.mark_uploaded(rec.id)
        before = time.time()
        assert queue.mark_acked(rec.id) is True
        after = queue.get(rec.id)
        assert after.state == STATE_ACKED
        assert after.acked_at is not None
        assert before + DEFAULT_RETENTION - 1 <= after.retained_until <= time.time() + DEFAULT_RETENTION + 1

    def test_mark_acked_custom_retention(self, tmp_path):
        q = UploadQueue(tmp_path / "q.db", retention_seconds=0.05)
        rec = q.enqueue("st-01", "exec-A", 0, "a.jsonl")
        q.mark_uploaded(rec.id)
        q.mark_acked(rec.id)
        after = q.get(rec.id)
        assert after.retained_until is not None
        assert after.retained_until - after.acked_at == pytest.approx(0.05, abs=1e-6)

    def test_mark_acked_unknown_id_returns_false(self, queue):
        assert queue.mark_acked(424242) is False

    def test_full_lifecycle_happy_path(self, queue):
        rec = queue.enqueue("st-01", "exec-A", 0, "a.jsonl")
        assert queue.stats() == {"pending": 1, "uploaded": 0, "acked": 0, "total": 1}
        queue.mark_uploaded(rec.id)
        assert queue.stats()["uploaded"] == 1
        queue.mark_acked(rec.id)
        stats = queue.stats()
        assert stats["acked"] == 1 and stats["pending"] == 0 and stats["total"] == 1


# ----------------------------------------------------------------------
# 过期清理：只删「已过期且已 acked」
# ----------------------------------------------------------------------
class TestPurgeExpired:
    def test_purge_removes_only_expired_acked(self, tmp_path):
        q = UploadQueue(tmp_path / "q.db", retention_seconds=0.05)
        keep_pending = q.enqueue("st-01", "e1", 0, "p.jsonl")
        keep_uploaded = q.enqueue("st-01", "e1", 1, "u.jsonl")
        keep_fresh_ack = q.enqueue("st-01", "e1", 2, "f.jsonl")
        doomed_ack = q.enqueue("st-01", "e1", 3, "d.jsonl")
        q.mark_uploaded(keep_uploaded.id)
        # doomed：睡眠前完成整个生命周期，其保留期窗口在睡眠后必然过期
        q.mark_uploaded(doomed_ack.id)
        q.mark_acked(doomed_ack.id)
        time.sleep(0.08)  # doomed_ack 的 retained_until（+0.05s）已过期
        # 存活 ack 在睡眠后才确认 → retained_until 仍在未来
        fresh2 = q.enqueue("st-01", "e1", 4, "f2.jsonl")
        for r in (keep_fresh_ack, fresh2):
            q.mark_uploaded(r.id)
            q.mark_acked(r.id)

        removed = q.purge_expired()
        assert removed == 1
        surviving_ids = {r.id for r in q.list_all()}
        assert doomed_ack.id not in surviving_ids
        assert {keep_pending.id, keep_uploaded.id, keep_fresh_ack.id, fresh2.id} <= surviving_ids

    def test_purge_boundary_inclusive_expiry(self, tmp_path):
        q = UploadQueue(tmp_path / "q.db", retention_seconds=100.0)
        rec = q.enqueue("st-01", "e1", 0, "a.jsonl")
        q.mark_uploaded(rec.id)
        q.mark_acked(rec.id)
        stored = q.get(rec.id)
        expiry = stored.retained_until
        assert q.purge_expired(now=expiry - 0.001) == 0  # 未到期 → 存活
        assert q.purge_expired(now=expiry) == 1  # 到期边界（<=）→ 删除

    def test_purge_never_touches_non_acked_even_if_stale(self, queue):
        rec = queue.enqueue("st-01", "e1", 0, "a.jsonl")
        queue.mark_uploaded(rec.id)
        # created_at 很久以前也不该删——只有 acked 行受保留期约束
        assert queue.purge_expired(now=time.time() + DEFAULT_RETENTION * 10) == 0
        assert queue.get(rec.id).state == STATE_UPLOADED


# ----------------------------------------------------------------------
# 崩溃安全重开（WAL 持久化）
# ----------------------------------------------------------------------
class TestCrashReopen:
    def test_reopen_preserves_all_transitions(self, tmp_path):
        db = tmp_path / "q.db"
        q1 = UploadQueue(db)
        ids = _enqueue_three(q1)
        q1.mark_uploaded(ids[0])
        q1.mark_uploaded(ids[1])
        q1.mark_acked(ids[0])
        q1.close()  # 模拟进程退出（WAL 正常 checkpoint）

        q2 = UploadQueue(db)
        states = {rid: q2.get(rid).state for rid in ids}
        assert states == {
            ids[0]: STATE_ACKED,
            ids[1]: STATE_UPLOADED,
            ids[2]: STATE_PENDING,
        }
        assert q2.stats() == {"pending": 1, "uploaded": 1, "acked": 1, "total": 3}
        # 重开后幂等键依然生效
        dup = q2.enqueue("st-01", "exec-A", 0, "records/exec-A/0.jsonl")
        assert dup.id == ids[0]

    def test_wal_mode_on(self, queue):
        mode = queue.journal_mode()
        assert mode.lower() == "wal"

    def test_newer_schema_version_rejected_on_open(self, tmp_path):
        db = tmp_path / "q.db"
        UploadQueue(db).close()
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        with pytest.raises(UploadQueueError, match="schema v99"):
            UploadQueue(db)


# ----------------------------------------------------------------------
# 视图：list_pending / stats / list_all / get
# ----------------------------------------------------------------------
class TestViews:
    def test_list_pending_order_and_filtering(self, queue):
        ids = _enqueue_three(queue)
        queue.mark_uploaded(ids[0])
        pending = queue.list_pending()
        assert [r.id for r in pending] == [ids[1], ids[2]]  # 插入序

    def test_stats_counts_all_states(self, queue):
        ids = _enqueue_three(queue)
        queue.mark_uploaded(ids[0])
        queue.mark_acked(ids[0])
        assert queue.stats() == {"pending": 2, "uploaded": 0, "acked": 1, "total": 3}

    def test_get_unknown_id_returns_none(self, queue):
        assert queue.get(987654) is None
