"""ExecutionDiff — structured diff between two recorded executions.

Design doc §7.9.3: ``ExecutionDiff.compare(exec_a, exec_b)`` compares step
results, measurement values (within a configurable tolerance), timing
deviations, resource usage and variables, producing a summary dict that the
frontend compare view (T37) renders side-by-side.

PURE ANALYSIS over event streams. Inputs are plain event-dict lists — exactly
what :meth:`ate_platform.simulation.recording.RecordingInterceptor.load`
returns — or equivalent hand-built dict payloads. Nothing is mutated; no
payloads are deep-copied into the summary (values are referenced as-is; the
recording layer already guarantees JSON-safety via redaction).

Input event schema (canonical dict form)
----------------------------------------
Each event is a mapping. The kind is read from ``kind`` (recording.py) or
``type`` (design-doc style); timestamps from ``t`` or ``ts`` (seconds).

    {"kind": "step_started"|"step_completed"|"step_failed",
     "t": float, "step_id": str, "error": str|None}
    {"kind": "instrument_call", "t": float,
     "resource": str (alias resource_id), "method": str,
     "result": Any, "elapsed_ms": float|None (alias duration_ms)}
    {"kind": "variable_change", "t": float,
     "scope": str, "key": str (alias name), "value": Any (alias new)}
    {"kind": "measurement", "t": float,
     "step_id": str|absent, "name": str, "value": number}

Unknown kinds are ignored (forward compatibility). Repeated ``step_id``s
(loop iterations) collapse to the LAST occurrence for status/duration.

Summary schema (returned by :meth:`ExecutionDiff.compare`)
----------------------------------------------------------
JSON-safe throughout; T37's compare view consumes it directly::

    {
      "match": bool,        # True ⇔ every diff section below is empty
      "meta": {"events_a": int, "events_b": int},
      "steps": {
        "added": [str, ...],                  # step_ids only in B (B order)
        "removed": [str, ...],                # step_ids only in A (A order)
        "status_changed": [
          {"step_id": str, "a": "passed"|"failed"|"running",
           "b": "passed"|"failed"|"running"}, ...
        ],
      },
      "measurements": [                       # ONLY out-of-tolerance pairs
        {"key": str,                          # "<step_id>:<name>" or
                                              # "<resource>.<method>#<occurrence>"
         "a": number|str, "b": number|str,
         "delta": float|null}, ...            # delta=null for non-numeric
      ],
      "timing": {
        "total": {"a_ms": float, "b_ms": float, "delta_ms": float} | null,
                                              # null ⇔ spans identical
        "steps": [                            # common steps, non-zero delta only
          {"step_id": str, "a_ms": float, "b_ms": float, "delta_ms": float}, ...
        ],
      },
      "resources": [                          # call-count diffs only
        {"resource": str, "method": str,
         "a_count": int, "b_count": int}, ...
      ],
      "variables": {
        "changed": [                          # final-value fold per (scope,key)
          {"scope": str, "key": str,
           "old": Any, "new": Any}, ...      # old/new null ⇔ key added/removed
        ],
      },
    }

Measurement matching & tolerance semantics
------------------------------------------
- Explicit ``measurement`` events pair by ``"<step_id>:<name>"`` (or just
  ``name`` when no step context).
- Numeric ``instrument_call`` results additionally pair by occurrence index
  within ``(resource, method)`` — replay drift shows up here.
- Comparison is PURE RELATIVE: values match iff
  ``abs(a - b) <= tolerance * max(abs(a), abs(b))`` (default 1e-9). Two exact
  zeros match; zero vs non-zero never matches. Non-numeric values compare by
  strict equality. Violations land in ``measurements`` with their delta.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = ["ExecutionDiff"]

_DEFAULT_TOLERANCE = 1e-9

_STATUS_PASSED = "passed"
_STATUS_FAILED = "failed"
_STATUS_RUNNING = "running"


def _kind(event: Mapping[str, Any]) -> str:
    """事件种类：兼容 recording.py 的 ``kind`` 与设计文档的 ``type``。"""
    kind = event.get("kind") or event.get("type")
    return kind if isinstance(kind, str) else ""


def _ts(event: Mapping[str, Any]) -> float | None:
    t = event.get("t", event.get("ts"))
    return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class ExecutionDiff:
    """Structured comparison of two execution event streams."""

    @staticmethod
    def compare(
        exec_a: Sequence[Mapping[str, Any]],
        exec_b: Sequence[Mapping[str, Any]],
        *,
        tolerance: float = _DEFAULT_TOLERANCE,
    ) -> dict[str, Any]:
        """Compare two executions and return the T37-consumable summary dict.

        Args:
            exec_a: Baseline execution events (e.g.
                ``RecordingInterceptor.load(path_a)``).
            exec_b: Candidate execution events.
            tolerance: Pure-relative measurement tolerance (default 1e-9).

        Returns:
            Summary dict — schema documented in the module docstring.

        Raises:
            TypeError: If either stream is not a sequence of mappings.
            ValueError: If ``tolerance`` is negative.
        """
        if tolerance < 0:
            raise ValueError(f"tolerance must be >= 0, got {tolerance!r}")
        for name, stream in (("exec_a", exec_a), ("exec_b", exec_b)):
            if not isinstance(stream, Iterable) or isinstance(stream, (str, bytes)):
                raise TypeError(f"{name} must be an iterable of event dicts")
            if any(not isinstance(ev, Mapping) for ev in stream):
                raise TypeError(f"{name} must contain only event dicts")

        steps_a = _fold_steps(exec_a)
        steps_b = _fold_steps(exec_b)
        calls_a = _count_calls(exec_a)
        calls_b = _count_calls(exec_b)
        vars_a = _fold_variables(exec_a)
        vars_b = _fold_variables(exec_b)
        meas_a = _collect_measurements(exec_a)
        meas_b = _collect_measurements(exec_b)

        added = [sid for sid in steps_b if sid not in steps_a]
        removed = [sid for sid in steps_a if sid not in steps_b]
        status_changed = [
            {"step_id": sid, "a": steps_a[sid]["status"], "b": steps_b[sid]["status"]}
            for sid in sorted(set(steps_a) & set(steps_b))
            if steps_a[sid]["status"] != steps_b[sid]["status"]
        ]

        measurements = _diff_measurements(meas_a, meas_b, tolerance)

        span_a = _span_seconds(exec_a)
        span_b = _span_seconds(exec_b)
        total: dict[str, float] | None = None
        if span_a is not None and span_b is not None and span_a != span_b:
            total = {"a_ms": span_a * 1000.0, "b_ms": span_b * 1000.0,
                     "delta_ms": (span_b - span_a) * 1000.0}
        timing_steps = [
            {"step_id": sid,
             "a_ms": steps_a[sid]["duration_ms"], "b_ms": steps_b[sid]["duration_ms"],
             "delta_ms": steps_b[sid]["duration_ms"] - steps_a[sid]["duration_ms"]}
            for sid in sorted(set(steps_a) & set(steps_b))
            if steps_a[sid]["duration_ms"] is not None
            and steps_b[sid]["duration_ms"] is not None
            and steps_a[sid]["duration_ms"] != steps_b[sid]["duration_ms"]
        ]

        resources = [
            {"resource": res, "method": meth, "a_count": calls_a.get((res, meth), 0),
             "b_count": calls_b.get((res, meth), 0)}
            for res, meth in sorted(set(calls_a) | set(calls_b))
            if calls_a.get((res, meth), 0) != calls_b.get((res, meth), 0)
        ]

        changed_vars = [
            {"scope": scope, "key": key, "old": vars_a.get((scope, key)),
             "new": vars_b.get((scope, key))}
            for scope, key in sorted(set(vars_a) | set(vars_b))
            if vars_a.get((scope, key)) != vars_b.get((scope, key))
        ]

        match = not (
            added or removed or status_changed or measurements or total is not None
            or timing_steps or resources or changed_vars
        )
        return {
            "match": match,
            "meta": {"events_a": len(exec_a), "events_b": len(exec_b)},
            "steps": {"added": added, "removed": removed, "status_changed": status_changed},
            "measurements": measurements,
            "timing": {"total": total, "steps": timing_steps},
            "resources": resources,
            "variables": {"changed": changed_vars},
        }


# ---------------------------------------------------------------------------
# 流折叠（纯函数：事件流 → 中间视图）
# ---------------------------------------------------------------------------
def _fold_steps(events: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 step_id 折叠步骤生命周期 → {step_id: {status, duration_ms}}。

    重复 step_id（循环迭代）取最后一次出现；无终态记 running。
    """
    folded: dict[str, dict[str, Any]] = {}
    pending_start: dict[str, float] = {}
    for ev in events:
        kind = _kind(ev)
        step_id = ev.get("step_id")
        if not isinstance(step_id, str):
            continue
        t = _ts(ev)
        if kind == "step_started":
            if t is not None:
                pending_start[step_id] = t
            folded[step_id] = {"status": _STATUS_RUNNING, "duration_ms": None}
        elif kind == "step_completed":
            folded[step_id] = {"status": _STATUS_PASSED, "duration_ms": _duration(pending_start, step_id, t)}
            pending_start.pop(step_id, None)
        elif kind == "step_failed":
            folded[step_id] = {"status": _STATUS_FAILED, "duration_ms": _duration(pending_start, step_id, t)}
            pending_start.pop(step_id, None)
    return folded


def _duration(pending_start: dict[str, float], step_id: str, end_t: float | None) -> float | None:
    start = pending_start.get(step_id)
    if start is None or end_t is None:
        return None
    return (end_t - start) * 1000.0


def _count_calls(events: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], int]:
    """按 (resource, method) 统计 instrument_call 次数。"""
    counts: dict[tuple[str, str], int] = {}
    for ev in events:
        if _kind(ev) != "instrument_call":
            continue
        resource = ev.get("resource", ev.get("resource_id"))
        method = ev.get("method")
        if isinstance(resource, str) and isinstance(method, str):
            counts[(resource, method)] = counts.get((resource, method), 0) + 1
    return counts


def _fold_variables(events: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], Any]:
    """按 (scope, key) 折叠变量变更日志 → 最终值表（后写覆盖）。"""
    final: dict[tuple[str, str], Any] = {}
    for ev in events:
        if _kind(ev) != "variable_change":
            continue
        scope = ev.get("scope", "")
        key = ev.get("key", ev.get("name"))
        if not isinstance(key, str):
            continue
        value = ev.get("value", ev.get("new"))
        final[(scope if isinstance(scope, str) else "", key)] = value
    return final


def _collect_measurements(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """收集测量值：显式 measurement 事件 + instrument_call 数值 result。

    键：显式事件用 ``<step_id>:<name>``（无 step 则 ``name``）；仪器调用数值
    返回值按 (resource, method) 出现序号编 ``<resource>.<method>#<n>``。
    """
    measurements: dict[str, Any] = {}
    occurrences: dict[tuple[str, str], int] = {}
    for ev in events:
        kind = _kind(ev)
        if kind == "measurement":
            name = ev.get("name")
            if not isinstance(name, str):
                continue
            step_id = ev.get("step_id")
            key = f"{step_id}:{name}" if isinstance(step_id, str) else name
            measurements[key] = ev.get("value")
        elif kind == "instrument_call":
            resource = ev.get("resource", ev.get("resource_id"))
            method = ev.get("method")
            result = ev.get("result")
            if isinstance(resource, str) and isinstance(method, str) and _num(result):
                n = occurrences.get((resource, method), 0)
                occurrences[(resource, method)] = n + 1
                measurements[f"{resource}.{method}#{n}"] = result
    return measurements


def _diff_measurements(
    meas_a: dict[str, Any], meas_b: dict[str, Any], tolerance: float
) -> list[dict[str, Any]]:
    """容差外 / 不相等的测量对 → 违规列表（按键排序，delta 数值为 float）。"""
    violations: list[dict[str, Any]] = []
    for key in sorted(set(meas_a) | set(meas_b)):
        a = meas_a.get(key)
        b = meas_b.get(key)
        if _num(a) and _num(b):
            fa, fb = float(a), float(b)
            scale = max(abs(fa), abs(fb))
            if abs(fa - fb) <= tolerance * scale:
                continue
            violations.append({"key": key, "a": a, "b": b, "delta": fb - fa})
        elif a != b:
            violations.append({"key": key, "a": a, "b": b, "delta": None})
    return violations


def _span_seconds(events: Sequence[Mapping[str, Any]]) -> float | None:
    """执行总时长（秒）：最早与最晚事件时间戳之差；无可解析时间戳 → None。"""
    stamps = [t for t in (_ts(ev) for ev in events) if t is not None]
    if not stamps:
        return None
    return max(stamps) - min(stamps)
