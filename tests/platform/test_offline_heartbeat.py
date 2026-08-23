"""T23 端侧心跳断连检测测试（设计文档 §10.5 心跳超时 10s）。

覆盖契约：
- **10s 超时进入离线**：既有 worker 心跳通道停止后，超时窗口内两次连续
  miss（迟滞）才翻转 offline；单次 miss 绝不翻转（防抖动）；
- **恢复即清除**：任一心跳到达立即回到 online 并清零 miss 计数与
  entered_offline_at；
- **进程本地暂停不进入离线**（§10.5 约束）：pause_local 期间 check 不计
  miss、不翻转；resume 后以恢复时刻为新基线重新计时；
- **可注入时钟**：构造器注入 clock 可调用对象，全部时间推进确定性完成，
  测试零 sleep。

全部判定基于 FakeClock 的显式 advance，无任何真实等待。
"""

from __future__ import annotations

import dataclasses

import pytest

from ate_platform.offline import (
    DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    DEFAULT_REQUIRED_MISSES,
    STATE_OFFLINE,
    STATE_ONLINE,
    HeartbeatError,
    HeartbeatMonitor,
    HeartbeatStatus,
)


class FakeClock:
    """确定性时钟：显式 advance 推进，绝无真实等待。"""

    def __init__(self, t0: float = 0.0) -> None:
        self._t = float(t0)

    def advance(self, dt: float) -> None:
        self._t += dt

    def __call__(self) -> float:
        return self._t


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def monitor(clock) -> HeartbeatMonitor:
    return HeartbeatMonitor(clock=clock)


# ----------------------------------------------------------------------
# 初始状态
# ----------------------------------------------------------------------
class TestInitialState:
    def test_fresh_monitor_is_online(self, monitor):
        st = monitor.status
        assert st.state == STATE_ONLINE
        assert st.consecutive_misses == 0
        assert st.paused is False
        assert st.entered_offline_at is None

    def test_defaults_match_spec(self):
        assert DEFAULT_HEARTBEAT_TIMEOUT_SECONDS == 10.0
        assert DEFAULT_REQUIRED_MISSES == 2


# ----------------------------------------------------------------------
# 超时 + 迟滞（2 次连续 miss）
# ----------------------------------------------------------------------
class TestTimeoutHysteresis:
    def test_within_timeout_no_miss(self, monitor, clock):
        monitor.record_beat()
        clock.advance(9.9)
        st = monitor.check()
        assert st.state == STATE_ONLINE
        assert st.consecutive_misses == 0

    def test_timeout_boundary_is_strictly_greater(self, monitor, clock):
        """恰好 timeout 秒整不算 miss（严格大于语义），越过边界才算。"""
        monitor.record_beat()
        clock.advance(10.0)
        assert monitor.check().consecutive_misses == 0
        clock.advance(0.1)
        assert monitor.check().consecutive_misses == 1

    def test_single_miss_does_not_flip_offline(self, monitor, clock):
        """QA 场景：单次漏拍绝不翻转（迟滞防抖）。"""
        monitor.record_beat()
        clock.advance(10.5)
        st = monitor.check()
        assert st.state == STATE_ONLINE
        assert st.consecutive_misses == 1

    def test_two_consecutive_misses_enter_offline(self, monitor, clock):
        """QA 场景：心跳停止 → 10s 超时后第 2 次评估进入 offline。"""
        monitor.record_beat()
        clock.advance(10.5)
        assert monitor.check().state == STATE_ONLINE  # miss #1
        clock.advance(1.0)
        st = monitor.check()
        assert st.state == STATE_OFFLINE  # miss #2 → 翻转
        assert st.entered_offline_at == clock()
        assert st.seconds_since_last_beat == pytest.approx(11.5)

    def test_never_seen_beat_enters_offline_after_timeout(self, monitor, clock):
        """启动后从未收到心跳：以构造时刻为基线，同样走超时+迟滞。"""
        clock.advance(10.5)
        assert monitor.check().state == STATE_ONLINE
        clock.advance(1.0)
        assert monitor.check().state == STATE_OFFLINE


# ----------------------------------------------------------------------
# 心跳重置 / 恢复
# ----------------------------------------------------------------------
class TestRecovery:
    def test_beat_resets_miss_streak(self, monitor, clock):
        monitor.record_beat()
        clock.advance(10.5)
        assert monitor.check().consecutive_misses == 1
        monitor.record_beat()  # 心跳恢复 → 计数清零
        clock.advance(10.5)
        st = monitor.check()
        assert st.state == STATE_ONLINE
        assert st.consecutive_misses == 1  # 新一轮的第 1 次 miss

    def test_recovery_from_offline_clears_state(self, monitor, clock):
        monitor.record_beat()
        clock.advance(10.5)
        monitor.check()
        clock.advance(1.0)
        assert monitor.check().state == STATE_OFFLINE

        clock.advance(30.0)
        st = monitor.record_beat()  # 云端恢复，心跳重新到达
        assert st.state == STATE_ONLINE
        assert st.consecutive_misses == 0
        assert st.entered_offline_at is None

    def test_flapping_oscillation_never_enters_offline(self, monitor, clock):
        """抖动链路：每轮只漏一拍就来拍 → 永远到不了连续 2 次 miss。"""
        for _ in range(5):
            monitor.record_beat()
            clock.advance(10.5)
            assert monitor.check().state == STATE_ONLINE


# ----------------------------------------------------------------------
# 进程本地暂停（§10.5：本地暂停不得进入离线）
# ----------------------------------------------------------------------
class TestProcessLocalPause:
    def test_paused_check_never_counts_misses(self, monitor, clock):
        monitor.record_beat()
        monitor.pause_local()
        clock.advance(1000.0)
        for _ in range(3):
            st = monitor.check()
            assert st.state == STATE_ONLINE
            assert st.paused is True
            assert st.consecutive_misses == 0

    def test_resume_rebaselines_timeout_window(self, monitor, clock):
        monitor.pause_local()
        clock.advance(500.0)
        monitor.resume_local()
        clock.advance(9.9)
        assert monitor.check().state == STATE_ONLINE  # 新基线起 10s 内
        clock.advance(0.5)
        assert monitor.check().consecutive_misses == 1
        clock.advance(1.0)
        assert monitor.check().state == STATE_OFFLINE  # 恢复正常判定

    def test_pause_resume_roundtrip_online(self, monitor):
        monitor.pause_local()
        monitor.resume_local()
        st = monitor.status
        assert st.state == STATE_ONLINE
        assert st.paused is False


# ----------------------------------------------------------------------
# 状态视图 / 配置校验 / 监听器
# ----------------------------------------------------------------------
class TestContract:
    def test_status_is_frozen_dataclass(self, monitor):
        st = monitor.status
        assert isinstance(st, HeartbeatStatus)
        with pytest.raises(dataclasses.FrozenInstanceError):
            st.state = STATE_OFFLINE  # type: ignore[misc]

    def test_invalid_config_raises(self, clock):
        with pytest.raises(ValueError):
            HeartbeatMonitor(timeout_seconds=0.0, clock=clock)
        with pytest.raises(ValueError):
            HeartbeatMonitor(timeout_seconds=-1.0, clock=clock)
        with pytest.raises(ValueError):
            HeartbeatMonitor(required_misses=0, clock=clock)

    def test_error_hierarchy_base(self):
        assert issubclass(HeartbeatError, Exception)

    def test_listener_notified_on_transitions(self, clock):
        seen: list[tuple[str, str]] = []
        mon = HeartbeatMonitor(clock=clock)
        mon.add_listener(lambda old, new: seen.append((old, new)))

        mon.record_beat()
        clock.advance(10.5)
        mon.check()  # miss #1，无翻转
        clock.advance(1.0)
        mon.check()  # → offline
        mon.record_beat()  # → online
        assert seen == [(STATE_ONLINE, STATE_OFFLINE), (STATE_OFFLINE, STATE_ONLINE)]

    def test_package_export_available(self):
        import ate_platform.offline as pkg

        for name in (
            "HeartbeatMonitor",
            "HeartbeatStatus",
            "HeartbeatError",
            "STATE_ONLINE",
            "STATE_OFFLINE",
            "DEFAULT_HEARTBEAT_TIMEOUT_SECONDS",
            "DEFAULT_REQUIRED_MISSES",
        ):
            assert hasattr(pkg, name), name
