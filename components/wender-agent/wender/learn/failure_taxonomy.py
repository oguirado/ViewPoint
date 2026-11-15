# @module {"id":"wa.learn.failure_taxonomy","layer":2,"owner":"LEARN","deps":[],"book":"HB-WA-s4-learn"}
"""Closed T2/harness failures and independently declared offline episode evidence.

No capture/actuation backend, wall clock, process, or mutable frame is imported.
Adapters can inject a source-specific structural outcome validator; this owner
never elevates adapter errors into authoritative T2 engine rejection reasons.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

T2_REASONS = frozenset((
    "SCHEMA TICK_WINDOW DUPLICATE_SEQ SEQUENCE ACTION_UNAVAILABLE ILLEGAL_RULE "
    "ILLEGAL_CHANNEL INSUFFICIENT_FUNDS CREDIT_LIMIT OVERFLOW REGULATION "
    "OWNERSHIP_CYCLE OWNER_RESERVED PARAMETER_MISSING NEEDS_CONFIRM "
    "STALE_CONFIRM TARGET_MISMATCH TERMS_MISMATCH USED_CONFIRM "
    "CONSENT_MAIN_SAVE TECH_LOCKED NO_EARLY_BENEFIT"
).split())
HARNESS_CODES = frozenset((
    "CAPTURE/DIGEST_MISMATCH CAPTURE/FORMAT_UNDECLARED CAPTURE/GEOMETRY_MISMATCH "
    "CAPTURE/TICK_UNBOUND CAPTURE/STALE_OR_SKIPPED CAPTURE/TELEMETRY_AUSENTE "
    "CAPTURE/UNOBSERVED CAPTURE/UNSUPPORTED CAPTURE/TORN_OR_LAPPED "
    "SELECTOR/MISS SELECTOR/AMBIGUOUS SELECTOR/STALE "
    "ADAPTER/T2_PATH_GRAMMAR ADAPTER/T2_OUTCOME_ORDER_FIELD "
    "ADAPTER/T2_CURRENT_BOUNDARY_SET ADAPTER/T2_LOCAL_SEQ_BURN "
    "TRACE/IDENTITY_MISMATCH TRACE/MISSING_OUTCOME TRACE/CONFLICT TRACE/SCHEMA "
    "INFRA/CONFIG_REQUIRED INFRA/STOPPED INFRA/TIMEOUT INFRA/QUARANTINED "
    "INFRA/CRASH REPLAY/UNSUPPORTED_BRANCH REPLAY/DIGEST_MISMATCH "
    "EVIDENCE/AUSENTE EVIDENCE/UNMAPPED"
).split())
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
OUTCOME_FIELDS = frozenset({"seq", "requested_tick", "applied_tick", "order_seq_id",
                            "status", "reason", "state_sha256"})
CDP_FIELDS = frozenset({"request_id", "trace_id", "status", "reason"})
POLICIES = frozenset({"godot_t2", "cdp"})
COHORTS = frozenset({"eligible", "excluded", "unsupported"})
_METRICS = ("completion", "exactness", "trail", "outcome")


class EvidenceError(ValueError):
    """AUSENTE: missing or malformed independently required declarations."""


@dataclass(frozen=True)
class Failure:
    code: str
    plane: str
    mapped: bool
    digest: str | None = None


@dataclass(frozen=True)
class EpisodeEvidence:
    episode_id: str
    source_policy: str
    outcome: Mapping | None
    postconditions: Mapping
    exactness: bool | None
    trail: bool | None
    emitted: bool
    local_refusal: bool
    frame_rows: int = 0
    faults: tuple = ()  # (plane, code) pairs; never raw receipts or frames


@dataclass(frozen=True)
class EpisodeVerdict:
    eligible: int
    completed: int
    refused: int
    incomplete: int
    excluded: int
    failures: dict
    metrics: dict
    reward: None
    reward_status: str
    confidence: dict
    passed: bool


__all__ = ("T2_REASONS", "HARNESS_CODES", "Failure", "EpisodeEvidence",
           "EpisodeVerdict", "EvidenceError", "classify_failure", "audit_episodes")


def _digest(value: object) -> str:
    """Only a fixed-size digest is retained from an uncontracted value."""
    if not isinstance(value, str):
        value = type(value).__name__
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def classify_failure(plane: str, reason: str | None, *, status: str | None = None) -> Failure | None:
    """Classify exact, case-sensitive engine reasons or disjoint harness codes.

    With status='accepted', a null reason means no failure; any reason is an
    unmapped protocol violation. 'proposed' does not attest a rejection.
    Unknown input is never returned verbatim, only a bounded digest; it fails.
    """
    if plane == "engine" and status == "accepted" and reason is None:
        return None
    if reason is None:
        return Failure("EVIDENCE/AUSENTE", "evidence", False)
    if plane == "engine" and (status is None or status == "rejected") and type(reason) is str and reason in T2_REASONS:
        return Failure("T2/" + reason, "engine", True)
    if plane == "harness" and status is None and type(reason) is str and reason in HARNESS_CODES:
        return Failure(reason, "harness", reason != "EVIDENCE/UNMAPPED")
    return Failure("EVIDENCE/UNMAPPED", "evidence", False, _digest(reason))


def _integer(value, name, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise EvidenceError("AUSENTE: invalid " + name)
    return value


def _require_dict(obj, name):
    if not isinstance(obj, Mapping):
        raise EvidenceError("AUSENTE: " + name)
    return obj


def _required_fraction(config, name):
    value = _require_dict(config.get(name), "thresholds." + name)
    if set(value) != {"numerator", "denominator"}:
        raise EvidenceError("AUSENTE: threshold fields " + name)
    n = _integer(value["numerator"], "threshold numerator")
    d = _integer(value["denominator"], "threshold denominator", positive=True)
    if n > d:
        raise EvidenceError("AUSENTE: impossible threshold " + name)
    return n, d


def _outcome_valid(outcome, policy):
    """Use explicit source policies: CDP is not a T2 engine stream."""
    if not isinstance(outcome, Mapping):
        return False
    if policy == "godot_t2":
        if set(outcome) != OUTCOME_FIELDS:
            return False
        if any(type(outcome[k]) is not int or outcome[k] < (1 if k in ("seq", "order_seq_id") else 0)
               for k in ("seq", "requested_tick", "applied_tick", "order_seq_id")):
            return False
        if not isinstance(outcome["state_sha256"], str) or not HEX64.fullmatch(outcome["state_sha256"]):
            return False
    elif policy == "cdp":
        if set(outcome) != CDP_FIELDS or not all(type(outcome[k]) is str and outcome[k]
                                                for k in ("request_id", "trace_id")):
            return False
    else:
        return False
    return (outcome.get("status") in ("accepted", "rejected", "proposed")
            and (outcome.get("reason") is None or
                 (type(outcome.get("reason")) is str and bool(outcome["reason"]))))


def _ratio(good, observed, declared, threshold):
    """Missing/zero denominators are UNKNOWN, never an observed zero or 100%."""
    if type(declared) is not int or declared <= 0 or observed != declared:
        return {"numerator": None, "denominator": None, "observed": observed,
                "declared": declared if type(declared) is int else None, "status": "UNKNOWN"}
    status = "PASS" if good * threshold[1] >= declared * threshold[0] else "FAIL"
    return {"numerator": good, "denominator": declared, "observed": observed,
            "declared": declared, "status": status}


def audit_episodes(episodes, manifest, *, scorecard=None, validators=None) -> EpisodeVerdict:
    """Independent episode battery audit; outcomes do not substitute for postconditions.

    manifest.episodes independently declares episode IDs, cohort, policy,
    expected_status, expected_reason and postconditions. manifest.denominators
    and manifest.thresholds require explicit completion/exactness/trail/outcome
    supports and rational thresholds. No implicit economic or time limit exists.
    """
    manifest = _require_dict(manifest, "manifest")
    expected = _require_dict(manifest.get("episodes"), "manifest.episodes")
    denominators = _require_dict(manifest.get("denominators"), "denominators")
    thresholds = _require_dict(manifest.get("thresholds"), "thresholds")
    limits = {k: _required_fraction(thresholds, k) for k in _METRICS}
    if not isinstance(validators, (Mapping, type(None))):
        raise EvidenceError("AUSENTE: validators mapping")
    validators = validators or {}
    for key, value in validators.items():
        if key not in POLICIES or not callable(value):
            raise EvidenceError("AUSENTE: unsupported validator")
    declared = {}
    for eid, requirement in expected.items():
        if type(eid) is not str or not eid:
            raise EvidenceError("AUSENTE: episode identifier")
        requirement = _require_dict(requirement, "episode requirement")
        cohort, policy = requirement.get("cohort"), requirement.get("source_policy")
        if cohort not in COHORTS or type(policy) is not str:
            raise EvidenceError("AUSENTE: cohort or source policy")
        if cohort != "unsupported" and policy not in POLICIES:
            raise EvidenceError("AUSENTE: undeclared supported policy")
        status, reason = requirement.get("expected_status"), requirement.get("expected_reason")
        if cohort == "eligible":
            if status not in ("accepted", "rejected"):
                raise EvidenceError("AUSENTE: expected terminal")
            if status == "accepted" and reason is not None:
                raise EvidenceError("AUSENTE: accepted terminal cannot have reason")
            if status == "rejected" and (type(reason) is not str or
                    reason not in (T2_REASONS if policy == "godot_t2" else HARNESS_CODES -
                                   {"EVIDENCE/AUSENTE", "EVIDENCE/UNMAPPED"})):
                raise EvidenceError("AUSENTE: expected closed source-specific reason")
            declared_post = _require_dict(requirement.get("postconditions"), "declared postconditions")
            if not declared_post or any(type(k) is not str or not k or type(v) is not bool
                                        for k, v in declared_post.items()):
                raise EvidenceError("AUSENTE: task postcondition declarations")
        declared[eid] = requirement
    if not expected:
        raise EvidenceError("AUSENTE: empty battery membership")

    rows = tuple(episodes)
    observed_ids = [row.episode_id for row in rows if isinstance(row, EpisodeEvidence)]
    membership_ok = (len(rows) == len(expected) and len(set(observed_ids)) == len(rows)
                     and set(observed_ids) == set(expected))
    errors = Counter()
    mapped_rejections = Counter()
    if not membership_ok:
        errors["EVIDENCE/AUSENTE"] += 1
    completed = refused = incomplete = excluded = unsupported = 0
    exact_good = exact_seen = trail_good = trail_seen = outcome_good = outcome_seen = 0
    emitted_attempts = local_refusals = eligible = independent_frames = 0
    for row in rows:
        if not isinstance(row, EpisodeEvidence):
            errors["EVIDENCE/UNMAPPED"] += 1
            continue
        requirement = declared.get(row.episode_id)
        if requirement is None:
            errors["EVIDENCE/UNMAPPED"] += 1
            continue
        cohort = requirement["cohort"]
        if cohort == "excluded":
            excluded += 1
            continue
        if cohort == "unsupported":
            unsupported += 1
            continue
        eligible += 1
        if (row.source_policy != requirement["source_policy"] or row.source_policy not in POLICIES):
            errors["EVIDENCE/UNMAPPED"] += 1
            incomplete += 1
            continue
        if (type(row.emitted) is not bool or type(row.local_refusal) is not bool
                or (row.emitted and row.local_refusal) or type(row.frame_rows) is not int
                or row.frame_rows < 0 or not isinstance(row.faults, tuple)):
            errors["EVIDENCE/UNMAPPED"] += 1
            incomplete += 1
            continue
        independent_frames += row.frame_rows  # descriptive only; NOT trials
        emitted_attempts += int(row.emitted)
        local_refusals += int(row.local_refusal)
        if type(row.exactness) is bool:
            exact_seen += 1
            exact_good += int(row.exactness)
        if type(row.trail) is bool:
            trail_seen += 1
            trail_good += int(row.trail)
        valid = _outcome_valid(row.outcome, row.source_policy)
        if valid and row.source_policy in validators:
            try:
                valid = validators[row.source_policy](row.outcome) is True
            except Exception:
                valid = False
        if valid and row.emitted:
            outcome_seen += 1
        else:
            errors["EVIDENCE/AUSENTE"] += 1
        expected_status = requirement["expected_status"]
        matched = bool(valid and row.emitted and row.outcome["status"] == expected_status
                       and row.outcome["reason"] == requirement.get("expected_reason"))
        if valid and row.outcome["status"] == "rejected":
            failure = (classify_failure("engine", row.outcome["reason"], status="rejected")
                       if row.source_policy == "godot_t2"
                       else classify_failure("harness", row.outcome["reason"]))
            if failure.mapped:
                mapped_rejections[failure.code] += 1
            else:
                errors[failure.code] += 1
        elif valid and row.outcome["status"] == "accepted":
            failure = (classify_failure("engine", row.outcome["reason"], status="accepted")
                       if row.source_policy == "godot_t2"
                       else (None if row.outcome["reason"] is None else
                             Failure("EVIDENCE/UNMAPPED", "evidence", False,
                                     _digest(row.outcome["reason"]))))
            if failure is not None:
                errors[failure.code] += 1
                matched = False
        for fault in row.faults:
            if not isinstance(fault, tuple) or len(fault) != 2:
                errors["EVIDENCE/UNMAPPED"] += 1
                matched = False
                continue
            failure = classify_failure(fault[0], fault[1])
            if failure is not None:
                errors[failure.code] += 1
                matched = False
        declared_post = requirement["postconditions"]
        post_ok = (isinstance(row.postconditions, Mapping)
                   and set(row.postconditions) == set(declared_post)
                   and all(type(row.postconditions[k]) is bool
                           and row.postconditions[k] == v for k, v in declared_post.items()))
        if matched:
            outcome_good += 1
        if not matched or not post_ok:
            incomplete += 1
            if not post_ok:
                errors["EVIDENCE/AUSENTE"] += 1
        elif expected_status == "accepted":
            completed += 1
        else:
            refused += 1

    # Denominator declarations are independent evidence, never guessed from rows.
    declared_d = {name: denominators.get(name) for name in _METRICS}
    for name in _METRICS:
        if type(declared_d[name]) is not int or declared_d[name] < 0:
            declared_d[name] = None
    metrics = {
        "completion": _ratio(completed, eligible, declared_d["completion"], limits["completion"]),
        "exactness": _ratio(exact_good, exact_seen, declared_d["exactness"], limits["exactness"]),
        "trail": _ratio(trail_good, trail_seen, declared_d["trail"], limits["trail"]),
        "outcome": _ratio(outcome_good, outcome_seen, declared_d["outcome"], limits["outcome"]),
        "cohorts": {"eligible": eligible, "excluded": excluded, "unsupported": unsupported},
        "attempts": {"emitted": emitted_attempts, "local_refused": local_refusals},
        "evidence": {"complete_episodes": completed + refused + incomplete,
                     "correlated_frame_rows": independent_frames},
    }
    expected_eligible = sum(req["cohort"] == "eligible" for req in declared.values())
    if eligible != expected_eligible or eligible != completed + refused + incomplete:
        errors["EVIDENCE/AUSENTE"] += 1
    scorecard_ok = True
    if scorecard is not None:
        scorecard_ok = (getattr(scorecard, "passed", None) is True
                        and isinstance(getattr(scorecard, "metrics", None), Mapping)
                        and all(isinstance(scorecard.metrics.get(k), Mapping)
                                and type(scorecard.metrics[k].get("denominator")) is int
                                and scorecard.metrics[k]["denominator"] > 0
                                for k in ("frame_exactness", "terminal_completeness")))
        if not scorecard_ok:
            errors["EVIDENCE/AUSENTE"] += 1
    passed = (membership_ok and eligible > 0 and scorecard_ok and not errors
              and incomplete == 0 and all(metrics[k]["status"] == "PASS" for k in _METRICS))
    confidence = {"status": "AUSENTE", "unit": "complete-episode", "method": None,
                  "lower": None, "upper": None}
    return EpisodeVerdict(eligible, completed, refused, incomplete, excluded,
                          dict(sorted((errors + mapped_rejections).items())), metrics, None, "DESIGNING", confidence, passed)
