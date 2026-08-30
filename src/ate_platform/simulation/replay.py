"""ReplayEngine —— 录制回放（严格模式校验）。

设计文档 §7.9 record/replay（AC-7：record→replay 偏差 <1%）。本模块是
:mod:`ate_platform.simulation.recording` 的只读消费者：加载 JSONL 录制，
按 ``(resource, method)`` 出现顺序建立严格索引，回放时逐次弹出下一条
录制调用并校验实参——任何偏离（实参不匹配 / 全局顺序违反 / 索引耗尽）
在严格模式下抛 :class:`ReplayMismatchError`，非严格模式下 warn-once 后
继续尽力回放。

索引与判定语义
--------------
- 索引键 ``(resource, method)`` → 该对调用按全局 ``seq`` 升序的队列；
  ``serve()`` 每次弹出队首（即该对的第 N 次出现，N=已消费计数）。
- **顺序违反**：录制事件携带全局 ``seq``；若本次弹出的调用 ``seq`` 小于
  已消费的最大 ``seq``，说明实况的资源交错顺序与录制不同 ⇒ 偏差。
  同资源同方法的重复调用天然按出现顺序弹出，不受此检查影响。
- **kwargs 子集规则**：录制侧的每个键必须出现在实况 kwargs 中且值相等；
  实况多出的键放行（前向兼容新增可选参数）。
- **脱敏感知**：录制值为 ``"[REDACTED]"``（RecordingInterceptor 脱敏产物）
  的字段无法比对，跳过校验而非误报。
- **浮点容差**：数值用 :func:`math.isclose`（rel/abs 容差均为
  ``float_tolerance``，默认 1e-9），递归作用于嵌套 list/dict/tuple。
- **忠实重放失败**：录制中 ``error`` 非空的调用在回放时抛
  :class:`ReplayRecordedError`（携带原始错误串）——这与"严格性"无关，
  是回放保真的一部分。

耗时/时间戳一律取自录制（``elapsed_ms`` / ``t``），回放路径零真实 I/O。
录制文件只读不写（不可变约束由测试保证）。
"""

from __future__ import annotations

import math
import warnings
from collections import deque
from pathlib import Path
from typing import Any

from ate_platform.simulation.recording import RecordingInterceptor

__all__ = ["ReplayEngine", "ReplayMismatchError", "ReplayRecordedError"]

#: RecordingInterceptor 的脱敏占位值——命中即跳过该字段比对。
_REDACTED = "[REDACTED]"

_INSTRUMENT_CALL = "instrument_call"


class ReplayMismatchError(Exception):
    """回放偏差：实参不匹配 / 全局顺序违反 / 索引耗尽。

    Attributes:
        reason: ``"args_mismatch" | "kwargs_mismatch" | "order_violation" |
            "exhausted"``。
        resource: 资源标识。
        method: 方法名。
        call_index: 该 (resource, method) 对内的出现序号（0 起）。
        seq: 录制全局序号（耗尽且从未录过该对时为 None）。
        expected: 期望值（args 元素 / kwargs 值 / 录制调用摘要）。
        actual: 实况值。
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        resource: str,
        method: str,
        call_index: int,
        seq: int | None = None,
        expected: Any = None,
        actual: Any = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.resource = resource
        self.method = method
        self.call_index = call_index
        self.seq = seq
        self.expected = expected
        self.actual = actual


class ReplayRecordedError(Exception):
    """录制中的仪器调用本身失败——忠实回放时重放该失败。

    Attributes:
        original_error: 录制的原始错误串。
    """

    def __init__(
        self,
        message: str,
        *,
        resource: str,
        method: str,
        call_index: int,
        original_error: str,
    ) -> None:
        super().__init__(message)
        self.resource = resource
        self.method = method
        self.call_index = call_index
        self.original_error = original_error


def _values_equal(expected: Any, actual: Any, tol: float) -> bool:
    """容差感知的递归相等比较（数值走 isclose，容器逐元素）。"""
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(expected, actual, rel_tol=tol, abs_tol=tol)
    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        return len(expected) == len(actual) and all(
            _values_equal(e, a, tol) for e, a in zip(expected, actual, strict=False)
        )
    if isinstance(expected, dict) and isinstance(actual, dict):
        return bool(
            expected.keys() == actual.keys()
            and all(_values_equal(v, actual[k], tol) for k, v in expected.items())
        )
    return bool(expected == actual)


class ReplayEngine:
    """从 RecordingInterceptor JSONL 录制确定性回放仪器调用结果。

    Attributes:
        strict: True=偏差抛 :class:`ReplayMismatchError`；False=warn-once 后
            尽力继续（仍返回录制结果）。
        float_tolerance: 数值比较容差（rel 与 abs 同值）。
        last_served: 最近一次成功弹出的完整录制事件字典（含 ``result`` /
            ``elapsed_ms`` / ``t`` / ``execution_id``）；尚未消费时为 None。
        execution_id: 录制会话键（从文件头读取；events 直构时为 None）。
    """

    def __init__(
        self,
        recording_path: str | Path | None = None,
        *,
        events: list[dict[str, Any]] | None = None,
        strict: bool = True,
        float_tolerance: float = 1e-9,
    ) -> None:
        """初始化回放引擎。

        Args:
            recording_path: JSONL 录制路径（经 :meth:`RecordingInterceptor.load`
                容错读取）；与 ``events`` 二选一。
            events: 已加载的事件列表（跳过非 instrument_call 种类）。
            strict: 严格模式开关（默认 True；严格模式绝不跳过校验）。
            float_tolerance: 浮点比较容差。

        Raises:
            ValueError: ``recording_path`` 与 ``events`` 未恰好提供一个。
        """
        if (recording_path is None) == (events is None):
            raise ValueError("provide exactly one of recording_path / events")

        if events is None:
            # XOR check above guarantees recording_path is set here.
            assert recording_path is not None
            events = RecordingInterceptor.load(recording_path)
            self.execution_id: str | None = RecordingInterceptor.read_header(
                recording_path
            ).get("execution_id")
        else:
            self.execution_id = None

        calls = sorted(
            (e for e in events if e.get("kind") == _INSTRUMENT_CALL),
            key=lambda e: e["seq"],
        )
        self._index: dict[tuple[str, str], deque[dict[str, Any]]] = {}
        for ev in calls:
            self._index.setdefault((ev["resource"], ev["method"]), deque()).append(ev)

        self.strict = bool(strict)
        self.float_tolerance = float(float_tolerance)
        self.last_served: dict[str, Any] | None = None

        self._consumed: dict[tuple[str, str], int] = {}
        self._max_consumed_seq = -1
        self._total_calls = len(calls)
        self._consumed_total = 0
        self._warned = False

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def total_calls(self) -> int:
        """录制中的 instrument_call 总数。"""
        return self._total_calls

    @property
    def consumed_count(self) -> int:
        """已成功回放的调用数。"""
        return self._consumed_total

    @property
    def all_consumed(self) -> bool:
        """所有录制调用是否已全部消费（happy 回放的完成判据）。"""
        return self._consumed_total == self._total_calls and not any(self._index.values())

    def pending(self, resource: str, method: str) -> int:
        """该 (resource, method) 对剩余未消费的录制调用数。"""
        return len(self._index.get((resource, method), ()))

    # ------------------------------------------------------------------
    # 回放
    # ------------------------------------------------------------------
    def serve(
        self,
        resource: str,
        method: str,
        args: list[Any] | tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """回放一次仪器调用：校验实参后返回录制结果。

        Args:
            resource: 资源标识（如 ``"PSU_MAIN"``）。
            method: 方法名（如 ``"query"``）。
            args: 实况位置参数。
            kwargs: 实况关键字参数。

        Returns:
            录制的 ``result`` 字段值。

        Raises:
            ReplayMismatchError: 严格模式下任何偏差（含索引耗尽、顺序违反）。
            ReplayRecordedError: 录制中的该次调用本身失败（忠实重放）。
        """
        args = list(args)
        kwargs = dict(kwargs or {})
        key = (resource, method)
        call_index = self._consumed.get(key, 0)
        queue = self._index.get(key)

        if not queue:
            if key not in self._index:
                detail = "pair was never recorded"
            else:
                detail = f"recorded {self._consumed[key]} call(s), all consumed"
            err = ReplayMismatchError(
                f"Replay exhausted: {resource}.{method} call #{call_index} — {detail}",
                reason="exhausted",
                resource=resource,
                method=method,
                call_index=call_index,
            )
            self._deviate(err)
            return None

        recorded = queue[0]
        seq = recorded["seq"]

        if seq < self._max_consumed_seq:
            err = ReplayMismatchError(
                f"Replay order violation: {resource}.{method} call #{call_index} "
                f"(seq={seq}) was recorded BEFORE an already-consumed call "
                f"(max consumed seq={self._max_consumed_seq}) — live interleaving "
                f"differs from recording",
                reason="order_violation",
                resource=resource,
                method=method,
                call_index=call_index,
                seq=seq,
                expected=f"seq >= {self._max_consumed_seq + 1}",
                actual=f"seq == {seq}",
            )
            self._deviate(err)

        mismatch = self._validate_args(recorded, args, kwargs)
        if mismatch is not None:
            reason, message, expected, actual = mismatch
            err = ReplayMismatchError(
                f"Replay mismatch ({reason}): {resource}.{method} call #{call_index} "
                f"(seq={seq}): {message}",
                reason=reason,
                resource=resource,
                method=method,
                call_index=call_index,
                seq=seq,
                expected=expected,
                actual=actual,
            )
            self._deviate(err)

        queue.popleft()
        self._consumed[key] = call_index + 1
        self._max_consumed_seq = max(self._max_consumed_seq, seq)
        self._consumed_total += 1
        self.last_served = recorded

        error = recorded.get("error")
        if error is not None:
            raise ReplayRecordedError(
                f"Recorded failure replayed: {resource}.{method} call #{call_index} "
                f"(seq={seq}) failed during recording: {error}",
                resource=resource,
                method=method,
                call_index=call_index,
                original_error=str(error),
            )
        return recorded.get("result")

    # ------------------------------------------------------------------
    # 校验与偏差处置
    # ------------------------------------------------------------------
    def _validate_args(
        self, recorded: dict[str, Any], args: list[Any], kwargs: dict[str, Any]
    ) -> tuple[str, str, Any, Any] | None:
        """实参校验；返回 ``(reason, message, expected, actual)`` 或 None。

        规则：位置参数数量+逐元素容差比较；录制 kwargs 键必须全部出现且
        值容差相等（子集规则，实况多余键放行）；``[REDACTED]`` 字段跳过。
        """
        exp_args = recorded.get("args") or []
        if len(exp_args) != len(args):
            return (
                "args_mismatch",
                f"positional arity {len(exp_args)} != {len(args)}",
                exp_args,
                args,
            )
        for i, (e, a) in enumerate(zip(exp_args, args, strict=False)):
            if not _values_equal(e, a, self.float_tolerance):
                return ("args_mismatch", f"arg[{i}] expected {e!r}, got {a!r}", e, a)

        exp_kwargs = recorded.get("kwargs") or {}
        for k, e in exp_kwargs.items():
            if k not in kwargs:
                return (
                    "kwargs_mismatch",
                    f"missing kwarg {k!r} (recorded {e!r})",
                    {k: e},
                    kwargs,
                )
            if e == _REDACTED or str(e) == _REDACTED:
                continue  # 脱敏字段无法比对：跳过而非误报
            if not _values_equal(e, kwargs[k], self.float_tolerance):
                return (
                    "kwargs_mismatch",
                    f"kwarg {k!r} expected {e!r}, got {kwargs[k]!r}",
                    e,
                    kwargs[k],
                )
        return None

    def _deviate(self, err: ReplayMismatchError) -> None:
        """偏差处置：严格抛出；非严格 warn-once 后继续。"""
        if self.strict:
            raise err
        if not self._warned:
            self._warned = True
            warnings.warn(
                f"ReplayEngine(non-strict): {err}",
                stacklevel=4,
            )
