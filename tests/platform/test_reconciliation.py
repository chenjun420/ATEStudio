"""T21 重连对账测试（设计文档 §10.5 reconciliation after reconnect）。

覆盖契约：
- 队列清空：按时间序（插入序）重放 pending 上传，成功即 mark_uploaded+mark_acked；
- 幂等：第二遍 reconcile 完全 no-op——零上传、零 ACK、零冲突、零释放，
  状态绝不回退（队列 bool 翻转 + 缓存 acked 终态天然重放安全）；
- 冲突版本化：服务器对离线产生/未上报的缓存版本报 conflict 时，本地载荷以
  服务器指定的新版本重新落库并直接 ACK（服务器已预先同意），旧数据绝不静默丢弃；
- 拒绝隔离：服务器拒绝某条记录 → record_retry + quarantine，其余记录照常推进；
- 版本锁清空：对账结束 release_all()（T26 幂等 API）；
- reconciled status：ReconciliationReport 计数与时间窗（可注入时钟）。
"""

from __future__ import annotations

import dataclasses

import pytest

from ate_platform.offline.cache_store import (
    KIND_SEQUENCE,
    OfflineCacheStore,
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
from ate_platform.offline.script_cache import OfflineScriptCache
from ate_platform.offline.upload_queue import (
    STATE_ACKED,
    STATE_PENDING,
    UploadQueue,
)
from ate_platform.offline.version_lock import VersionLockManager


class FakeUploader:
    """结构化满足 ReconcileUploader 协议的可编程假传输（无真实 NATS/HTTP）。"""

    def __init__(self) -> None:
        self.upload_order: list[tuple[str, str, int]] = []
        self.reject_keys: set[tuple[str, str, int]] = set()
        self.explode_keys: set[tuple[str, str, int]] = set()
        self.resolutions: dict[tuple[str, str, str], VersionResolution] = {}
        self.script_reports: list[tuple[str, str, str]] = []
        self.corrupt_scripts: set[str] = set()

    def upload_record(self, record) -> bool:
        key = (record.station_id, record.execution_id, record.seq_no)
        if key in self.explode_keys:
            raise ConnectionError(f"transport exploded for {key}")
        self.upload_order.append(key)
        return key not in self.reject_keys

    def resolve_version(self, kind, entry_id, version, checksum) -> VersionResolution:
        return self.resolutions.get((kind, entry_id, version), VersionResolution(RESOLVE_CONFIRMED))

    def report_script(self, script_id, version, checksum) -> bool:
        if script_id in self.corrupt_scripts:
            return False
        self.script_reports.append((script_id, version, checksum))
        return True


class FakeClock:
    """手动推进的可注入时钟（纳秒级断言时间窗用）。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


@pytest.fixture()
def queue(tmp_path):
    return UploadQueue(tmp_path / "upload_queue.db")


@pytest.fixture()
def cache(tmp_path):
    return OfflineCacheStore(tmp_path / "cache.db")


@pytest.fixture()
def scripts(tmp_path):
    return OfflineScriptCache(tmp_path / "scripts")


@pytest.fixture()
def clock():
    return FakeClock()


def make_reconciler(queue, cache, scripts, clock, uploader):
    locks = VersionLockManager(cache, clock=clock)
    locks.set_online(False)
    rec = Reconciler(
        queue=queue, cache=cache, scripts=scripts, locks=locks, uploader=uploader, clock=clock
    )
    return rec, locks


# ----------------------------------------------------------------------
# Phase 1 — 上传队列清空（时间序重放）
# ----------------------------------------------------------------------
class TestQueueFlush:
    def test_pending_uploaded_in_time_order_and_acked(self, queue, cache, scripts, clock):
        uploader = FakeUploader()
        rec, _ = make_reconciler(queue, cache, scripts, clock, uploader)
        queue.enqueue("st-01", "exec-B", 0, "records/b/0.jsonl")
        queue.enqueue("st-01", "exec-A", 5, "records/a/5.jsonl")
        queue.enqueue("st-02", "exec-C", 1, "records/c/1.jsonl")

        report = rec.reconcile()

        # list_pending 插入序 = 时间序；重放必须保序
        assert uploader.upload_order == [
            ("st-01", "exec-B", 0),
            ("st-01", "exec-A", 5),
            ("st-02", "exec-C", 1),
        ]
        assert report.uploaded == 3
        assert all(r.state == STATE_ACKED for r in queue.list_all())
        assert queue.stats()["pending"] == 0

    def test_server_reject_quarantines_one_rest_proceed(self, queue, cache, scripts, clock):
        uploader = FakeUploader()
        uploader.reject_keys.add(("st-01", "exec-BAD", 0))
        rec, _ = make_reconciler(queue, cache, scripts, clock, uploader)
        bad = queue.enqueue("st-01", "exec-BAD", 0, "records/bad.jsonl")
        good = queue.enqueue("st-01", "exec-OK", 1, "records/ok.jsonl")

        report = rec.reconcile()

        assert report.uploaded == 1
        assert report.quarantined == 1
        assert [q.execution_id for q in report.quarantine] == ["exec-BAD"]
        assert isinstance(report.quarantine[0], QuarantinedItem)
        assert queue.get(bad.id).state == STATE_PENDING  # 数据保留，绝不静默丢弃
        assert queue.get(bad.id).retries == 1  # record_retry 已计数
        assert queue.get(good.id).state == STATE_ACKED  # 其余照常推进

    def test_transport_exception_does_not_abort_pass(self, queue, cache, scripts, clock):
        uploader = FakeUploader()
        uploader.explode_keys.add(("st-01", "exec-X", 0))
        rec, _ = make_reconciler(queue, cache, scripts, clock, uploader)
        queue.enqueue("st-01", "exec-X", 0, "x.jsonl")
        queue.enqueue("st-01", "exec-Y", 1, "y.jsonl")

        report = rec.reconcile()

        assert report.ok is True  # 单条故障不推翻整趟对账
        assert report.quarantined == 1
        assert report.uploaded == 1


# ----------------------------------------------------------------------
# Phase 2 — 未上报缓存版本对账（冲突版本化）
# ----------------------------------------------------------------------
class TestVersionResolution:
    def test_unacked_entry_confirmed_flips_to_acked(self, queue, cache, scripts, clock):
        uploader = FakeUploader()
        rec, _ = make_reconciler(queue, cache, scripts, clock, uploader)
        cache.store_sequence("seq-1", "v2", "payload-v2")  # cached（未上报）

        report = rec.reconcile()

        assert report.confirmed_entries == 1
        states = {e.version: e.state for e in cache.list_cached(KIND_SEQUENCE)}
        assert states["v2"] == "acked"

    def test_conflict_reversioned_via_public_store_api(self, queue, cache, scripts, clock):
        uploader = FakeUploader()
        uploader.resolutions[("sequence", "seq-1", "v2")] = VersionResolution(
            RESOLVE_CONFLICT, new_version="v2-r1"
        )
        rec, _ = make_reconciler(queue, cache, scripts, clock, uploader)
        cache.store_sequence("seq-1", "v2", "local-edit-payload")

        report = rec.reconcile()

        assert report.conflicts_resolved == 1
        # 新版本经公开 store API 落库且直接 ACK（服务器已预先同意新版本号）
        assert cache.get_usable(KIND_SEQUENCE, "seq-1", "v2-r1") == "local-edit-payload"
        states = {(e.version): e.state for e in cache.list_cached(KIND_SEQUENCE)}
        assert states["v2-r1"] == "acked"
        # 被取代的冲突旧行已删除（载荷由 v2-r1 完整承接）——否则每趟对账
        # 都会重复报同一冲突，永不收敛（幂等性要求）
        assert "v2" not in states

    def test_conflict_resolution_converges_across_passes(self, queue, cache, scripts, clock):
        uploader = FakeUploader()
        uploader.resolutions[("sequence", "seq-1", "v1")] = VersionResolution(
            RESOLVE_CONFLICT, new_version="v1-r1"
        )
        rec, _ = make_reconciler(queue, cache, scripts, clock, uploader)
        cache.store_sequence("seq-1", "v1", "draft")

        first = rec.reconcile()
        second = rec.reconcile()

        assert first.conflicts_resolved == 1
        assert second.conflicts_resolved == 0  # 已收敛：第二趟零冲突

    def test_conflict_without_new_version_quarantined_not_dropped(self, queue, cache, scripts, clock):
        uploader = FakeUploader()
        uploader.resolutions[("sequence", "seq-9", "v1")] = VersionResolution(RESOLVE_CONFLICT)
        rec, _ = make_reconciler(queue, cache, scripts, clock, uploader)
        cache.store_sequence("seq-9", "v1", "orphan")

        report = rec.reconcile()

        assert report.conflicts_resolved == 0
        assert report.quarantined == 1
        assert report.quarantine[0].reason == "version_conflict"
        assert cache.get_usable(KIND_SEQUENCE, "seq-9", "v1", require_acked=False) == "orphan"

    def test_acked_entries_never_re_reported(self, queue, cache, scripts, clock):
        uploader = FakeUploader()
        rec, _ = make_reconciler(queue, cache, scripts, clock, uploader)
        cache.store_sequence("seq-1", "v1", "p1")
        cache.mark_acked(KIND_SEQUENCE, "seq-1", "v1")

        report = rec.reconcile()

        assert report.confirmed_entries == 0
        assert report.conflicts_resolved == 0


# ----------------------------------------------------------------------
# Phase 3 — 脚本完整性巡检
# ----------------------------------------------------------------------
class TestScriptSweep:
    def test_intact_scripts_reported_corrupt_quarantined(self, queue, cache, scripts, clock):
        uploader = FakeUploader()
        uploader.corrupt_scripts.add("s-bad")
        rec, _ = make_reconciler(queue, cache, scripts, clock, uploader)
        scripts.store_script("s-good", "v1", "print('ok')")
        scripts.store_script("s-bad", "v1", "print('bad')")
        # 人为破坏 s-bad 内容文件（绕过 store 的原子写入）
        content_path = next(scripts._script_dir("s-bad").glob("*.script"))
        content_path.write_text("tampered", encoding="utf-8")

        report = rec.reconcile()

        assert len(uploader.script_reports) == 1
        assert uploader.script_reports[0][0] == "s-good"
        assert report.quarantined == 1
        assert report.quarantine[0].reason == "script_corrupt"
        assert report.quarantine[0].entry_id == "s-bad"


# ----------------------------------------------------------------------
# Phase 4 — 版本锁清空（T26 对账释放）
# ----------------------------------------------------------------------
class TestLockRelease:
    def test_locks_cleared_on_reconcile(self, queue, cache, scripts, clock):
        uploader = FakeUploader()
        rec, locks = make_reconciler(queue, cache, scripts, clock, uploader)
        cache.store_sequence("seq-1", "v1", "p")
        cache.mark_acked(KIND_SEQUENCE, "seq-1", "v1")
        locks.acquire(KIND_SEQUENCE, "seq-1", "v1", "exec-A")
        assert locks.list_locks() != []

        report = rec.reconcile()

        assert report.locks_released == 1
        assert locks.list_locks() == []
        assert locks.is_online is False  # 在线翻转由断网感知层负责，对账不越权


# ----------------------------------------------------------------------
# 幂等 —— 第二遍完全 no-op
# ----------------------------------------------------------------------
class TestIdempotency:
    def test_second_run_is_noop(self, queue, cache, scripts, clock):
        uploader = FakeUploader()
        rec, locks = make_reconciler(queue, cache, scripts, clock, uploader)
        queue.enqueue("st-01", "exec-A", 0, "a.jsonl")
        queue.enqueue("st-01", "exec-A", 1, "b.jsonl")
        cache.store_sequence("seq-1", "v1", "p")
        cache.store_sequence("seq-1", "v2", "p2")
        scripts.store_script("s-1", "v1", "code")
        cache.mark_acked(KIND_SEQUENCE, "seq-1", "v1")
        cache.mark_acked(KIND_SEQUENCE, "seq-1", "v2")  # 加锁前提：版本已过 ACK 门控
        locks.acquire(KIND_SEQUENCE, "seq-1", "v2", "exec-A")

        first = rec.reconcile()
        snapshot = queue.list_all()

        second = rec.reconcile()

        assert first.uploaded == 2 and first.locks_released == 1
        assert (second.uploaded, second.acked, second.confirmed_entries) == (0, 0, 0)
        assert second.conflicts_resolved == 0
        assert second.locks_released == 0
        assert second.quarantined == 0
        assert queue.list_all() == snapshot  # 无重复行、状态零回退
        assert uploader.upload_order.count(("st-01", "exec-A", 0)) == 1  # 绝不重复投递

    def test_rejected_record_retry_keeps_state_pending(self, queue, cache, scripts, clock):
        uploader = FakeUploader()
        uploader.reject_keys.add(("st-01", "e", 0))
        rec, _ = make_reconciler(queue, cache, scripts, clock, uploader)
        r = queue.enqueue("st-01", "e", 0, "a.jsonl")

        rec.reconcile()
        again = rec.reconcile()  # 重试语义：仍 pending，仍会再试（数据不丢）

        assert queue.get(r.id).state == STATE_PENDING
        assert queue.get(r.id).retries == 2
        assert again.quarantined == 1


# ----------------------------------------------------------------------
# Reconciled status 报告
# ----------------------------------------------------------------------
class TestReport:
    def test_report_counts_and_time_window(self, queue, cache, scripts, clock):
        uploader = FakeUploader()
        rec, _ = make_reconciler(queue, cache, scripts, clock, uploader)
        queue.enqueue("st-01", "exec-A", 0, "a.jsonl")

        class SlowSeam:
            """最小缝形态：仅 upload_record 的回调对象（其余方法缺省）。"""

            def upload_record(self, record):
                clock.advance(0.5)
                return True

        rec._uploader = SlowSeam()
        report = rec.reconcile()

        assert isinstance(report, ReconcileReport)
        assert report.started_at == 1000.0
        assert report.finished_at == 1000.5  # 结束时钟读取夹住一次 advance(0.5)
        assert report.duration == pytest.approx(0.5)
        assert report.ok is True
        assert rec.last_report is report

    def test_empty_station_reconciles_clean(self, queue, cache, scripts, clock):
        uploader = FakeUploader()
        rec, _ = make_reconciler(queue, cache, scripts, clock, uploader)

        report = rec.reconcile()

        assert report.ok is True
        assert (
            report.uploaded,
            report.acked,
            report.confirmed_entries,
            report.conflicts_resolved,
            report.quarantined,
            report.locks_released,
        ) == (0, 0, 0, 0, 0, 0)


# ----------------------------------------------------------------------
# 协议形状（seam 契约冻结，防 T24 接线漂移）
# ----------------------------------------------------------------------
class TestProtocolShape:
    def test_fake_uploader_satisfies_protocol_runtime_check(self):
        assert isinstance(FakeUploader(), ReconcileUploader)

    def test_version_resolution_frozen_defaults(self):
        res = VersionResolution(RESOLVE_CONFIRMED)
        assert res.new_version is None
        with pytest.raises(dataclasses.FrozenInstanceError):
            res.outcome = RESOLVE_CONFLICT  # frozen=True
