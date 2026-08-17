"""多 UUT 实例管理与同步屏障（设计文档 §6.3.7 / F6）。

- :class:`UUT` — 单个被测单元实例（状态机 idle/testing/passed/failed）。
- :class:`UUTManager` — 多 UUT 池：空闲分配、按 ID 获取、同步屏障。
- :class:`SyncBarrier` — 线程安全同步屏障：所有参与者到达后放行；
  超时强制解除并标记未到达 UUT 为 failed（防死锁，见 §6.3.7 死锁防护）。

与文档参考实现（忙轮询 + 非线程安全）的改进：
- 使用 ``threading.Condition`` 的 ``wait``/``notify_all``，无忙等；
- 屏障可复用（每轮测试重复到达）；
- 超时语义明确：返回 ``BarrierResult``，标记未到达者为 failed。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class UUTState(Enum):
    """UUT 生命周期状态。"""

    IDLE = "idle"
    TESTING = "testing"
    PASSED = "passed"
    FAILED = "failed"


@dataclass
class UUT:
    """单个被测单元实例。

    Attributes:
        uut_id: UUT 标识。
        fixture_id: 绑定的夹具 ID（可选）。
        variables: UUT 级变量空间。
    """

    uut_id: str
    fixture_id: str | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    state: UUTState = UUTState.IDLE
    last_error: str | None = None

    @property
    def busy(self) -> bool:
        """是否处于测试中。"""
        return self.state == UUTState.TESTING

    def start_test(self) -> None:
        """开始测试（idle → testing）。"""
        self.state = UUTState.TESTING

    def finish(self, passed: bool) -> None:
        """结束测试并记录结果。"""
        self.state = UUTState.PASSED if passed else UUTState.FAILED

    def reset(self) -> None:
        """复位到 idle（新一轮测试）。"""
        self.state = UUTState.IDLE
        self.last_error = None


@dataclass
class BarrierResult:
    """同步屏障的一次结果。

    Attributes:
        barrier_name: 屏障名。
        reached: 实际到达的 UUT 集合。
        missing: 未到达的 UUT 集合（超时解除时非空）。
        timed_out: 是否超时。
        waited: 实际等待秒数。
    """

    barrier_name: str
    reached: set[str]
    missing: set[str] = field(default_factory=set)
    timed_out: bool = False
    waited: float = 0.0

    @property
    def all_reached(self) -> bool:
        """所有 UUT 是否都到达。"""
        return not self.missing


class SyncBarrier:
    """线程安全同步屏障（可复用）。

    ``wait`` 会阻塞直到所有注册参与者到达；``release`` 显式解除（可作
    外部兜底）。超时后自动解除：调用方通过返回值判定缺员并标记 failed。
    """

    def __init__(self, name: str, participants: set[str], timeout: float = 60.0) -> None:
        """初始化屏障。

        Args:
            name: 屏障名。
            participants: 需要到达的参与者（UUT ID）集合。
            timeout: 等待超时秒数。
        """
        self.name = name
        self._participants: set[str] = set(participants)
        self._arrived: set[str] = set()
        self.timeout = timeout
        self._cond = threading.Condition()
        self._released = False
        self._release_reason: str | None = None

    def wait(
        self,
        uut_id: str,
        timeout: float | None = None,
    ) -> BarrierResult:
        """注册到达并等待其余参与者。

        Args:
            uut_id: 到达的 UUT ID。
            timeout: 覆盖默认超时（None 用构造超时）。

        Returns:
            屏障结果（含缺员集合，超时解除时非空）。
        """
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        with self._cond:
            self._arrived.add(uut_id)
            self._cond.notify_all()

            while not self._released and self._arrived != self._participants:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._force_release("timeout")
                    break
                self._cond.wait(timeout=remaining)

            # 显式 release 或已满员即放行
            if not self._released and self._arrived == self._participants:
                self._released = True

            reached = set(self._arrived)
            missing = self._participants - reached
            return BarrierResult(
                barrier_name=self.name,
                reached=reached,
                missing=missing,
                timed_out=self._release_reason == "timeout",
                waited=self.timeout - (deadline - time.monotonic()),
            )

    def release(self) -> None:
        """显式解除屏障（外部兜底，如调度器中止）。"""
        with self._cond:
            self._force_release("external")

    def _force_release(self, reason: str) -> None:
        """置为已解除并唤醒所有等待者。"""
        self._released = True
        self._release_reason = reason
        self._cond.notify_all()

    @property
    def is_released(self) -> bool:
        """是否已解除。"""
        with self._cond:
            return self._released


class UUTManager:
    """多 UUT 实例池与同步管理。

    Attributes:
        uuts: uut_id → UUT 映射。
    """

    def __init__(
        self,
        count: int = 1,
        uut_ids: list[str] | None = None,
        fixture_ids: dict[str, str] | None = None,
    ) -> None:
        """初始化 UUT 池。

        Args:
            count: 默认 UUT 数量（uut_ids 为空时生成 ``UUT_0..N-1``）。
            uut_ids: 自定义 UUT ID 列表。
            fixture_ids: uut_id → fixture_id 初始绑定。
        """
        ids = uut_ids if uut_ids is not None else [f"UUT_{i}" for i in range(count)]
        fixture_ids = fixture_ids or {}
        self.uuts: dict[str, UUT] = {
            uid: UUT(uut_id=uid, fixture_id=fixture_ids.get(uid)) for uid in ids
        }
        # RLock：allocate() → get_idle() 等方法内部会嵌套获取本锁
        self._lock = threading.RLock()
        self._barriers: dict[str, SyncBarrier] = {}

    # ------------------------------------------------------------------
    # UUT 访问
    # ------------------------------------------------------------------
    def get_idle(self) -> UUT | None:
        """返回第一个空闲 UUT（无则 None）。"""
        with self._lock:
            for uut in self.uuts.values():
                if not uut.busy:
                    return uut
            return None

    def get(self, uut_id: str) -> UUT | None:
        """按 ID 获取 UUT。"""
        with self._lock:
            return self.uuts.get(uut_id)

    def allocate(self, fixture_id: str | None = None) -> UUT | None:
        """分配一个空闲 UUT 并置为测试中。

        Args:
            fixture_id: 可选绑定夹具。

        Returns:
            分配到的 UUT；无空闲返回 None。
        """
        with self._lock:
            uut = self.get_idle()
            if uut is None:
                return None
            uut.start_test()
            if fixture_id is not None:
                uut.fixture_id = fixture_id
            return uut

    def release(self, uut_id: str, passed: bool = True) -> None:
        """结束测试并释放 UUT。"""
        with self._lock:
            uut = self.uuts.get(uut_id)
            if uut is not None:
                uut.finish(passed)

    def reset_all(self) -> None:
        """复位全部 UUT 到 idle（新一轮批量测试前）。"""
        with self._lock:
            for uut in self.uuts.values():
                uut.reset()

    @property
    def uut_ids(self) -> list[str]:
        """全部 UUT ID。"""
        return list(self.uuts.keys())

    @property
    def busy_uuts(self) -> list[str]:
        """当前测试中的 UUT ID。"""
        with self._lock:
            return [u for u, ut in self.uuts.items() if ut.busy]

    # ------------------------------------------------------------------
    # 同步屏障
    # ------------------------------------------------------------------
    def wait_barrier(
        self,
        barrier_name: str,
        uut_id: str,
        timeout: float = 60.0,
    ) -> BarrierResult:
        """同步屏障：所有 UUT 到达后才放行（§6.3.7）。

        首次调用创建屏障（参与者为当前全部 UUT）；之后各 UUT 到达并等待。
        超时强制解除，未到达 UUT 标记为 failed 并返回缺员集合。

        Args:
            barrier_name: 屏障名（同一轮中可复用，满员后自动复位）。
            uut_id: 到达的 UUT ID。
            timeout: 超时秒数。

        Returns:
            BarrierResult（含 reached/missing/timed_out）。

        Raises:
            KeyError: uut_id 不属于本管理器。
            RuntimeError: 屏障被显式解除（调度器中止）时等待者返回。
        """
        if uut_id not in self.uuts:
            msg = f"Unknown UUT: {uut_id}"
            raise KeyError(msg)

        with self._lock:
            barrier = self._barriers.get(barrier_name)
            if barrier is None or barrier.is_released:
                # 创建/重建屏障（满员后被消费，下一次到达重建）
                barrier = SyncBarrier(
                    barrier_name,
                    set(self.uuts.keys()),
                    timeout=timeout,
                )
                self._barriers[barrier_name] = barrier

        result = barrier.wait(uut_id, timeout=timeout)

        # 超时解除：标记未到达 UUT 为 failed（§6.3.7 死锁防护）
        if result.timed_out:
            with self._lock:
                for missing in result.missing:
                    uut = self.uuts.get(missing)
                    if uut is not None and not uut.busy:
                        uut.state = UUTState.FAILED
                        uut.last_error = f"Barrier '{barrier_name}' timeout"

        # 满员放行后清理，使同一屏障名可复用
        with self._lock:
            if self._barriers.get(barrier_name) is barrier and barrier.is_released:
                self._barriers.pop(barrier_name, None)

        return result
