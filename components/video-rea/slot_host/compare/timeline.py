# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic-host event timeline comparison for retained REA timelines."""

from ._model import Rejected, Timeline, TimelinePolicy
from ._timeline import compare_timeline


def _kinds_of(side):
    if type(side) is not dict or type(side.get("events")) is not list:
        return set()
    return {row["kind"] for row in side["events"]
            if type(row) is dict and type(row.get("kind")) is str}


def _binding_problem(reference, candidate, clock_binding):
    sources = []
    if type(clock_binding) is dict:
        sources.append(clock_binding.get("clock_source"))
    if type(reference) is dict:
        sources.append(reference.get("clock_source"))
    if type(candidate) is dict:
        sources.append(candidate.get("clock_source"))
    if "wall" in sources:
        return "clock_source_unsuitable", "Wall clocks cannot establish event timing."
    if type(clock_binding) is not dict:
        return "missing_clock_binding", "The clock binding is missing."
    for side, name in ((reference, "reference"), (candidate, "candidate")):
        if type(side) is not dict:
            return "timeline_invalid", f"The {name} timeline is malformed."
    source = clock_binding.get("clock_source")
    num = clock_binding.get("time_base_numerator")
    den = clock_binding.get("time_base_denominator")
    if source not in ("pts", "monotonic"):
        return "missing_clock_binding", "The clock binding lacks a supported source."
    if type(num) is not int or type(den) is not int or num <= 0 or den <= 0:
        return "missing_clock_binding", "The clock binding lacks a verified time base."
    if type(clock_binding.get("origin_tick")) is not int or clock_binding["origin_tick"] < 0:
        return "missing_clock_binding", "The clock binding lacks an origin tick."
    for side, name in ((reference, "reference"), (candidate, "candidate")):
        if side.get("clock_source") not in ("pts", "monotonic"):
            return "missing_clock_binding", f"The {name} timeline lacks a supported clock source."
        if side.get("origin") not in ("episode_start", "recording_start", "first_input"):
            return "missing_clock_binding", f"The {name} timeline lacks a supported origin."
        base = side.get("time_base")
        if type(base) is dict and base.get("num") is not None:
            if base.get("num") != num or base.get("den") != den:
                return "missing_clock_binding", f"The {name} time base differs from the binding."
    for side, name in ((reference, "reference"), (candidate, "candidate")):
        if side.get("clock_source") != source:
            return "clock_source_mismatch", f"The {name} clock does not match the binding."
    if reference.get("origin") != candidate.get("origin"):
        return "origin_mismatch", "The timelines use different anchoring rules."
    if reference.get("clock_source") != candidate.get("clock_source"):
        return "clock_source_mismatch", "The clock sources have no verified bridge."
    return None


def _report(finding):
    diagnostics = finding.diagnostics or {}
    report = {"result": finding.outcome, "checks": [finding.receipt_fields()],
              "order_conflicts": list(diagnostics.get("order_conflicts", []))}
    if diagnostics:
        report["coverage"] = {key: diagnostics.get(key) for key in (
            "reference_count", "candidate_count", "reference_complete",
            "candidate_complete", "reference_missing_kinds", "candidate_missing_kinds")
            if key in diagnostics}
        report["events"] = list(diagnostics.get("events", []))
        report["deltas"] = list(diagnostics.get("events", []))
        report["unscoped_events"] = list(diagnostics.get("unscoped_events", []))
    if finding.outcome == "abstain":
        report["abstain_reason"] = finding.reason
        report["abstain_detail"] = finding.detail[:2000]
    return report


def compare(reference, candidate, *, clock_binding, late_tolerance_ms=33):
    """Compare two retained timeline sides in the profile vocabulary."""
    empty_reference = type(reference) is dict and reference.get("events") == []
    if reference is None:
        return {"result": "abstain", "abstain_reason": "missing_reference_coverage",
                "abstain_detail": "No reference timeline was retained.",
                "checks": [{"check_id": "timeline", "kind": "event_timeline",
                            "outcome": "abstain", "detail": "No reference timeline was retained."}],
                "order_conflicts": []}
    if candidate is None:
        checks = [] if empty_reference else [{"check_id": "timeline", "kind": "event_timeline",
                            "outcome": "abstain", "detail": "No candidate timeline was retained."}]
        return {"result": "abstain", "abstain_reason": "coverage_insufficient",
                "abstain_detail": "No candidate timeline was retained.",
                "checks": checks, "order_conflicts": []}
    if type(late_tolerance_ms) is not int or late_tolerance_ms < 0:
        return {"result": "abstain", "abstain_reason": "policy_undeclared",
                "abstain_detail": "The late tolerance is invalid.",
                "checks": [], "order_conflicts": []}
    problem = _binding_problem(reference, candidate, clock_binding)
    if problem is not None:
        reason, detail = problem
        checks = [] if empty_reference else [{"check_id": "timeline", "kind": "event_timeline",
                            "outcome": "abstain", "detail": detail}]
        return {"result": "abstain", "abstain_reason": reason, "abstain_detail": detail,
                "checks": checks, "order_conflicts": []}
    policy = TimelinePolicy(kinds=frozenset(_kinds_of(reference) | _kinds_of(candidate)),
                            required_kinds=frozenset(_kinds_of(reference)),
                            late_tolerance_ms=late_tolerance_ms, integer_milliseconds=False)
    anchor = (f"{clock_binding['clock_source']}:{clock_binding['time_base_numerator']}/"
              f"{clock_binding['time_base_denominator']}:{clock_binding['origin_tick']}")
    ref = Timeline(record=reference, clock_verified=True, complete=True,
                   ordinal_prefix_verified=True, clock_domain=anchor, common_anchor=anchor)
    cand = Timeline(record=candidate, clock_verified=True, complete=True,
                    ordinal_prefix_verified=True, clock_domain=anchor, common_anchor=anchor)
    try:
        finding = compare_timeline(ref, cand, policy)
    except Rejected as error:
        return {"result": "abstain", "abstain_reason": error.reason,
                "abstain_detail": str(error)[:2000], "checks": [], "order_conflicts": []}
    return _report(finding)
