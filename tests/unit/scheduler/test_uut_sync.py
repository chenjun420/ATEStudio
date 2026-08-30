"""UUTManager / SyncBarrier 测试（F6 多 UUT 同步屏障）。

覆盖：
- UUT 状态机（idle/testing/passed/failed）与分配/释放
- 同步屏障：所有 UUT 到达后同时放行
- 屏障超时强制解除 + 未到达 UUT 标记 failed（§6.3.7 死锁防护）
- 屏障复用（同一名字多轮）
- 显式 release 解除等待
"""

from __future__ import annotations

import threading
import time

import pytest

from ate_platform.scheduler.uut_sync import UUT, SyncBarrier, UUTManager, UUTState

# ---------------------------------------------------------------------------
# UUT 状态机
# ---------------------------------------------------------------------------


def test_uut_state_transitions() -> None:
    uut = UUT(uut_id="UUT_0")
    assert uut.state == UUTState.IDLE
    assert not uut.busy
    uut.start_test()
    assert uut.state == UUTState.TESTING
    assert uut.busy
    uut.finish(passed=True)
    assert uut.state == UUTState.PASSED
    uut.reset()
    assert uut.state == UUTState.IDLE
    uut.finish(passed=False)
    assert uut.state == UUTState.FAILED


# ---------------------------------------------------------------------------
# UUTManager 分配
# ---------------------------------------------------------------------------


def test_manager_allocate_and_release() -> None:
    mgr = UUTManager(count=2)
    uut = mgr.allocate(fixture_id="fixture_A")
    assert uut is not None and uut.busy
    assert uut.fixture_id == "fixture_A"
    # 只剩一个空闲
    second = mgr.allocate()
    assert second is not None
    # 全部占用
    assert mgr.allocate() is None
    assert len(mgr.busy_uuts) == 2
    mgr.release(uut.uut_id, passed=True)
    assert mgr.get(uut.uut_id).state == UUTState.PASSED  # type: ignore[union-attr]
    assert len(mgr.busy_uuts) == 1


def test_manager_get_idle() -> None:
    mgr = UUTManager(uut_ids=["A", "B"])
    assert mgr.get_idle().uut_id == "A"  # type: ignore[union-attr]
    mgr.get("A").start_test()  # type: ignore[union-attr]
    assert mgr.get_idle().uut_id == "B"  # type: ignore[union-attr]


def test_manager_unknown_uut_raises() -> None:
    mgr = UUTManager(count=1)
    with pytest.raises(KeyError):
        mgr.wait_barrier("b", "NO_SUCH_UUT")


# ---------------------------------------------------------------------------
# 同步屏障
# ---------------------------------------------------------------------------


def test_barrier_all_arrive_together() -> None:
    """所有 UUT 到达后同时放行。"""
    mgr = UUTManager(count=3)
    results: dict[str, float] = {}
    lock = threading.Lock()
    arrived_at: list[float] = []

    def _worker(uut_id: str) -> None:
        res = mgr.wait_barrier("sync", uut_id, timeout=5.0)
        with lock:
            results[uut_id] = res.waited
            arrived_at.append(time.monotonic())

    threads = [threading.Thread(target=_worker, args=(uid,)) for uid in ["UUT_0", "UUT_1", "UUT_2"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "barrier should not deadlock"

    assert set(results) == {"UUT_0", "UUT_1", "UUT_2"}
    for res in results.values():
        assert res < 5.0
    # 三者几乎同时放行（同一 notify_all 窗口）
    spread = max(arrived_at) - min(arrived_at)
    assert spread < 0.5, f"barrier should release together, spread={spread:.3f}s"


def test_barrier_reusable_after_all_arrive() -> None:
    """满员放行后同一屏障名可复用（下一轮）。"""
    mgr = UUTManager(count=2)

    def _round(results: list, barrier_name: str) -> None:
        ts = [
            threading.Thread(
                target=lambda uid=uid: results.append(
                    mgr.wait_barrier(barrier_name, uid, timeout=5.0)
                )
            )
            for uid in ["UUT_0", "UUT_1"]
        ]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=10)
            assert not t.is_alive()

    # 第一轮
    r1: list = []
    _round(r1, "round")
    assert all(not r.timed_out and r.all_reached for r in r1)

    # 第二轮（屏障已消费重建）
    r2: list = []
    _round(r2, "round")
    assert all(r.all_reached for r in r2)


def test_barrier_timeout_marks_missing_failed() -> None:
    """超时强制解除，未到达 UUT 标记 failed（§6.3.7 死锁防护）。"""
    mgr = UUTManager(count=3)
    # 并发让 UUT_0/UUT_1 到达，UUT_2 缺席 → 都超时
    results: list = []
    lock = threading.Lock()

    def _worker(uid: str) -> None:
        res = mgr.wait_barrier("b", uid, timeout=0.4)
        with lock:
            results.append(res)

    ts = [threading.Thread(target=_worker, args=(f"UUT_{i}",)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)

    assert len(results) == 2
    for res in results:
        assert res.timed_out
        assert res.missing == {"UUT_2"}
    assert mgr.get("UUT_2").state == UUTState.FAILED  # type: ignore[union-attr]
    assert mgr.get("UUT_2").last_error  # type: ignore[union-attr]


def test_sync_barrier_external_release() -> None:
    """显式 release 解除等待（调度器中止兜底）。"""
    barrier = SyncBarrier("b", {"A", "B"}, timeout=30.0)
    result_holder: list[SyncBarrier | None] = []

    def _waiter() -> None:
        result_holder.append(barrier.wait("A", timeout=30.0))

    t = threading.Thread(target=_waiter)
    t.start()
    time.sleep(0.1)
    barrier.release()
    t.join(timeout=2.0)
    assert not t.is_alive()
    res = result_holder[0]
    assert res.missing == {"B"}  # type: ignore[union-attr]
    assert not res.timed_out  # type: ignore[union-attr]


def test_parallel_uut_throughput() -> None:
    """4 UUT 并行执行 + 屏障同步（AC-5 场景）。"""
    mgr = UUTManager(count=4)
    order: list[str] = []
    lock = threading.Lock()
    barrier_hit = threading.Event()

    def _run(uut_id: str) -> None:
        # 模拟每 UUT 完成自己的准备工作
        time.sleep(0.05 * int(uut_id[-1]))
        res = mgr.wait_barrier("start_measure", uut_id, timeout=3.0)
        assert res.all_reached, f"{uut_id} missing {res.missing}"
        with lock:
            order.append(uut_id)
            if len(order) == 4:
                barrier_hit.set()

    threads = [threading.Thread(target=_run, args=(f"UUT_{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert barrier_hit.is_set()
    # 屏障后 4 个全部到达才继续
    assert len(order) == 4
