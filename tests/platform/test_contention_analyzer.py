"""Tests for ResourceContentionAnalyzer (plan v41-gap-analysis T13).

Pure analysis over synthetic lock-event streams — no scheduler, no I/O.
Event schema (canonical): {"ts": float, "type": "wait"|"acquire"|"release",
"resource": str, "owner": str}. Alias keys resource_id/owner_id accepted.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from ate_platform.simulation.contention import ResourceContentionAnalyzer


def ev(ts: float, kind: str, resource: str, owner: str) -> dict[str, Any]:
    return {"ts": ts, "type": kind, "resource": resource, "owner": owner}


# ---------------------------------------------------------------------------
# wait / hold time math
# ---------------------------------------------------------------------------


def test_empty_stream_yields_zeroed_report() -> None:
    report = ResourceContentionAnalyzer().analyze()
    assert report["generated_from"] == {"events": 0, "resources": 0, "owners": 0}
    assert report["resources"] == {}
    assert report["gantt"] == []
    assert report["deadlocks"] == []
    assert report["unresolved_waits"] == []


def test_single_hold_time_math_exact() -> None:
    events = [ev(0.0, "acquire", "DMM_CH1", "uut1"), ev(2.5, "release", "DMM_CH1", "uut1")]
    stats = ResourceContentionAnalyzer.from_events(events).analyze()["resources"]["DMM_CH1"]
    assert stats["acquire_count"] == 1
    assert stats["release_count"] == 1
    assert stats["hold"]["count"] == 1
    assert stats["hold"]["total"] == pytest.approx(2.5)
    assert stats["hold"]["min"] == pytest.approx(2.5)
    assert stats["hold"]["max"] == pytest.approx(2.5)
    assert stats["hold"]["mean"] == pytest.approx(2.5)


def test_wait_time_math_exact() -> None:
    # uut2 starts waiting at t=1.0 while uut1 holds; granted at t=3.0 → wait 2.0
    events = [
        ev(0.0, "acquire", "PSU_MAIN", "uut1"),
        ev(1.0, "wait", "PSU_MAIN", "uut2"),
        ev(3.0, "release", "PSU_MAIN", "uut1"),
        ev(3.0, "acquire", "PSU_MAIN", "uut2"),
        ev(4.0, "release", "PSU_MAIN", "uut2"),
    ]
    stats = ResourceContentionAnalyzer.from_events(events).analyze()["resources"]["PSU_MAIN"]
    assert stats["wait"]["count"] == 1
    assert stats["wait"]["total"] == pytest.approx(2.0)
    assert stats["wait"]["max"] == pytest.approx(2.0)
    assert stats["hold"]["count"] == 2  # uut1 (3.0) + uut2 (1.0)
    assert stats["hold"]["total"] == pytest.approx(4.0)


def test_no_wait_fast_path_zero_contention() -> None:
    events = [
        ev(0.0, "acquire", "R", "a"),
        ev(0.1, "release", "R", "a"),
        ev(0.2, "acquire", "R", "b"),
        ev(0.3, "release", "R", "b"),
    ]
    stats = ResourceContentionAnalyzer.from_events(events).analyze()["resources"]["R"]
    assert stats["contention_count"] == 0
    assert stats["wait"]["count"] == 0
    assert stats["max_concurrent_waiters"] == 0


def test_wait_and_hold_min_max_mean_over_multiple_intervals() -> None:
    events = [
        # R held by a twice: holds of 1.0 and 3.0 → mean 2.0
        ev(0.0, "acquire", "R", "a"),
        ev(1.0, "release", "R", "a"),
        # b waits 0.5 then holds 3.0
        ev(1.5, "wait", "R", "b"),
        ev(2.0, "acquire", "R", "b"),
        ev(5.0, "release", "R", "b"),
        # c waits 2.0 then holds 1.0
        ev(6.0, "wait", "R", "c"),
        ev(8.0, "acquire", "R", "c"),
        ev(9.0, "release", "R", "c"),
    ]
    stats = ResourceContentionAnalyzer.from_events(events).analyze()["resources"]["R"]
    assert stats["wait"]["min"] == pytest.approx(0.5)
    assert stats["wait"]["max"] == pytest.approx(2.0)
    assert stats["wait"]["mean"] == pytest.approx(1.25)
    assert stats["hold"]["min"] == pytest.approx(1.0)
    assert stats["hold"]["max"] == pytest.approx(3.0)
    assert stats["hold"]["mean"] == pytest.approx(5.0 / 3.0)  # holds: 1.0 + 3.0 + 1.0


# ---------------------------------------------------------------------------
# contention counting + top-N sort
# ---------------------------------------------------------------------------


def test_contention_count_counts_each_waiter() -> None:
    events = [
        ev(0.0, "acquire", "R", "a"),
        ev(0.1, "wait", "R", "b"),
        ev(0.2, "wait", "R", "c"),  # two concurrent waiters
        ev(1.0, "release", "R", "a"),
        ev(1.0, "acquire", "R", "b"),
        ev(2.0, "release", "R", "b"),
        ev(2.0, "acquire", "R", "c"),
        ev(3.0, "release", "R", "c"),
    ]
    stats = ResourceContentionAnalyzer.from_events(events).analyze()["resources"]["R"]
    assert stats["contention_count"] == 2
    assert stats["max_concurrent_waiters"] == 2


def test_max_concurrent_waiters_tracks_peak_not_sum() -> None:
    events = [
        ev(0.0, "acquire", "R", "a"),
        ev(0.1, "wait", "R", "b"),
        ev(0.2, "wait", "R", "c"),  # peak: 2 concurrent waiters
        ev(0.4, "release", "R", "a"),
        ev(0.4, "acquire", "R", "b"),  # b granted → 1 waiter left
        ev(0.9, "release", "R", "b"),
        ev(0.9, "acquire", "R", "c"),
        ev(1.5, "release", "R", "c"),
    ]
    stats = ResourceContentionAnalyzer.from_events(events).analyze()["resources"]["R"]
    assert stats["max_concurrent_waiters"] == 2


def test_top_contended_sort_desc_then_name() -> None:
    events: list[dict[str, Any]] = []
    # R_hot: 3 contentions; R_mid: 2; R_cold: 0; tie broken by name (A_tie before B_tie)
    # One global clock: the analyzer enforces non-decreasing ts across the stream.
    t = 0.0
    for res, n in (("R_hot", 3), ("R_mid", 2), ("A_tie", 1), ("B_tie", 1)):
        events.append(ev(t, "acquire", res, "holder"))
        for i in range(n):
            t += 0.1
            events.append(ev(t, "wait", res, f"w{i}"))
        t += 1.0
        events.append(ev(t, "release", res, "holder"))
        for j in range(n):
            events.append(ev(t, "acquire", res, f"w{j}"))
            t += 0.1
            events.append(ev(t, "release", res, f"w{j}"))
    # R_cold: used once, nobody ever waited for it
    events.append(ev(t, "acquire", "R_cold", "solo"))
    t += 0.1
    events.append(ev(t, "release", "R_cold", "solo"))
    analyzer = ResourceContentionAnalyzer.from_events(events)
    top = analyzer.top_contended(3)
    assert top[0] == ("R_hot", 3)
    assert top[1] == ("R_mid", 2)
    assert top[2] == ("A_tie", 1)  # name-ascending tie-break
    assert len(top) == 3
    full = analyzer.top_contended(10)
    assert full[-1] == ("R_cold", 0)


# ---------------------------------------------------------------------------
# histograms
# ---------------------------------------------------------------------------


def test_histogram_buckets_place_waits_correctly() -> None:
    events = [
        ev(0.0, "acquire", "R", "a"),
        ev(0.0005, "wait", "R", "b"),  # granted at 0.001 → wait 0.0005 → bucket [0, 0.001)
        ev(0.001, "release", "R", "a"),
        ev(0.001, "acquire", "R", "b"),
        ev(0.002, "wait", "R", "c"),  # granted at 0.2 → wait 0.198 → bucket [0.05, 0.1)? no → [0.1, 0.5)
        ev(0.2, "release", "R", "b"),
        ev(0.2, "acquire", "R", "c"),
        ev(0.3, "release", "R", "c"),
    ]
    buckets = ResourceContentionAnalyzer.from_events(events).analyze()["resources"]["R"][
        "wait"
    ]["histogram"]
    by_label = {b["bucket"]: b["count"] for b in buckets}
    assert by_label["[0, 0.001)"] == 1
    assert sum(by_label.values()) == 2
    big = [lbl for lbl, cnt in by_label.items() if cnt == 1 and lbl != "[0, 0.001)"]
    assert big == ["[0.1, 0.5)"]


def test_custom_histogram_buckets_respected() -> None:
    events = [
        ev(0.0, "acquire", "R", "a"),
        ev(0.5, "wait", "R", "b"),
        ev(1.5, "release", "R", "a"),
        ev(1.5, "acquire", "R", "b"),  # wait 1.0
        ev(2.0, "release", "R", "b"),
    ]
    analyzer = ResourceContentionAnalyzer(histogram_buckets=[0.0, 0.5, math.inf])
    analyzer.ingest(events)
    buckets = analyzer.analyze()["resources"]["R"]["wait"]["histogram"]
    assert [b["bucket"] for b in buckets] == ["[0, 0.5)", "[0.5, ∞)"]
    assert [b["count"] for b in buckets] == [0, 1]


# ---------------------------------------------------------------------------
# gantt data rows (frontend contract — see module docstring schema)
# ---------------------------------------------------------------------------


def test_gantt_rows_wait_and_hold_sorted_by_start() -> None:
    events = [
        ev(0.0, "acquire", "R", "a"),
        ev(0.5, "wait", "R", "b"),
        ev(1.5, "release", "R", "a"),
        ev(1.5, "acquire", "R", "b"),
        ev(2.5, "release", "R", "b"),
    ]
    gantt = ResourceContentionAnalyzer.from_events(events).analyze()["gantt"]
    kinds = [(row["start"], row["kind"], row["owner"]) for row in gantt]
    assert kinds == [
        (0.0, "hold", "a"),
        (0.5, "wait", "b"),
        (1.5, "hold", "b"),
    ]
    row = gantt[1]
    assert set(row) == {"resource", "owner", "start", "end", "kind"}
    assert row["resource"] == "R"
    assert row["end"] == pytest.approx(1.5)


def test_gantt_open_ended_hold_has_null_end() -> None:
    events = [ev(0.0, "acquire", "R", "a")]
    gantt = ResourceContentionAnalyzer.from_events(events).analyze()["gantt"]
    assert gantt == [{"resource": "R", "owner": "a", "start": 0.0, "end": None, "kind": "hold"}]


# ---------------------------------------------------------------------------
# deadlock detection (wait-for cycle)
# ---------------------------------------------------------------------------


def test_cross_lock_deadlock_cycle_detected_with_resources() -> None:
    # Classic ABBA: uut1 holds R1 wants R2; uut2 holds R2 wants R1.
    events = [
        ev(0.0, "acquire", "R1", "uut1"),
        ev(0.1, "acquire", "R2", "uut2"),
        ev(0.2, "wait", "R2", "uut1"),
        ev(0.3, "wait", "R1", "uut2"),
    ]
    report = ResourceContentionAnalyzer.from_events(events).analyze()
    assert len(report["deadlocks"]) == 1
    d = report["deadlocks"][0]
    assert sorted(d["cycle_owners"]) == ["uut1", "uut2"]
    assert sorted(d["involved_resources"]) == ["R1", "R2"]
    assert d["detected_at_ts"] == pytest.approx(0.3)  # cycle completes when uut2 waits
    edges = {(e["waiter"], e["waits_for"], e["held_by"]) for e in d["edges"]}
    assert edges == {("uut1", "R2", "uut2"), ("uut2", "R1", "uut1")}


def test_happy_four_uut_shared_instrument_no_deadlock() -> None:
    # QA happy scenario: 4 UUTs serialize on one shared instrument — no cycle.
    # uut0 arrives first and finds the instrument free (no wait event).
    events: list[dict[str, Any]] = [ev(0.0, "acquire", "ELOAD_MAIN", "uut0")]
    events.append(ev(5.0, "release", "ELOAD_MAIN", "uut0"))
    for i in range(1, 4):
        owner = f"uut{i}"
        events.append(ev(i * 10.0, "wait", "ELOAD_MAIN", owner))
        events.append(ev(i * 10.0 + 1.0, "acquire", "ELOAD_MAIN", owner))
        events.append(ev(i * 10.0 + 5.0, "release", "ELOAD_MAIN", owner))
    report = ResourceContentionAnalyzer.from_events(events).analyze()
    assert report["deadlocks"] == []
    assert report["unresolved_waits"] == []
    stats = report["resources"]["ELOAD_MAIN"]
    assert stats["contention_count"] == 3  # uut1..uut3 waited; uut0 granted instantly
    assert len([r for r in report["gantt"] if r["kind"] == "hold"]) == 4


def test_unresolved_wait_without_cycle_reported() -> None:
    # b waits forever but holder never blocks on anything → starvation, not deadlock.
    events = [
        ev(0.0, "acquire", "R", "a"),
        ev(1.0, "wait", "R", "b"),
    ]
    report = ResourceContentionAnalyzer.from_events(events).analyze()
    assert report["deadlocks"] == []
    assert report["unresolved_waits"] == [{"owner": "b", "resource": "R", "since_ts": 1.0}]


def test_deadlock_cycle_deduped_across_repeated_checks() -> None:
    # Cycle forms at 0.3 and persists to stream end — reported exactly once.
    events = [
        ev(0.0, "acquire", "R1", "uut1"),
        ev(0.1, "acquire", "R2", "uut2"),
        ev(0.2, "wait", "R2", "uut1"),
        ev(0.3, "wait", "R1", "uut2"),
        ev(9.9, "release", "R1", "uut1"),  # resolution after long stall
        ev(9.9, "acquire", "R1", "uut2"),
        ev(10.0, "release", "R2", "uut2"),
        ev(10.0, "acquire", "R2", "uut1"),
        ev(10.1, "release", "R1", "uut2"),
        ev(10.2, "release", "R2", "uut1"),
    ]
    report = ResourceContentionAnalyzer.from_events(events).analyze()
    assert len(report["deadlocks"]) == 1
    assert report["unresolved_waits"] == []


# ---------------------------------------------------------------------------
# input tolerance + validation errors (no silent fallback)
# ---------------------------------------------------------------------------


def test_resource_id_owner_id_alias_keys_accepted() -> None:
    events = [
        {"ts": 0.0, "type": "acquire", "resource_id": "R", "owner_id": "a"},
        {"ts": 1.0, "type": "release", "resource_id": "R", "owner_id": "a"},
    ]
    stats = ResourceContentionAnalyzer.from_events(events).analyze()["resources"]["R"]
    assert stats["hold"]["total"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    "events",
    [
        [ev(0.0, "release", "R", "a")],  # release without acquire
        [ev(0.0, "acquire", "R", "a"), ev(1.0, "acquire", "R", "a")],  # double acquire
        [ev(1.0, "acquire", "R", "a"), ev(0.5, "release", "R", "a")],  # ts goes backwards
        [ev(0.0, "teleport", "R", "a")],  # unknown event type
    ],
)
def test_invalid_streams_raise_value_error(events: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError, match="event"):
        ResourceContentionAnalyzer.from_events(events).analyze()


def test_ingest_is_incremental_across_calls() -> None:
    analyzer = ResourceContentionAnalyzer()
    analyzer.ingest([ev(0.0, "acquire", "R", "a")])
    analyzer.ingest([ev(1.0, "release", "R", "a")])
    stats = analyzer.analyze()["resources"]["R"]
    assert stats["hold"]["total"] == pytest.approx(1.0)
