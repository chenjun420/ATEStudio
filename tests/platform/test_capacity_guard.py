"""T22 离线缓存容量保护测试（设计文档 §10.5.4.5）。

覆盖契约：
- 目录体积统计排除瞬态临时文件（*.tmp / *.temp / *.part），递归含子目录；
- 越过软阈值（默认 500MB / 72h）触发告警事件，边沿触发不重复轰炸；
- 达到硬阈值暂停接收新下载（can_download()=False），已缓存内容照常执行；
- 清理（purge）后自动恢复下载；空目录/仅临时文件无年龄告警；
- 纯咨询式（advisory）：守卫自身绝不删除用户数据、绝不抛容量异常。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ate_platform.offline.cache_store import KIND_SEQUENCE, OfflineCacheStore
from ate_platform.offline.capacity_guard import (
    DEFAULT_SOFT_AGE_SECONDS,
    DEFAULT_SOFT_SIZE_BYTES,
    CapacityAlert,
    CapacityGuard,
    CapacityStatus,
)

_NOW = 1_800_000_000.0  # 固定注入时钟（秒）


def _clock() -> float:
    return _NOW


def _make_file(path: Path, size: int, age_seconds: float = 0.0) -> Path:
    """写入 size 字节文件并把 mtime 拨到 clock()-age_seconds。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    mtime = _NOW - age_seconds
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture()
def cache_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cache"
    d.mkdir()
    return d


@pytest.fixture()
def guard(cache_dir: Path) -> CapacityGuard:
    return CapacityGuard(cache_dir, clock=_clock)


# ----------------------------------------------------------------------
# 体积统计：排除瞬态临时文件
# ----------------------------------------------------------------------
class TestSizeAccounting:
    def test_empty_dir_reports_zero_and_never_pauses(self, guard: CapacityGuard) -> None:
        status = guard.check()
        assert status.size_bytes == 0
        assert status.file_count == 0
        assert status.oldest_age_seconds is None
        assert status.downloads_paused is False
        assert status.alerts == ()

    def test_size_excludes_temp_files_and_counts_nested(
        self, cache_dir: Path, guard: CapacityGuard
    ) -> None:
        _make_file(cache_dir / "payload.bin", 100)
        _make_file(cache_dir / "partial.download.tmp", 999)
        _make_file(cache_dir / "inflight.part", 888)
        _make_file(cache_dir / "scratch.temp", 777)
        _make_file(cache_dir / "sub" / "nested.bin", 50)  # 递归计入
        status = guard.measure()
        # 只计 payload.bin(100) + nested.bin(50)；三个临时文件全部排除
        assert status.size_bytes == 150
        assert status.file_count == 2

    def test_temp_suffixes_configurable(self, cache_dir: Path) -> None:
        g = CapacityGuard(cache_dir, clock=_clock, temp_suffixes=frozenset({".bak"}))
        _make_file(cache_dir / "keep.tmp", 10)  # 默认会被排除，这里不再视为临时
        _make_file(cache_dir / "old.bak", 20)
        status = g.measure()
        assert status.size_bytes == 10  # 仅 keep.tmp 计入

    def test_missing_cache_dir_treated_as_empty(self, tmp_path: Path) -> None:
        g = CapacityGuard(tmp_path / "does_not_exist_yet", clock=_clock)
        status = g.measure()
        assert status.size_bytes == 0
        assert status.oldest_age_seconds is None
        assert not (tmp_path / "does_not_exist_yet").exists()  # 观察者不产生副作用


# ----------------------------------------------------------------------
# 软阈值告警（边沿触发）
# ----------------------------------------------------------------------
class TestSoftAlerts:
    def test_alert_fires_once_crossing_soft_limit(self, cache_dir: Path) -> None:
        g = CapacityGuard(cache_dir, soft_size_bytes=100, clock=_clock)
        _make_file(cache_dir / "a.bin", 150)
        first = g.check()
        kinds = [(a.kind, a.level) for a in first.alerts]
        assert ("size", "soft") in kinds
        assert first.soft_exceeded is True
        # 边沿触发：立即复查不再重复告警
        second = g.check()
        assert second.alerts == ()
        assert second.soft_exceeded is True  # 状态仍在，但事件只发一次

    def test_alert_rearms_after_dropping_below(self, cache_dir: Path) -> None:
        g = CapacityGuard(
            cache_dir, soft_size_bytes=100, hard_size_bytes=500, clock=_clock
        )  # 显式抬高硬线，隔离软告警行为（缺省硬线=软线，越线会同时发硬事件）
        f = _make_file(cache_dir / "a.bin", 150)
        g.check()  # 告警一次
        f.unlink()
        below = g.check()
        assert below.soft_exceeded is False
        _make_file(cache_dir / "b.bin", 150)
        again = g.check()
        assert [a.level for a in again.alerts] == ["soft"]  # 回落再越线 → 重新告警

    def test_measure_is_pure_does_not_fire_alerts(self, cache_dir: Path) -> None:
        g = CapacityGuard(
            cache_dir, soft_size_bytes=10, hard_size_bytes=1000, clock=_clock
        )  # 500B 落在软硬之间 → 只发 soft 一条
        _make_file(cache_dir / "big.bin", 500)
        for _ in range(3):
            s = g.measure()
            assert s.alerts == ()  # measure 无副作用，不消耗边沿
        fired = g.check()
        assert len(fired.alerts) == 1  # 首次 check 才触发

    def test_defaults_are_doc_values_500mb_72h(self) -> None:
        assert DEFAULT_SOFT_SIZE_BYTES == 500 * 1024 * 1024
        assert DEFAULT_SOFT_AGE_SECONDS == 72 * 3600


# ----------------------------------------------------------------------
# 硬阈值暂停 + 已缓存内容不受影响 + purge 后恢复
# ----------------------------------------------------------------------
class TestHardPauseAndResume:
    def test_downloads_paused_at_hard_limit(self, cache_dir: Path) -> None:
        g = CapacityGuard(cache_dir, soft_size_bytes=50, hard_size_bytes=100, clock=_clock)
        _make_file(cache_dir / "a.bin", 60)
        mid = g.check()
        assert mid.downloads_paused is False  # 软区：仅告警
        assert mid.can_download is True
        _make_file(cache_dir / "b.bin", 60)
        full = g.check()
        assert full.hard_exceeded is True
        assert full.downloads_paused is True
        assert full.can_download is False
        levels = [a.level for a in full.alerts]
        assert levels == ["hard"]  # soft 已在软区检查时边沿触发过，此处只发 hard

    def test_cached_content_still_executable_while_paused(self, cache_dir: Path) -> None:
        """§10.5.4.5：暂停的是『新序列下发』，已缓存内容离线执行绝不受影响。"""
        store = OfflineCacheStore(cache_dir / "offline_cache.db")
        try:
            payload = "steps:\n  - id: s1\n"
            store.store_sequence("seq-x", "v1", payload)
            store.mark_acked(KIND_SEQUENCE, "seq-x", "v1")
            _make_file(cache_dir / "huge.bin", 10_000)
            tight = CapacityGuard(
                cache_dir, soft_size_bytes=100, hard_size_bytes=200, clock=_clock
            )
            assert tight.check().downloads_paused is True
            # 暂停状态下读取缓存照常成功（守卫是纯咨询，不拦截读路径）
            assert store.get_usable(KIND_SEQUENCE, "seq-x", "v1") == payload
        finally:
            store.close()

    def test_resume_after_purge(self, cache_dir: Path) -> None:
        g = CapacityGuard(cache_dir, soft_size_bytes=50, hard_size_bytes=100, clock=_clock)
        big = _make_file(cache_dir / "a.bin", 300)
        assert g.check().downloads_paused is True
        big.unlink()  # 用户/运维清理（守卫自己绝不删数据）
        after = g.check()
        assert after.downloads_paused is False
        assert after.can_download is True
        assert after.hard_exceeded is False

    def test_age_threshold_alerts_and_pauses(self, cache_dir: Path) -> None:
        g = CapacityGuard(cache_dir, soft_age_seconds=3600.0, hard_age_seconds=7200.0, clock=_clock)
        _make_file(cache_dir / "fresh.bin", 10, age_seconds=60.0)
        fresh = g.check()
        assert fresh.oldest_age_seconds == pytest.approx(60.0)
        assert fresh.alerts == ()
        _make_file(cache_dir / "stale.bin", 10, age_seconds=8000.0)
        hit = g.check()
        kinds_levels = {(a.kind, a.level) for a in hit.alerts}
        assert ("age", "soft") in kinds_levels
        assert ("age", "hard") in kinds_levels
        assert hit.downloads_paused is True  # 文档：超过阈值告警并暂停新下发

    def test_only_temp_files_means_no_age_signal(self, cache_dir: Path) -> None:
        g = CapacityGuard(cache_dir, soft_age_seconds=60.0, clock=_clock)
        _make_file(cache_dir / "junk.tmp", 5, age_seconds=999_999.0)
        status = g.check()
        assert status.oldest_age_seconds is None
        assert status.alerts == ()


# ----------------------------------------------------------------------
# 配置校验 / 监听器 / 导出
# ----------------------------------------------------------------------
class TestConfigAndEvents:
    def test_invalid_thresholds_rejected(self, cache_dir: Path) -> None:
        with pytest.raises(ValueError, match="soft_size_bytes"):
            CapacityGuard(cache_dir, soft_size_bytes=-1)
        with pytest.raises(ValueError, match="soft_age_seconds"):
            CapacityGuard(cache_dir, soft_age_seconds=-1.0)
        with pytest.raises(ValueError, match="hard.*soft"):
            CapacityGuard(cache_dir, soft_size_bytes=100, hard_size_bytes=99)

    def test_listener_receives_alerts_and_survives_exceptions(self, cache_dir: Path) -> None:
        received: list[CapacityAlert] = []

        def boom(_alert: CapacityAlert) -> None:
            raise RuntimeError("listener crash must not break the guard")

        g = CapacityGuard(
            cache_dir, soft_size_bytes=10, hard_size_bytes=10_000, clock=_clock
        )  # 只越软线 → 监听器恰好收到一条 soft
        g.add_listener(received.append)
        g.add_listener(boom)
        _make_file(cache_dir / "a.bin", 500)
        status = g.check()
        assert [a.level for a in received] == ["soft"]
        assert isinstance(status.alerts[0], CapacityAlert)

    def test_status_is_frozen(self, guard: CapacityGuard) -> None:
        status = guard.measure()
        with pytest.raises(Exception, match="frozen|cannot assign"):
            status.size_bytes = 42  # type: ignore[misc]
        assert isinstance(status, CapacityStatus)

    def test_package_exports(self) -> None:
        from ate_platform.offline import (
            CapacityAlert as ExportedAlert,
        )
        from ate_platform.offline import (
            CapacityGuard as ExportedGuard,
        )
        from ate_platform.offline import (
            CapacityStatus as ExportedStatus,
        )

        assert ExportedGuard is CapacityGuard
        assert ExportedAlert is CapacityAlert
        assert ExportedStatus is CapacityStatus
