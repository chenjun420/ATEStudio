"""ResourceContentionAnalyzer — lock contention analysis over recorded event streams.

Design doc §7.6 (multi-UUT simulation): ResourceContentionAnalyzer records lock
wait/hold times and contention counts, and produces gantt-chart data plus a
deadlock detection report.

The analyzer is PURE ANALYSIS over an ingested event stream. It never touches
locks itself, does not sample timers, and has no scheduler coupling: feed it the
lock wait/acquire/release events recorded during a run (e.g. ResourceManager
activity logged by the proxy pool / scheduler) and it reconstructs what happened.

Input event schema (canonical dict form)
----------------------------------------
    {"ts": float, "type": str, "resource": str, "owner": str}

- ``ts``       non-decreasing timestamp in seconds (arbitrary epoch).
- ``type``     one of ``"wait"`` (owner began waiting for a held resource),
               ``"acquire"`` (owner was granted the resource), ``"release"``
               (owner released it). Aliases accepted: ``lock_wait`` /
               ``wait_start``, ``lock_acquire`` / ``acquired``,
               ``lock_release`` / ``released``.
- ``resource`` resource identifier (alias key ``resource_id``).
- ``owner``    owner identifier, typically a UUT-scoped step id
               (alias key ``owner_id``).

Violations raise :class:`ValueError` naming the offending event index — no
silent fallback: release-without-acquire, double-acquire, a second ``wait``
while already waiting, timestamps going backwards, unknown event types.

Report schema (returned by :meth:`ResourceContentionAnalyzer.analyze`)
---------------------------------------------------------------------
Frontend-consumable; T36's gantt timeline renders ``gantt`` directly::

    {
      "generated_from": {"events": int, "resources": int, "owners": int},
      "resources": {
        "<resource_id>": {
          "acquire_count": int,
          "release_count": int,
          "contention_count": int,        # owners that had to wait
          "max_concurrent_waiters": int,  # peak simultaneous waiters
          "wait": _interval_stats,
          "hold": _interval_stats,
        }, ...
      },
      "gantt": [                          # sorted by start; timeline bars
        {"resource": str, "owner": str,
         "start": float, "end": float | None,   # end=None ⇒ open at stream end
         "kind": "wait" | "hold"},
        ...
      ],
      "deadlocks": [                      # wait-for cycles, one entry per incident
        {"detected_at_ts": float,         # first ts where the cycle existed
         "cycle_owners": [str, ...],      # rotation-normalized cycle path
         "involved_resources": [str, ...],
         "edges": [{"waiter": str, "waits_for": str, "held_by": str}, ...]},
      ],
      "unresolved_waits": [               # waiting at stream end, cycle-free
        {"owner": str, "resource": str, "since_ts": float},
      ],
    }

where ``_interval_stats`` is::

    {"count": int, "total": float, "min": float, "max": float, "mean": float,
     "histogram": [{"bucket": "[lo, hi)", "count": int}, ...]}

Zero-interval stats report 0 for every numeric field. Histogram buckets default
to sub-millisecond…1s+ edges; pass ``histogram_buckets`` (strictly ascending
floats) to customize — the final bucket always extends to ∞.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ResourceContentionAnalyzer"]

_DEFAULT_BUCKETS: tuple[float, ...] = (
    0.0, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, math.inf,
)

_TYPE_ALIASES: dict[str, str] = {
    "wait": "wait",
    "lock_wait": "wait",
    "wait_start": "wait",
    "acquire": "acquire",
    "lock_acquire": "acquire",
    "acquired": "acquire",
    "release": "release",
    "lock_release": "release",
    "released": "release",
}


def _bucket_label(lo: float, hi: float) -> str:
    lo_s = f"{lo:g}" if lo != 0.0 else "0"
    hi_s = "∞" if math.isinf(hi) else f"{hi:g}"
    return f"[{lo_s}, {hi_s})"


@dataclass
class _ReplayState:
    """Mutable scratch state threaded through the :meth:`analyze` replay."""

    holders: dict[str, str] = field(default_factory=dict)
    open_holds: dict[str, tuple[str, float]] = field(default_factory=dict)
    open_waits: dict[tuple[str, str], float] = field(default_factory=dict)
    concurrent_waiters: dict[str, int] = field(default_factory=dict)
    wait_samples: dict[str, list[float]] = field(default_factory=dict)
    hold_samples: dict[str, list[float]] = field(default_factory=dict)
    acquire_count: dict[str, int] = field(default_factory=dict)
    release_count: dict[str, int] = field(default_factory=dict)
    peak_waiters: dict[str, int] = field(default_factory=dict)
    gantt: list[dict[str, Any]] = field(default_factory=list)
    deadlocks: list[dict[str, Any]] = field(default_factory=list)
    seen_cycles: set[frozenset[str]] = field(default_factory=set)


class ResourceContentionAnalyzer:
    """Pure analyzer over lock wait/acquire/release event streams.

    Example:
        >>> analyzer = ResourceContentionAnalyzer()
        >>> analyzer.ingest([
        ...     {"ts": 0.0, "type": "acquire", "resource": "DMM_CH1", "owner": "uut1"},
        ...     {"ts": 1.5, "type": "release", "resource": "DMM_CH1", "owner": "uut1"},
        ... ])
        >>> report = analyzer.analyze()
        >>> report["resources"]["DMM_CH1"]["hold"]["total"]
        1.5
    """

    def __init__(self, histogram_buckets: Iterable[float] | None = None) -> None:
        """Initialize the analyzer.

        Args:
            histogram_buckets: Strictly ascending bucket edges for wait/hold
                histograms. Defaults to sub-millisecond…1s+ edges. The final
                bucket always extends to infinity.
        """
        buckets = tuple(
            float(b) for b in (histogram_buckets if histogram_buckets is not None else _DEFAULT_BUCKETS)
        )
        if len(buckets) < 2 or any(
            b <= a for a, b in zip(buckets, buckets[1:], strict=False)
        ):
            raise ValueError("histogram_buckets must be ≥2 strictly ascending floats")
        if buckets[-1] != math.inf:
            buckets = (*buckets, math.inf)
        self._buckets = buckets
        self._raw_events: list[Mapping[str, Any]] = []

    # ------------------------------------------------------------------
    # ingestion
    # ------------------------------------------------------------------

    def ingest(self, events: Iterable[Mapping[str, Any]]) -> None:
        """Accumulate events for later :meth:`analyze`.

        Events are stored in arrival order; validation happens during
        :meth:`analyze`, which replays the whole accumulated stream.
        Multiple calls append incrementally.
        """
        self._raw_events.extend(events)

    @classmethod
    def from_events(cls, events: Iterable[Mapping[str, Any]]) -> ResourceContentionAnalyzer:
        """Convenience constructor: build, ingest, and return the analyzer."""
        analyzer = cls()
        analyzer.ingest(events)
        return analyzer

    def top_contended(self, n: int = 5) -> list[tuple[str, int]]:
        """Return the ``n`` most contended resources as ``(resource, count)``.

        Sorted by contention count descending, ties broken by resource name
        ascending. Zero-contention resources are included at the tail.
        """
        report = self.analyze()
        ranked = sorted(
            ((res, stats["contention_count"]) for res, stats in report["resources"].items()),
            key=lambda item: (-item[1], item[0]),
        )
        return ranked[:n]

    # ------------------------------------------------------------------
    # analysis
    # ------------------------------------------------------------------

    def analyze(self) -> dict[str, Any]:
        """Replay the accumulated event stream and build the full report.

        See the module docstring for the exact report schema. Idempotent:
        each call replays from scratch over the accumulated events.
        """
        events = [self._normalize(i, e) for i, e in enumerate(self._raw_events)]
        state = _ReplayState()

        prev_ts = -math.inf
        for idx, (ts, kind, resource, owner) in enumerate(events):
            if ts < prev_ts:
                raise ValueError(
                    f"event #{idx}: ts {ts} goes backwards (previous {prev_ts})"
                    " - lock event streams must be non-decreasing"
                )
            prev_ts = ts

            if kind == "wait":
                self._apply_wait(state, idx, ts, resource, owner)
            elif kind == "acquire":
                self._apply_acquire(state, idx, ts, resource, owner)
            else:  # release
                self._apply_release(state, idx, ts, resource, owner)

            self._detect_deadlocks(state.holders, state.open_waits, ts, state.deadlocks, state.seen_cycles)

        unresolved = self._close_open_intervals(state)
        resources = self._compute_resource_stats(state)
        owners = {owner for _, _, _, owner in events}
        return {
            "generated_from": {
                "events": len(events),
                "resources": len(resources),
                "owners": len(owners),
            },
            "resources": resources,
            "gantt": state.gantt,
            "deadlocks": state.deadlocks,
            "unresolved_waits": unresolved,
        }

    def _apply_wait(
        self, state: _ReplayState, idx: int, ts: float, resource: str, owner: str
    ) -> None:
        """Record that *owner* began waiting for *resource* at *ts*."""
        if (resource, owner) in state.open_waits:
            raise ValueError(
                f"event #{idx}: owner '{owner}' is already waiting for"
                f" '{resource}' - resolve the previous wait first"
            )
        state.open_waits[(resource, owner)] = ts
        state.concurrent_waiters[resource] = state.concurrent_waiters.get(resource, 0) + 1
        state.peak_waiters[resource] = max(
            state.peak_waiters.get(resource, 0), state.concurrent_waiters[resource]
        )

    def _apply_acquire(
        self, state: _ReplayState, idx: int, ts: float, resource: str, owner: str
    ) -> None:
        """Grant *resource* to *owner*; close any matching wait interval."""
        current = state.holders.get(resource)
        if current == owner:
            raise ValueError(f"event #{idx}: owner '{owner}' already holds '{resource}'")
        if current is not None:
            raise ValueError(
                f"event #{idx}: '{resource}' is held by '{current}' but owner"
                f" '{owner}' acquired it - mutual exclusion violated"
            )
        since = state.open_waits.pop((resource, owner), None)
        if since is not None:
            state.concurrent_waiters[resource] -= 1
            state.wait_samples.setdefault(resource, []).append(ts - since)
            state.gantt.append({
                "resource": resource, "owner": owner,
                "start": since, "end": ts, "kind": "wait",
            })
        state.holders[resource] = owner
        state.open_holds[resource] = (owner, ts)
        state.acquire_count[resource] = state.acquire_count.get(resource, 0) + 1

    def _apply_release(
        self, state: _ReplayState, idx: int, ts: float, resource: str, owner: str
    ) -> None:
        """Release *resource* from *owner*; close the hold interval."""
        holder_info = state.open_holds.pop(resource, None)
        if holder_info is None or holder_info[0] != owner:
            actual = state.holders.get(resource)
            raise ValueError(
                f"event #{idx}: cannot release '{resource}' as '{owner}'"
                f" (holder: '{actual}') - release without matching acquire"
            )
        del state.holders[resource]
        state.hold_samples.setdefault(resource, []).append(ts - holder_info[1])
        state.gantt.append({
            "resource": resource, "owner": owner,
            "start": holder_info[1], "end": ts, "kind": "hold",
        })
        state.release_count[resource] = state.release_count.get(resource, 0) + 1

    def _close_open_intervals(self, state: _ReplayState) -> list[dict[str, Any]]:
        """Finalize the gantt timeline and return still-open waits at stream end."""
        unresolved = [
            {"owner": owner, "resource": resource, "since_ts": since}
            for (resource, owner), since in state.open_waits.items()
        ]
        unresolved.sort(key=lambda item: (item["since_ts"], item["owner"]))
        for item in unresolved:
            state.gantt.append({
                "resource": item["resource"], "owner": item["owner"],
                "start": item["since_ts"], "end": None, "kind": "wait",
            })
        for resource, (owner, start) in state.open_holds.items():
            state.gantt.append({
                "resource": resource, "owner": owner,
                "start": start, "end": None, "kind": "hold",
            })
        state.gantt.sort(key=lambda row: row["start"])
        return unresolved

    def _compute_resource_stats(self, state: _ReplayState) -> dict[str, Any]:
        """Aggregate per-resource counts, contention peaks, and interval stats."""
        resources: dict[str, Any] = {}
        all_resources = (
            set(state.acquire_count) | set(state.release_count) | set(state.peak_waiters)
        )
        for resource in all_resources:
            resources[resource] = {
                "acquire_count": state.acquire_count.get(resource, 0),
                "release_count": state.release_count.get(resource, 0),
                "contention_count": len(state.wait_samples.get(resource, [])),
                "max_concurrent_waiters": state.peak_waiters.get(resource, 0),
                "wait": self._interval_stats(state.wait_samples.get(resource, [])),
                "hold": self._interval_stats(state.hold_samples.get(resource, [])),
            }
        return resources

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _normalize(self, idx: int, event: Mapping[str, Any]) -> tuple[float, str, str, str]:
        """Validate one raw event and map aliases to the canonical 4-tuple."""
        if not isinstance(event, Mapping):
            raise ValueError(f"event #{idx}: expected a mapping, got {type(event).__name__}")
        raw_type = _first_of(event, "type", "event_type")
        kind = _TYPE_ALIASES.get(raw_type) if isinstance(raw_type, str) else None
        if kind is None:
            valid = ", ".join(sorted(set(_TYPE_ALIASES)))
            raise ValueError(
                f"event #{idx}: unknown event type {raw_type!r} - valid types: {valid}"
            )
        ts = _first_of(event, "ts", "t", "time")
        resource = _first_of(event, "resource", "resource_id")
        owner = _first_of(event, "owner", "owner_id")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            raise ValueError(f"event #{idx}: 'ts' must be a number, got {ts!r}")
        if not resource or not owner:
            raise ValueError(
                f"event #{idx}: 'resource'/'owner' must be non-empty strings,"
                f" got {resource!r}/{owner!r}"
            )
        return float(ts), kind, str(resource), str(owner)

    def _interval_stats(self, samples: list[float]) -> dict[str, Any]:
        """Summarize interval durations: count/total/min/max/mean + histogram."""
        histogram = [0] * (len(self._buckets) - 1)
        for value in samples:
            index = max(bisect_right(self._buckets, value) - 1, 0)
            histogram[index] += 1
        count = len(samples)
        total = sum(samples)
        return {
            "count": count,
            "total": total,
            "min": min(samples) if samples else 0.0,
            "max": max(samples) if samples else 0.0,
            "mean": total / count if count else 0.0,
            "histogram": [
                {"bucket": _bucket_label(lo, hi), "count": n}
                for lo, hi, n in zip(
                    self._buckets, self._buckets[1:], histogram, strict=False
                )
            ],
        }

    @staticmethod
    def _detect_deadlocks(
        holders: Mapping[str, str],
        open_waits: Mapping[tuple[str, str], float],
        ts: float,
        deadlocks: list[dict[str, Any]],
        seen_cycles: set[frozenset[str]],
    ) -> None:
        """Record any NEW wait-for cycle among currently blocked owners.

        An edge A→B exists when A waits for a resource held by B. Cycles are
        rotation-normalized and deduplicated per incident; an identical cycle
        that dissolves and later re-forms is reported again as a new incident.
        """
        adjacency: dict[str, set[str]] = {}
        edge_detail: dict[tuple[str, str], tuple[str, str]] = {}  # (a, b) -> (res, waiter)
        for (resource, waiter), _since in open_waits.items():
            held_by = holders.get(resource)
            if held_by is None:
                continue
            adjacency.setdefault(waiter, set()).add(held_by)
            edge_detail[(waiter, held_by)] = (resource, waiter)

        for cycle in _find_cycles(adjacency):
            key = frozenset(cycle)
            if key in seen_cycles:
                continue
            seen_cycles.add(key)
            members = set(cycle)
            edges = []
            involved: set[str] = set()
            for node in cycle:
                for nxt in adjacency.get(node, ()):
                    if nxt in members:
                        resource, waiter = edge_detail[(node, nxt)]
                        edges.append({"waiter": waiter, "waits_for": resource, "held_by": nxt})
                        involved.add(resource)
            deadlocks.append({
                "detected_at_ts": ts,
                "cycle_owners": list(cycle),
                "involved_resources": sorted(involved),
                "edges": edges,
            })

        # Forget dissolved cycles so a genuine re-formation reports again.
        live = _find_cycles(adjacency)
        live_keys = {frozenset(c) for c in live}
        seen_cycles &= live_keys


def _find_cycles(adjacency: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """Find simple cycles in a small directed graph (owners → blocking owners).

    Lock wait-for graphs are tiny (≤ a few dozen nodes), so exhaustive DFS from
    each start node is fine. Each cycle is returned once, rotated so its
    lexicographically smallest member comes first.
    """
    cycles: dict[tuple[str, ...], list[str]] = {}
    for start in adjacency:
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        while stack:
            node, path = stack.pop()
            for nxt in adjacency.get(node, ()):
                if nxt == start:
                    rotated = path[:]
                    pivot = rotated.index(min(rotated))
                    normalized = tuple(rotated[pivot:] + rotated[:pivot])
                    cycles.setdefault(normalized, list(normalized))
                elif nxt not in path:
                    stack.append((nxt, [*path, nxt]))
    return list(cycles.values())


def _first_of(event: Mapping[str, Any], *keys: str) -> Any:
    """Return the value of the first key present in *event* (alias tolerance)."""
    for key in keys:
        if key in event:
            return event[key]
    return None
