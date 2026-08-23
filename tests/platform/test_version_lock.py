"""T26 离线版本锁测试（设计文档 §10.5.4.2 版本一致性）。

覆盖契约：
- 联网禁止加锁（must not lock when online）；离线读不隐式加锁；
- 执行期锁定版本快照：断网期间不隐式升级，进行中任务始终用锁定版本；
- 新任务用新版本：更新到达离线 → 缓存新增版本行（绝不覆写已锁定的旧行），
  对账 ACK + 释放锁后新任务解析到新版本；
- 锁定版本不可变：离线改写已锁定版本被拒绝，载荷字节级不变；
- ACK 门控集成：未 ACK 版本不可加锁、不可离线使用（NotAckedError 透传）；
- 可重入/幂等加锁、冲突拒绝、对账释放（单条 / 按执行 / 全量）。
"""

from __future__ import annotations

import pytest

from ate_platform.offline import (
    KIND_SEQUENCE,
    KIND_TOPOLOGY,
    AlreadyLockedError,
    CacheMissError,
    LockedVersionImmutableError,
    NotAckedError,
    OfflineCacheStore,
    OnlineLockRejectedError,
    VersionLockManager,
)

SEQ_V1 = "steps:\n  - id: s1\n    action: measure\n"
SEQ_V2 = "steps:\n  - id: s2\n    action: measure\n"
SEQ_V1_TAMPERED = "steps:\n  - id: s1-TAMPERED\n    action: measure\n"
TOPO_T1 = '{"fixture": "FX-01", "routes": ["K1"]}'
TOPO_T2 = '{"fixture": "FX-02", "routes": ["K2"]}'


class _FakeClock:
    """可注入时钟（house pattern）：手动推进，断言 locked_at 精确可控。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def tick(self, dt: float = 1.0) -> float:
        self.now += dt
        return self.now


@pytest.fixture()
def store(tmp_path):
    s = OfflineCacheStore(tmp_path / "offline_cache.db")
    yield s
    s.close()


@pytest.fixture()
def clock():
    return _FakeClock()


@pytest.fixture()
def mgr(store, clock):
    m = VersionLockManager(store, clock=clock)
    m.set_online(False)  # 测试默认离线自治态
    return m


def _seed_acked_v1(store) -> None:
    store.store_sequence("seq-1", "v1", SEQ_V1)
    store.mark_acked(KIND_SEQUENCE, "seq-1", "v1")


# ----------------------------------------------------------------------
# 联网门控（must not lock when online）
# ----------------------------------------------------------------------
class TestOnlineGate:
    def test_acquire_rejected_while_online(self, store):
        _seed_acked_v1(store)
        m = VersionLockManager(store)  # 默认在线
        with pytest.raises(OnlineLockRejectedError):
            m.acquire(KIND_SEQUENCE, "seq-1", "v1", "exec-A")
        assert m.get_lock(KIND_SEQUENCE, "seq-1") is None

    def test_online_read_does_not_pin(self, store):
        _seed_acked_v1(store)
        m = VersionLockManager(store)  # 在线
        version, payload = m.resolve_pinned(KIND_SEQUENCE, "seq-1", "exec-A")
        assert version == "v1" and payload == SEQ_V1
        assert m.get_lock(KIND_SEQUENCE, "seq-1") is None  # 在线绝不落锁
        assert m.list_locks() == []


# ----------------------------------------------------------------------
# 加锁 / 快照语义
# ----------------------------------------------------------------------
class TestPinning:
    def test_acquire_offline_records_lock_with_injected_clock(self, store, mgr, clock):
        _seed_acked_v1(store)
        lock = mgr.acquire(KIND_SEQUENCE, "seq-1", "v1", "exec-A")
        assert (lock.kind, lock.entry_id, lock.version, lock.execution_id) == (
            KIND_SEQUENCE,
            "seq-1",
            "v1",
            "exec-A",
        )
        assert lock.locked_at == clock.now
        assert mgr.get_lock(KIND_SEQUENCE, "seq-1") == lock

    def test_reentrant_acquire_same_args_returns_same_lock(self, store, mgr):
        _seed_acked_v1(store)
        first = mgr.acquire(KIND_SEQUENCE, "seq-1", "v1", "exec-A")
        second = mgr.acquire(KIND_SEQUENCE, "seq-1", "v1", "exec-A")  # 重入：幂等
        assert first == second
        assert len(mgr.list_locks()) == 1

    def test_conflicting_lock_rejected(self, store, mgr):
        _seed_acked_v1(store)
        mgr.acquire(KIND_SEQUENCE, "seq-1", "v1", "exec-A")
        with pytest.raises(AlreadyLockedError):  # 不同执行抢同一条目
            mgr.acquire(KIND_SEQUENCE, "seq-1", "v1", "exec-B")
        with pytest.raises(AlreadyLockedError):  # 同执行换绑别的版本也拒绝
            mgr.acquire(KIND_SEQUENCE, "seq-1", "v2", "exec-A")


# ----------------------------------------------------------------------
# 进行中 vs 新任务（QA happy path）
# ----------------------------------------------------------------------
class TestRunningVsNewTask:
    def test_update_arrives_offline_running_keeps_locked_v1(self, store, mgr):
        _seed_acked_v1(store)
        mgr.acquire(KIND_SEQUENCE, "seq-1", "v1", "exec-A")
        mgr.store_update(KIND_SEQUENCE, "seq-1", "v2", SEQ_V2)  # 更新离线到达 → 新版本行
        versions = [e.version for e in store.list_cached(KIND_SEQUENCE)]
        assert versions == ["v2", "v1"]  # 两行并存，v1 未被覆写
        # 进行中任务继续用锁定 v1；新任务在 v2 未 ACK 前也只能用已 ACK 白名单里的 v1
        assert mgr.resolve_pinned(KIND_SEQUENCE, "seq-1", "exec-A") == ("v1", SEQ_V1)
        assert mgr.resolve_pinned(KIND_SEQUENCE, "seq-1", "exec-B") == ("v1", SEQ_V1)

    def test_after_ack_and_release_new_task_gets_v2(self, store, mgr):
        _seed_acked_v1(store)
        mgr.acquire(KIND_SEQUENCE, "seq-1", "v1", "exec-A")
        mgr.store_update(KIND_SEQUENCE, "seq-1", "v2", SEQ_V2)
        store.mark_acked(KIND_SEQUENCE, "seq-1", "v2")  # 对账：云端确认 v2 下发成功
        assert mgr.release(KIND_SEQUENCE, "seq-1")  # 对账释放锁
        assert mgr.resolve_pinned(KIND_SEQUENCE, "seq-1", "exec-B") == ("v2", SEQ_V2)
        # 旧版本行完好保留（§10.5.2 保留最近 N 个版本的前提）
        assert store.get_usable(KIND_SEQUENCE, "seq-1", "v1") == SEQ_V1


# ----------------------------------------------------------------------
# 锁定版本不可变（QA failure path）
# ----------------------------------------------------------------------
class TestImmutability:
    def test_mutate_locked_version_rejected_and_payload_intact(self, store, mgr):
        _seed_acked_v1(store)
        mgr.acquire(KIND_SEQUENCE, "seq-1", "v1", "exec-A")
        with pytest.raises(LockedVersionImmutableError):
            mgr.store_update(KIND_SEQUENCE, "seq-1", "v1", SEQ_V1_TAMPERED)
        assert store.get_usable(KIND_SEQUENCE, "seq-1", "v1") == SEQ_V1  # 字节级不变

    def test_store_different_version_while_locked_leaves_locked_row_intact(self, store, mgr):
        _seed_acked_v1(store)
        mgr.acquire(KIND_SEQUENCE, "seq-1", "v1", "exec-A")
        mgr.store_update(KIND_SEQUENCE, "seq-1", "v2", SEQ_V2)  # 新版本行不受限
        assert store.get_usable(KIND_SEQUENCE, "seq-1", "v1") == SEQ_V1

    def test_store_update_checksum_mismatch_propagates_valueerror(self, store, mgr):
        _seed_acked_v1(store)
        with pytest.raises(ValueError, match="checksum mismatch"):
            mgr.store_update(KIND_SEQUENCE, "seq-1", "v2", SEQ_V2, checksum="deadbeef")
        assert [e.version for e in store.list_cached(KIND_SEQUENCE)] == ["v1"]


# ----------------------------------------------------------------------
# ACK 门控集成（未 ACK 版本离线不可用）
# ----------------------------------------------------------------------
class TestAckGateIntegration:
    def test_acquire_on_unacked_version_raises_notacked_and_no_lock(self, store, mgr):
        _seed_acked_v1(store)
        store.store_sequence("seq-1", "v2", SEQ_V2)  # 未 ACK
        with pytest.raises(NotAckedError):
            mgr.acquire(KIND_SEQUENCE, "seq-1", "v2", "exec-A")
        assert mgr.get_lock(KIND_SEQUENCE, "seq-1") is None  # 加锁失败不留半把锁

    def test_resolve_without_acked_versions_propagates_cache_gates(self, store, mgr):
        store.store_sequence("seq-x", "v9", SEQ_V2)  # 只有未 ACK 版本
        with pytest.raises(NotAckedError):
            mgr.resolve_pinned(KIND_SEQUENCE, "seq-x", "exec-A")
        with pytest.raises(CacheMissError):  # 完全未知条目
            mgr.resolve_pinned(KIND_SEQUENCE, "nope", "exec-A")


# ----------------------------------------------------------------------
# 对账释放（unlock on reconcile）
# ----------------------------------------------------------------------
class TestRelease:
    def test_release_idempotent_and_unlocks(self, store, mgr):
        _seed_acked_v1(store)
        mgr.acquire(KIND_SEQUENCE, "seq-1", "v1", "exec-A")
        assert mgr.release(KIND_SEQUENCE, "seq-1") is True
        assert mgr.release(KIND_SEQUENCE, "seq-1") is False  # 幂等：重复释放安全
        assert mgr.get_lock(KIND_SEQUENCE, "seq-1") is None

    def test_release_for_execution_scoped_across_kinds(self, store, mgr):
        store.store_sequence("seq-1", "v1", SEQ_V1)
        store.mark_acked(KIND_SEQUENCE, "seq-1", "v1")
        store.store_topology("topo-1", "t1", TOPO_T1)
        store.mark_acked(KIND_TOPOLOGY, "topo-1", "t1")
        store.store_topology("topo-2", "t2", TOPO_T2)
        store.mark_acked(KIND_TOPOLOGY, "topo-2", "t2")

        mgr.acquire(KIND_SEQUENCE, "seq-1", "v1", "exec-A")
        mgr.acquire(KIND_TOPOLOGY, "topo-1", "t1", "exec-A")
        mgr.acquire(KIND_TOPOLOGY, "topo-2", "t2", "exec-B")

        assert mgr.release_for_execution("exec-A") == 2  # 跨 kind 清理同一执行的锁
        assert mgr.get_lock(KIND_SEQUENCE, "seq-1") is None
        assert mgr.get_lock(KIND_TOPOLOGY, "topo-1") is None
        assert mgr.get_lock(KIND_TOPOLOGY, "topo-2") is not None  # 其他执行不受影响
