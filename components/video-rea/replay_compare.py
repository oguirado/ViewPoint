# SPDX-License-Identifier: AGPL-3.0-only
"""Compare authored synthetic replay events on a shared microsecond timeline."""

from collections import Counter
from itertools import groupby


def _index_events(events, side, *, allow_duplicates=False):
    if type(events) not in (list, tuple):
        raise ValueError(f"{side} must be a list or tuple of events")

    times = {}
    for index, event in enumerate(events):
        location = f"{side}[{index}]"
        if (type(event) is not dict or any(type(key) is not str for key in event)
                or event.keys() != {"event_id", "time_us"}):
            raise ValueError(f"{location} must contain exactly event_id and time_us")

        event_id = event["event_id"]
        if type(event_id) is not str or not event_id or event_id != event_id.strip():
            raise ValueError(f"{location}.event_id must be a nonblank trimmed string")
        if event_id in times and not allow_duplicates:
            raise ValueError(f"{location}.event_id duplicates {event_id!r}")

        time_us = event["time_us"]
        if type(time_us) is not int:
            raise ValueError(f"{location}.time_us must be an integer")
        times.setdefault(event_id, []).append(time_us)
    return times


def _order_conflicts(reference_times, candidate_times):
    matched = sorted((time, event_id) for event_id, time in reference_times.items()
                     if event_id in candidate_times)
    conflicts = []
    latest_earlier = None
    for _, simultaneous in groupby(matched, key=lambda event: event[0]):
        latest_in_group = None
        for _, event_id in simultaneous:
            current = candidate_times[event_id], event_id
            if latest_earlier is not None and current[0] <= latest_earlier[0]:
                conflicts.append({"earlier_reference_event_id": latest_earlier[1],
                                  "later_reference_event_id": event_id})
            if latest_in_group is None or current > latest_in_group:
                latest_in_group = current
        if latest_earlier is None or latest_in_group > latest_earlier:
            latest_earlier = latest_in_group
    return conflicts


def compare_replay_events(reference, candidate, *, late_tolerance_us):
    """Return a fixture comparison report, raising ValueError on malformed input.

    Each event is a dict with a unique authored event_id and a signed integer
    time_us. Both traces must use the same timeline and origin. Input order is
    immaterial and IDs are matched exactly.

    Deltas are candidate minus reference. Any negative delta fails, and late
    deltas pass up to and including the explicit nonnegative tolerance. Missing
    or unexpected IDs fail. Strict precedence in the reference must be preserved.
    Events with equal reference timestamps establish no precedence.
    Duplicate candidate emissions fail. An empty reference is incomplete.
    """
    if type(late_tolerance_us) is not int or late_tolerance_us < 0:
        raise ValueError("late_tolerance_us must be a nonnegative integer")

    reference_times = {event_id: times[0] for event_id, times in _index_events(reference, "reference").items()}
    candidate_occurrences = _index_events(candidate, "candidate", allow_duplicates=True)
    candidate_times = {event_id: times[0] for event_id, times in candidate_occurrences.items()
                       if len(times) == 1}
    events = []
    for event_id in sorted(reference_times.keys() | candidate_occurrences.keys()):
        reference_us = reference_times.get(event_id)
        candidate_us = candidate_times.get(event_id)
        delta_us = None
        occurrences = candidate_occurrences.get(event_id, [])
        if len(occurrences) > 1:
            events.append({
                "event_id": event_id,
                "reference_us": reference_us,
                "candidate_us": None,
                "candidate_occurrences_us": sorted(occurrences),
                "delta_us": None,
                "verdict": "duplicate",
            })
            continue
        if reference_us is None:
            verdict = "unexpected"
        elif candidate_us is None:
            verdict = "missing"
        else:
            delta_us = candidate_us - reference_us
            if delta_us < 0:
                verdict = "early"
            elif delta_us > late_tolerance_us:
                verdict = "late"
            else:
                verdict = "pass"
        events.append({
            "event_id": event_id,
            "reference_us": reference_us,
            "candidate_us": candidate_us,
            "delta_us": delta_us,
            "verdict": verdict,
        })

    matched_count = len(reference_times.keys() & candidate_occurrences.keys())
    counts = Counter(event["verdict"] for event in events)
    reference_count = len(reference_times)
    conflicts = _order_conflicts(reference_times, candidate_times)
    if not reference_count:
        verdict = "incomplete"
    elif not conflicts and counts["pass"] == len(events):
        verdict = "pass"
    else:
        verdict = "fail"
    return {
        "schema": "rea.replay_event_comparison/v1",
        "late_tolerance_us": late_tolerance_us,
        "coverage": {
            "reference_count": reference_count,
            "candidate_count": sum(len(times) for times in candidate_occurrences.values()),
            "timing_pass_count": counts["pass"],
            "missing_count": counts["missing"],
            "unexpected_count": len(candidate_occurrences.keys() - reference_times.keys()),
            "duplicate_count": counts["duplicate"],
            "matched_count": matched_count,
            "reference_fraction": matched_count / reference_count if reference_count else None,
        },
        "events": events,
        "order_conflicts": conflicts,
        "verdict": verdict,
    }
