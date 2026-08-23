"""T19 离线脚本磁盘缓存测试（设计文档 §10.5.2 脚本缓存层）。

覆盖契约：
- 脚本文件 + sidecar 元数据（版本 / SHA256 / 时间戳）落盘，原子写入（tmp+rename）；
- 读取时校验文件哈希：不匹配回退到最近可用版本并 logging.warning 点名 script_id，
  绝不静默返回损坏内容；
- 最新版本解析（Windows 时钟粒度粗 → 以写入序号决胜）、显式版本锁定；
- 幂等重存、云端下发校验和验证、列表健康视图、删除/清理助手。
"""

from __future__ import annotations

import hashlib
import json
import logging

import pytest

from ate_platform.offline.script_cache import (
    ScriptCorruptionError,
    ScriptMissError,
    ScriptVersionMismatchError,
    sha256_text,
)

SCRIPT_V1 = "def measure(station):\n    return {'v': 1}\n"
SCRIPT_V2 = "def measure(station):\n    return {'v': 2}\n"
SCRIPT_V3 = "def measure(station):\n    return {'v': 3}\n"


@pytest.fixture()
def cache(tmp_path):
    from ate_platform.offline.script_cache import OfflineScriptCache

    return OfflineScriptCache(tmp_path / "script_cache")


@pytest.fixture()
def caplog_warn(caplog):
    caplog.set_level(logging.WARNING, logger="ate_platform.offline.script_cache")
    return caplog


# ----------------------------------------------------------------------
# 存取往返 + 落盘布局
# ----------------------------------------------------------------------
class TestStoreReadRoundtrip:
    def test_roundtrip_content_and_disk_layout(self, cache, tmp_path):
        cache.store_script("fw-check", "v1", SCRIPT_V1)
        assert cache.get_script("fw-check", "v1") == SCRIPT_V1
        # 磁盘上确有内容文件 + sidecar 元数据（sha256/version/时间戳）
        script_dir = tmp_path / "script_cache" / "fw-check"
        metas = list(script_dir.glob("*.meta.json"))
        contents = [p for p in script_dir.glob("*") if p.suffix != ".json"]
        assert len(metas) == 1 and len(contents) == 1
        meta = json.loads(metas[0].read_text(encoding="utf-8"))
        assert meta["version"] == "v1"
        assert meta["sha256"] == hashlib.sha256(SCRIPT_V1.encode("utf-8")).hexdigest()
        assert isinstance(meta["stored_at"], float)

    def test_latest_version_resolution(self, cache):
        cache.store_script("fw-check", "v1", SCRIPT_V1)
        cache.store_script("fw-check", "v2", SCRIPT_V2)
        assert cache.get_script("fw-check") == SCRIPT_V2

    def test_explicit_version_pin(self, cache):
        cache.store_script("fw-check", "v1", SCRIPT_V1)
        cache.store_script("fw-check", "v2", SCRIPT_V2)
        assert cache.get_script("fw-check", "v1") == SCRIPT_V1

    def test_atomic_write_no_tmp_leftovers(self, cache, tmp_path):
        cache.store_script("fw-check", "v1", SCRIPT_V1)
        leftovers = list((tmp_path / "script_cache").rglob("*.tmp"))
        assert leftovers == []


# ----------------------------------------------------------------------
# 损坏检测 → 回退上一可用版本 + 告警
# ----------------------------------------------------------------------
class TestCorruptionFallback:
    def test_corrupt_newest_falls_back_to_last_good_with_warning(self, cache, tmp_path, caplog_warn):
        cache.store_script("fw-check", "v1", SCRIPT_V1)
        cache.store_script("fw-check", "v2", SCRIPT_V2)
        # 直接篡改 v2 在磁盘上的字节（模拟磁盘损坏/半写）
        v2_file = next(
            p
            for p in (tmp_path / "script_cache" / "fw-check").glob("*")
            if p.suffix != ".json" and "v2" in p.name
        )
        v2_file.write_bytes(b"def measure(station):\n    return {'HACKED'}\n")

        served = cache.get_script("fw-check")  # latest → v2 已坏 → 回退 v1
        assert served == SCRIPT_V1
        assert "fw-check" in caplog_warn.text
        assert any(r.levelno == logging.WARNING for r in caplog_warn.records)

    def test_corrupt_pinned_version_falls_back_with_warning(self, cache, tmp_path, caplog_warn):
        cache.store_script("fw-check", "v1", SCRIPT_V1)
        cache.store_script("fw-check", "v2", SCRIPT_V2)
        target = next(
            p
            for p in (tmp_path / "script_cache" / "fw-check").glob("*")
            if p.suffix != ".json" and "v2" in p.name
        )
        target.write_bytes(b"garbage")

        assert cache.get_script("fw-check", "v2") == SCRIPT_V1
        assert "fw-check" in caplog_warn.text

    def test_all_versions_corrupt_raises_never_serves_silently(self, cache, tmp_path, caplog_warn):
        cache.store_script("fw-check", "v1", SCRIPT_V1)
        for p in (tmp_path / "script_cache" / "fw-check").glob("*"):
            if p.suffix != ".json":
                p.write_bytes(b"\x00corrupt")
        with pytest.raises(ScriptCorruptionError, match="fw-check"):
            cache.get_script("fw-check")
        assert "fw-check" in caplog_warn.text

    def test_missing_content_file_counts_as_corrupt(self, cache, tmp_path, caplog_warn):
        cache.store_script("fw-check", "v1", SCRIPT_V1)
        cache.store_script("fw-check", "v2", SCRIPT_V2)
        for p in (tmp_path / "script_cache" / "fw-check").glob("*"):
            if p.suffix != ".json" and "v2" in p.name:
                p.unlink()
        assert cache.get_script("fw-check") == SCRIPT_V1
        assert "fw-check" in caplog_warn.text


# ----------------------------------------------------------------------
# 缺失 / 版本不一致
# ----------------------------------------------------------------------
class TestMissAndMismatch:
    def test_unknown_script_raises_miss(self, cache):
        with pytest.raises(ScriptMissError, match="no-such"):
            cache.get_script("no-such")

    def test_pinned_never_cached_version_raises_mismatch(self, cache):
        cache.store_script("fw-check", "v1", SCRIPT_V1)
        with pytest.raises(ScriptVersionMismatchError, match="v9"):
            cache.get_script("fw-check", "v9")


# ----------------------------------------------------------------------
# 幂等重存 + 云端校验和验证
# ----------------------------------------------------------------------
class TestStoreSemantics:
    def test_idempotent_restore_preserves_entry_and_timestamp(self, cache):
        cache.store_script("fw-check", "v1", SCRIPT_V1)
        first = cache.list_scripts("fw-check")[0]
        cache.store_script("fw-check", "v1", SCRIPT_V1)  # 云端重试重发
        entries = cache.list_scripts("fw-check")
        assert len(entries) == 1
        assert entries[0].stored_at == first.stored_at
        assert cache.get_script("fw-check", "v1") == SCRIPT_V1

    def test_restore_same_version_changed_content_updates(self, cache):
        cache.store_script("fw-check", "v1", SCRIPT_V1)
        cache.store_script("fw-check", "v1", SCRIPT_V3)  # 同版本内容变化 → 覆盖更新
        assert cache.get_script("fw-check", "v1") == SCRIPT_V3

    def test_cloud_checksum_validated_on_store(self, cache):
        good = hashlib.sha256(SCRIPT_V1.encode("utf-8")).hexdigest()
        cache.store_script("fw-check", "v1", SCRIPT_V1, checksum=good)  # 与上传时哈希一致
        with pytest.raises(ValueError, match="checksum mismatch"):
            cache.store_script("fw-check", "v2", SCRIPT_V2, checksum="0" * 64)

    def test_windows_clock_tie_broken_by_insertion_order(self, cache):
        # Windows time.time() 粒度 ~15ms：同秒连存三个版本，最新解析必须确定
        for i, content in enumerate((SCRIPT_V1, SCRIPT_V2, SCRIPT_V3), start=1):
            cache.store_script("fw-check", f"r{i}", content)
        assert cache.get_script("fw-check") == SCRIPT_V3


# ----------------------------------------------------------------------
# 列表 / 删除 / 清理
# ----------------------------------------------------------------------
class TestListDeletePrune:
    def test_list_scripts_status_view_with_intact_flag(self, cache, tmp_path):
        cache.store_script("fw-check", "v1", SCRIPT_V1)
        cache.store_script("other", "r1", SCRIPT_V2)
        entries = cache.list_scripts()
        assert {(e.script_id, e.version) for e in entries} == {("fw-check", "v1"), ("other", "r1")}
        assert all(e.intact and e.size_bytes > 0 and len(e.checksum) == 64 for e in entries)

        victim = next(p for p in (tmp_path / "script_cache" / "fw-check").glob("*") if p.suffix != ".json")
        victim.write_bytes(b"tampered")
        statuses = {e.version: e for e in cache.list_scripts("fw-check")}
        assert statuses["v1"].intact is False

    def test_delete_single_version_then_all(self, cache):
        cache.store_script("fw-check", "v1", SCRIPT_V1)
        cache.store_script("fw-check", "v2", SCRIPT_V2)
        assert cache.delete("fw-check", "v2") == 1
        assert cache.get_script("fw-check") == SCRIPT_V1
        assert cache.delete("fw-check") == 1
        with pytest.raises(ScriptMissError):
            cache.get_script("fw-check")
        assert cache.delete("fw-check") == 0  # 幂等友好

    def test_prune_keeps_last_n_newest(self, cache):
        for i, content in enumerate((SCRIPT_V1, SCRIPT_V2, SCRIPT_V3), start=1):
            cache.store_script("fw-check", f"r{i}", content)
        removed = cache.prune(keep_last_n=2)
        assert removed == 1
        remaining = [e.version for e in cache.list_scripts("fw-check")]
        assert remaining == ["r3", "r2"]
        with pytest.raises(ScriptVersionMismatchError):
            cache.get_script("fw-check", "r1")


# ----------------------------------------------------------------------
# 杂项
# ----------------------------------------------------------------------
class TestMisc:
    def test_sha256_text_helper_matches_hashlib(self):
        assert sha256_text(SCRIPT_V1) == hashlib.sha256(SCRIPT_V1.encode("utf-8")).hexdigest()

    def test_empty_ids_rejected(self, cache):
        with pytest.raises(ValueError):
            cache.store_script("", "v1", SCRIPT_V1)
        with pytest.raises(ValueError):
            cache.store_script("fw-check", "", SCRIPT_V1)

    def test_non_str_content_rejected(self, cache):
        with pytest.raises(TypeError):
            cache.store_script("fw-check", "v1", b"bytes-not-allowed")  # type: ignore[arg-type]
