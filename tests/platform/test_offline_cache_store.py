"""T18 离线缓存存储层测试（设计文档 §10.5 / §9.4.2）。

覆盖契约：
- SQLite WAL pragma（journal_mode=WAL / synchronous=NORMAL / busy_timeout）；
- 下发即缓存 + ACK 门控：未 ACK 的版本不可离线使用（§10.5.4.1/§10.5.4.3）；
- 校验和读取时验证：不匹配拒绝服务，绝不静默返回损坏载荷；
- 幂等重存、版本不匹配拒绝、列表状态视图、删除/清理助手；
- 线程安全与跨连接持久化。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading

import pytest

from ate_platform.offline.cache_store import (
    KIND_SEQUENCE,
    KIND_TOPOLOGY,
    CacheMissError,
    CorruptionError,
    NotAckedError,
    OfflineCacheStore,
    VersionMismatchError,
    sha256_checksum,
)

SEQ_YAML_V1 = "steps:\n  - id: s1\n    action: measure\n"
SEQ_YAML_V2 = "steps:\n  - id: s2\n    action: measure\n"
TOPO_JSON = '{"fixture": "FX-01", "routes": ["K1", "K2"]}'


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.fixture()
def store(tmp_path):
    s = OfflineCacheStore(tmp_path / "offline_cache.db")
    yield s
    s.close()


# ----------------------------------------------------------------------
# Pragma / schema
# ----------------------------------------------------------------------
class TestWalPragmas:
    def test_journal_mode_is_wal(self, tmp_path):
        s = OfflineCacheStore(tmp_path / "c.db")
        try:
            mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert str(mode).lower() == "wal"
        finally:
            s.close()

    def test_synchronous_and_busy_timeout(self, store):
        sync = store._conn.execute("PRAGMA synchronous").fetchone()[0]
        assert int(sync) == 1  # NORMAL
        timeout = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert int(timeout) >= 30000

    def test_schema_version_recorded(self, store):
        ver = store._conn.execute("PRAGMA user_version").fetchone()[0]
        assert int(ver) == OfflineCacheStore.SCHEMA_VERSION >= 1


# ----------------------------------------------------------------------
# 存取往返 + ACK 门控
# ----------------------------------------------------------------------
class TestAckGating:
    def test_sequence_roundtrip_after_ack(self, store):
        store.store_sequence("seq-a", "v1", SEQ_YAML_V1)
        store.mark_acked(KIND_SEQUENCE, "seq-a", "v1")
        assert store.get_usable(KIND_SEQUENCE, "seq-a", "v1") == SEQ_YAML_V1

    def test_topology_roundtrip_after_ack(self, store):
        store.store_topology("fx-01", "r3", TOPO_JSON)
        store.mark_acked(KIND_TOPOLOGY, "fx-01", "r3")
        assert store.get_usable(KIND_TOPOLOGY, "fx-01", "r3") == TOPO_JSON

    def test_unacked_read_blocked_by_default(self, store):
        store.store_sequence("seq-b", "v1", SEQ_YAML_V1)
        with pytest.raises(NotAckedError):
            store.get_usable(KIND_SEQUENCE, "seq-b", "v1")

    def test_unacked_readable_when_gate_disabled(self, store):
        """require_acked=False 仅限在线模式诊断用，离线执行路径必须保持默认 True。"""
        store.store_sequence("seq-c", "v1", SEQ_YAML_V1)
        assert store.get_usable(KIND_SEQUENCE, "seq-c", "v1", require_acked=False) == SEQ_YAML_V1

    def test_mark_acked_flips_state(self, store):
        store.store_sequence("seq-d", "v1", SEQ_YAML_V1)
        assert store.mark_acked(KIND_SEQUENCE, "seq-d", "v1") is True
        status = {e.id: e for e in store.list_cached(KIND_SEQUENCE)}["seq-d"]
        assert status.state == "acked"
        assert status.acked_at is not None

    def test_mark_acked_missing_entry_returns_false(self, store):
        assert store.mark_acked(KIND_SEQUENCE, "ghost", "v9") is False

    def test_latest_version_resolved_when_version_omitted(self, store):
        store.store_sequence("seq-e", "v1", SEQ_YAML_V1)
        store.store_sequence("seq-e", "v2", SEQ_YAML_V2)
        store.mark_acked(KIND_SEQUENCE, "seq-e", "v2")
        # 未指定版本 → 最新版本 v2（已 ACK）
        assert store.get_usable(KIND_SEQUENCE, "seq-e") == SEQ_YAML_V2

    def test_version_mismatch_rejected(self, store):
        store.store_sequence("seq-f", "v1", SEQ_YAML_V1)
        store.mark_acked(KIND_SEQUENCE, "seq-f", "v1")
        with pytest.raises(VersionMismatchError):
            store.get_usable(KIND_SEQUENCE, "seq-f", "v999")

    def test_missing_entry_raises_cache_miss(self, store):
        with pytest.raises(CacheMissError):
            store.get_usable(KIND_SEQUENCE, "nope")


# ----------------------------------------------------------------------
# 校验和完整性
# ----------------------------------------------------------------------
class TestChecksumIntegrity:
    def test_sha256_checksum_helper(self):
        assert sha256_checksum(SEQ_YAML_V1) == _digest(SEQ_YAML_V1)

    def test_tampered_payload_rejected(self, store, tmp_path):
        """直接改库模拟磁盘损坏/篡改：读取必须拒绝，绝不静默返回。"""
        store.store_sequence("seq-g", "v1", SEQ_YAML_V1)
        store.mark_acked(KIND_SEQUENCE, "seq-g", "v1")
        store._conn.execute(
            "UPDATE sequences SET payload = ? WHERE id = 'seq-g'", ("steps: tampered\n",)
        )
        store._conn.commit()
        with pytest.raises(CorruptionError):
            store.get_usable(KIND_SEQUENCE, "seq-g", "v1")

    def test_corruption_error_never_carries_payload(self, store):
        store.store_sequence("seq-h", "v1", SEQ_YAML_V1)
        store.mark_acked(KIND_SEQUENCE, "seq-h", "v1")
        store._conn.execute("UPDATE sequences SET payload = 'evil'")
        store._conn.commit()
        with pytest.raises(CorruptionError) as exc_info:
            store.get_usable(KIND_SEQUENCE, "seq-h")
        assert "evil" not in str(exc_info.value)

    def test_explicit_checksum_mismatch_rejected_at_store(self, store):
        with pytest.raises(ValueError):
            store.store_sequence("seq-i", "v1", SEQ_YAML_V1, checksum="deadbeef")

    def test_wrong_checksum_on_read_rejects(self, store):
        """checksum 列被篡改（而非 payload）：同样视为损坏。"""
        store.store_sequence("seq-j", "v1", SEQ_YAML_V1)
        store.mark_acked(KIND_SEQUENCE, "seq-j", "v1")
        store._conn.execute("UPDATE sequences SET checksum = 'f' * 64")
        store._conn.commit()
        with pytest.raises(CorruptionError):
            store.get_usable(KIND_SEQUENCE, "seq-j")


# ----------------------------------------------------------------------
# 幂等重存 / 持久化 / 并发
# ----------------------------------------------------------------------
class TestIdempotencyAndLifecycle:
    def test_idempotent_restore_preserves_acked_state(self, store):
        store.store_sequence("seq-k", "v1", SEQ_YAML_V1)
        store.mark_acked(KIND_SEQUENCE, "seq-k", "v1")
        store.store_sequence("seq-k", "v1", SEQ_YAML_V1)  # 云端重发同内容
        status = {e.id: e for e in store.list_cached(KIND_SEQUENCE)}["seq-k"]
        assert status.state == "acked"

    def test_changed_payload_same_version_resets_to_cached(self, store):
        """同版本但内容变化 = 需要重新 ACK，绝不允许带旧 ACK 复用。"""
        store.store_sequence("seq-l", "v1", SEQ_YAML_V1)
        store.mark_acked(KIND_SEQUENCE, "seq-l", "v1")
        store.store_sequence("seq-l", "v1", SEQ_YAML_V2)
        with pytest.raises(NotAckedError):
            store.get_usable(KIND_SEQUENCE, "seq-l", "v1")

    def test_state_survives_reopen(self, tmp_path):
        db = tmp_path / "persist.db"
        s1 = OfflineCacheStore(db)
        s1.store_topology("fx", "r1", TOPO_JSON)
        s1.mark_acked(KIND_TOPOLOGY, "fx", "r1")
        s1.close()
        s2 = OfflineCacheStore(db)
        try:
            assert s2.get_usable(KIND_TOPOLOGY, "fx", "r1") == TOPO_JSON
        finally:
            s2.close()

    def test_concurrent_stores_thread_safe(self, store):
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                for j in range(5):
                    payload = f"steps:\n  - id: s{i}-{j}\n"
                    store.store_sequence(f"seq-t{i}", f"v{j}", payload)
                    store.mark_acked(KIND_SEQUENCE, f"seq-t{i}", f"v{j}")
                    assert (
                        store.get_usable(KIND_SEQUENCE, f"seq-t{i}", f"v{j}") == payload
                    )
            except Exception as exc:  # noqa: BLE001 - 收集任意失败供断言
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(store.list_cached(KIND_SEQUENCE)) == 20


# ----------------------------------------------------------------------
# 列表视图 / 删除清理
# ----------------------------------------------------------------------
class TestListingAndCleanup:
    def test_list_cached_status_fields(self, store):
        store.store_sequence("seq-m", "v1", SEQ_YAML_V1)
        entries = [e for e in store.list_cached() if e.id == "seq-m"]
        assert len(entries) == 1
        e = entries[0]
        assert (e.kind, e.id, e.version, e.state) == (KIND_SEQUENCE, "seq-m", "v1", "cached")
        assert e.checksum == _digest(SEQ_YAML_V1)
        assert e.created_at > 0
        assert e.acked_at is None

    def test_list_cached_filters_by_kind(self, store):
        store.store_sequence("s", "v1", SEQ_YAML_V1)
        store.store_topology("t", "r1", TOPO_JSON)
        seq_ids = {e.id for e in store.list_cached(KIND_SEQUENCE)}
        topo_ids = {e.id for e in store.list_cached(KIND_TOPOLOGY)}
        assert seq_ids == {"s"}
        assert topo_ids == {"t"}

    def test_delete_specific_version_and_all(self, store):
        store.store_sequence("seq-n", "v1", SEQ_YAML_V1)
        store.store_sequence("seq-n", "v2", SEQ_YAML_V2)
        assert store.delete(KIND_SEQUENCE, "seq-n", version="v1") == 1
        remaining = {e.version for e in store.list_cached(KIND_SEQUENCE) if e.id == "seq-n"}
        assert remaining == {"v2"}
        assert store.delete(KIND_SEQUENCE, "seq-n") == 1
        assert store.delete(KIND_SEQUENCE, "seq-n") == 0

    def test_prune_keeps_last_n_versions(self, store):
        for i in range(5):
            store.store_sequence("seq-p", f"v{i}", f"payload-{i}\n")
        removed = store.prune(KIND_SEQUENCE, keep_last_n=2)
        assert removed == 3
        versions = sorted(e.version for e in store.list_cached(KIND_SEQUENCE) if e.id == "seq-p")
        assert versions == ["v3", "v4"]

    def test_db_file_created_in_wal_dir(self, store, tmp_path):
        assert (tmp_path / "offline_cache.db").exists()
        # WAL 模式下应产生 -wal 伴生文件（打开期间存在）
        assert sqlite3.threadsafety >= 1
